"""Tests for the JOB_KINDS registry (P2 cron Phase A.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from memtomem.scheduler import JOB_KINDS, JobSpec
from memtomem.server.context import AppContext
from memtomem.storage import orphan_detect


@pytest.fixture
def app(components):
    return AppContext.from_components(components)


class TestJobRegistryShape:
    def test_four_kinds_registered(self):
        assert set(JOB_KINDS.keys()) == {
            "compaction",
            "importance_decay",
            "dead_chunk_link_cleanup",
            "dedup_scan",
        }

    @pytest.mark.parametrize("name", list(JOB_KINDS))
    def test_specs_well_formed(self, name):
        spec = JOB_KINDS[name]
        assert isinstance(spec, JobSpec)
        assert spec.name == name
        assert spec.description and isinstance(spec.description, str)
        # The Phase B contract: each params_model must be JSON-schemable.
        schema = spec.params_model.model_json_schema()
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        # Default-construction must succeed (every param has a default).
        instance = spec.params_model()
        assert spec.params_model.model_validate(instance.model_dump()) == instance


class TestRunners:
    @pytest.mark.asyncio
    async def test_compaction_idempotent_on_empty(self, app):
        spec = JOB_KINDS["compaction"]
        result = await spec.runner(app, **spec.params_model().model_dump())
        assert result["chunks_deleted"] == 0
        assert result["orphan_files"] == 0
        # Re-run is still a no-op.
        result2 = await spec.runner(app, **spec.params_model().model_dump())
        assert result2["chunks_deleted"] == 0

    @pytest.mark.asyncio
    async def test_importance_decay_zero_on_empty(self, app):
        spec = JOB_KINDS["importance_decay"]
        result = await spec.runner(app, **spec.params_model().model_dump())
        assert result["deleted_chunks"] == 0
        assert result["expired_chunks"] == 0

    @pytest.mark.asyncio
    async def test_importance_decay_validates_params(self):
        spec = JOB_KINDS["importance_decay"]
        with pytest.raises(ValidationError):
            spec.params_model.model_validate({"max_age_days": -1})

    @pytest.mark.asyncio
    async def test_dead_chunk_link_cleanup_zero_on_empty(self, app):
        spec = JOB_KINDS["dead_chunk_link_cleanup"]
        result = await spec.runner(app, **spec.params_model().model_dump())
        assert result == {"dead_links_deleted": 0}

    @pytest.mark.asyncio
    async def test_dedup_scan_zero_on_empty(self, app):
        spec = JOB_KINDS["dedup_scan"]
        result = await spec.runner(app, **spec.params_model().model_dump())
        assert "candidates" in result
        assert result["candidates"] == 0

    @pytest.mark.asyncio
    async def test_dedup_scan_validates_threshold(self):
        spec = JOB_KINDS["dedup_scan"]
        with pytest.raises(ValidationError):
            spec.params_model.model_validate({"threshold": 1.5})


class TestDeadLinkCleanupSemantics:
    @pytest.mark.asyncio
    async def test_removes_only_null_source_rows(self, app):
        """Only rows with source_id IS NULL should be deleted."""
        from datetime import datetime, timezone

        db = app.storage._get_db()
        # Insert a chunk so we have a valid target_id (FK on target).
        db.execute(
            "INSERT INTO chunks (id, content, content_hash, source_file, "
            "created_at, updated_at) "
            "VALUES (?, '', 'h', 's', ?, ?)",
            ("chunk-a", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Dead row: source NULL
        db.execute(
            "INSERT INTO chunk_links (source_id, target_id, link_type, "
            "namespace_target, created_at) VALUES (NULL, 'chunk-a', 'shared', 'default', ?)",
            (now,),
        )
        # Live row: source present (use chunk-a as both source and target with
        # link_type='summarizes' to satisfy the (target_id, link_type) PK).
        db.execute(
            "INSERT INTO chunk_links (source_id, target_id, link_type, "
            "namespace_target, created_at) VALUES ('chunk-a', 'chunk-a', "
            "'summarizes', 'default', ?)",
            (now,),
        )
        db.commit()

        spec = JOB_KINDS["dead_chunk_link_cleanup"]
        result = await spec.runner(app)
        assert result["dead_links_deleted"] == 1

        # Live row survives.
        remaining = db.execute("SELECT link_type FROM chunk_links ORDER BY link_type").fetchall()
        assert [r[0] for r in remaining] == ["summarizes"]


class _ScriptedSource:
    """Source path whose ``exists()`` yields a scripted True/False sequence."""

    def __init__(self, name: str, exists_seq: list[bool]) -> None:
        self._name = name
        self._seq = list(exists_seq)
        self._last = self._seq[-1]

    def exists(self) -> bool:
        if self._seq:
            self._last = self._seq.pop(0)
        return self._last

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<src {self._name}>"


def _fake_app(sources, *, delete_return: int = 3) -> MagicMock:
    app = MagicMock()
    app.storage.get_all_source_files = AsyncMock(return_value=set(sources))
    app.storage.delete_by_source = AsyncMock(return_value=delete_return)
    app.search_pipeline.invalidate_cache = MagicMock()
    return app


class TestCompactionOrphanGuards:
    """#1565 — compaction must not mass-delete on a transient/mount blip."""

    @pytest.fixture(autouse=True)
    def _no_delay(self, monkeypatch):
        # Keep the two-pass re-check instant in tests.
        monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)

    @pytest.mark.asyncio
    async def test_transient_absence_not_deleted(self):
        """A source absent on pass 1 but back on pass 2 is never deleted."""
        transient = _ScriptedSource("flaky", [False, True])
        present = _ScriptedSource("ok", [True])
        app = _fake_app([transient, present])

        result = await JOB_KINDS["compaction"].runner(app)

        assert result["chunks_deleted"] == 0
        assert result["orphan_files"] == 0
        assert result["sources_checked"] == 2
        assert "skipped_reason" not in result
        app.storage.delete_by_source.assert_not_awaited()
        app.search_pipeline.invalidate_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_mass_orphan_event_skips_delete(self):
        """Many sources vanishing at once is refused, not deleted."""
        sources = [_ScriptedSource(f"gone-{i}", [False, False]) for i in range(12)]
        app = _fake_app(sources)

        result = await JOB_KINDS["compaction"].runner(app)

        assert result["chunks_deleted"] == 0
        assert result["orphan_files"] == 12
        assert result["sources_checked"] == 12
        assert result["skipped_reason"] == "orphan_ratio_exceeded"
        app.storage.delete_by_source.assert_not_awaited()
        app.search_pipeline.invalidate_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_small_stable_orphan_is_deleted(self):
        """A confirmed orphan below the mass-delete brake deletes normally."""
        gone = _ScriptedSource("gone", [False, False])
        present = [_ScriptedSource(f"ok-{i}", [True]) for i in range(4)]
        app = _fake_app([gone, *present], delete_return=3)

        result = await JOB_KINDS["compaction"].runner(app)

        assert result["chunks_deleted"] == 3
        assert result["orphan_files"] == 1
        assert result["sources_checked"] == 5
        assert "skipped_reason" not in result
        app.storage.delete_by_source.assert_awaited_once_with(gone)
        app.search_pipeline.invalidate_cache.assert_called_once()
