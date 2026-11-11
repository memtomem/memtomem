"""Tests for the JOB_KINDS registry (P2 cron Phase A.2)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from memtomem.scheduler import JOB_KINDS, JobSpec
from memtomem.scheduler.jobs import validate_new_params
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

    def test_compaction_description_matches_its_runner(self):
        # Phase B hands these descriptions to a model choosing jobs, so the
        # compaction line must say what ``_run_compaction`` does: it holds
        # sources outside search and never deletes (#2498). The old "Delete
        # chunks whose source files no longer exist" told it the opposite.
        # Positive phrases, not "delete" absent: the honest wording names the
        # deletion it does *not* do.
        description = JOB_KINDS["compaction"].description
        assert description.startswith("Hold chunks of missing sources"), description
        assert "never deletes" in description, description
        assert "mm gc orphan-sources --apply" in description, description


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

    def stat(self):
        if self._seq:
            self._last = self._seq.pop(0)
        if not self._last:
            raise FileNotFoundError(self._name)
        return None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<src {self._name}>"


def _fake_app(sources, *, delete_return: int = 3) -> MagicMock:
    app = MagicMock()
    app.storage.get_all_source_files = AsyncMock(return_value=set(sources))
    app.storage.delete_by_source = AsyncMock(return_value=delete_return)
    app.storage.hold_source = AsyncMock(return_value=True)
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
        assert result["held_sources"] == 12
        app.storage.delete_by_source.assert_not_awaited()
        app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_small_stable_orphan_is_held(self):
        """A confirmed orphan below the old mass-delete floor is held."""
        gone = _ScriptedSource("gone", [False, False])
        present = [_ScriptedSource(f"ok-{i}", [True]) for i in range(4)]
        app = _fake_app([gone, *present], delete_return=3)

        result = await JOB_KINDS["compaction"].runner(app)

        assert result["chunks_deleted"] == 0
        assert result["orphan_files"] == 1
        assert result["held_sources"] == 1
        assert result["sources_checked"] == 5
        assert "skipped_reason" not in result
        app.storage.delete_by_source.assert_not_awaited()
        app.storage.hold_source.assert_awaited_once_with(gone, "scan_missing", as_stored=True)
        app.search_pipeline.invalidate_cache.assert_called_once()


class TestValidateNewParams:
    """Registration refuses keys the params model does not declare."""

    @pytest.mark.parametrize("name", list(JOB_KINDS))
    def test_params_fields_declare_no_alias(self, name):
        # ``validate_new_params`` diffs against field names only; an alias
        # would let a correctly spelled key be refused, or a typo pass.
        for field in JOB_KINDS[name].params_model.model_fields.values():
            assert field.alias is None
            assert field.validation_alias is None

    def test_unknown_keys_named_in_sorted_order(self):
        with pytest.raises(ValueError) as excinfo:
            validate_new_params(JOB_KINDS["importance_decay"], {"zz": 1, "max_age": 3})
        assert str(excinfo.value) == (
            "unknown params for importance_decay: 'max_age', 'zz' "
            "(allowed: max_age_days, source_filter)"
        )

    def test_known_keys_return_the_validated_model(self):
        validated = validate_new_params(JOB_KINDS["dedup_scan"], {"limit": 20})
        assert validated.model_dump() == {"threshold": 0.92, "limit": 20, "max_scan": 500}

    def test_value_errors_still_come_from_the_model(self):
        with pytest.raises(ValidationError):
            validate_new_params(JOB_KINDS["dedup_scan"], {"limit": 0})

    def test_non_dict_is_rejected_by_the_model_not_split_into_keys(self):
        with pytest.raises(ValidationError) as excinfo:
            validate_new_params(JOB_KINDS["dedup_scan"], "foo")
        assert "unknown params" not in str(excinfo.value)


class TestMcpScheduleRegister:
    @pytest.fixture
    def tools(self, components, monkeypatch):
        from memtomem.server.tools import schedule as schedule_tools

        app = AppContext.from_components(components)
        monkeypatch.setattr(schedule_tools, "_get_app_initialized", AsyncMock(return_value=app))
        return schedule_tools

    @pytest.mark.asyncio
    async def test_unknown_key_refused_and_not_stored(self, tools, components):
        out = json.loads(await tools.mem_schedule_register("* * * * *", "dedup_scan", {"limt": 20}))
        assert out == {
            "ok": False,
            "reason": (
                "invalid params: unknown params for dedup_scan: 'limt' "
                "(allowed: limit, max_scan, threshold)"
            ),
        }
        assert await components.storage.schedule_list_all() == []

    @pytest.mark.asyncio
    async def test_known_key_stored(self, tools, components):
        out = json.loads(
            await tools.mem_schedule_register("* * * * *", "dedup_scan", {"limit": 20})
        )
        assert out["ok"] is True
        sched = await components.storage.schedule_get(out["id"])
        assert sched["params"] == {"limit": 20}

    @pytest.mark.asyncio
    async def test_non_dict_params_refused_without_listing_characters(self, tools, components):
        out = json.loads(await tools.mem_schedule_register("* * * * *", "dedup_scan", "foo"))
        assert out["ok"] is False
        assert out["reason"].startswith("invalid params: ")
        assert "unknown params" not in out["reason"]
        assert await components.storage.schedule_list_all() == []
