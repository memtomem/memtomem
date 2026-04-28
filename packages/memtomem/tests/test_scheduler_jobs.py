"""Tests for the JOB_KINDS registry (P2 cron Phase A.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memtomem.scheduler import JOB_KINDS, JobSpec
from memtomem.server.context import AppContext


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
