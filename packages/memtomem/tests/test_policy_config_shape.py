"""A policy config must be a JSON object (#2955).

Every policy handler reads its settings with ``config.get``. ``mem_policy_add``
used to store any JSON value, and running such a row raised out of
``run_policy`` and ended the batch.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from helpers import make_chunk

from memtomem.tools.policy_engine import _VALID_TYPES, run_all_enabled, run_policy

NON_OBJECTS = [
    ("[1]", "an array"),
    ("5", "a number"),
    ("1.5", "a number"),
    ('"x"', "a string"),
    ("true", "a boolean"),
    ("null", "null"),
]


class TestMemPolicyAdd:
    @pytest.mark.parametrize("config,kind", NON_OBJECTS)
    async def test_a_non_object_config_is_refused(self, components, config, kind):
        from memtomem.server.tools.policy import mem_policy_add

        from tests.test_tools_logic import _fake_ctx

        out = await mem_policy_add(
            name="p", policy_type="auto_expire", config=config, ctx=_fake_ctx(components)
        )
        assert out == f"Error: config must be a JSON object, got {kind}"
        assert await components.storage.policy_get("p") is None

    @pytest.mark.parametrize("config", ["{}", '{"max_age_days": 90}'])
    async def test_an_object_config_is_stored(self, components, config):
        from memtomem.server.tools.policy import mem_policy_add

        from tests.test_tools_logic import _fake_ctx

        out = await mem_policy_add(
            name="p", policy_type="auto_expire", config=config, ctx=_fake_ctx(components)
        )
        assert "created" in out
        assert (await components.storage.policy_get("p"))["config"] == json.loads(config)


class TestStoredNonObjectConfig:
    """A row stored before the check runs to an error result instead of raising."""

    @pytest.mark.parametrize("ptype", sorted(_VALID_TYPES))
    @pytest.mark.parametrize("config,kind", NON_OBJECTS)
    async def test_applied_run_records_an_error(self, storage, ptype, config, kind):
        await storage.policy_add("p", ptype, json.loads(config))
        policy = await storage.policy_get("p")

        result = await run_policy(storage, policy, dry_run=False)

        assert result.error == f"config must be a JSON object, got {kind}"
        assert result.details == f"Error: {result.error}"
        assert result.affected_count == 0 and result.mutated is False
        (run,) = await storage.maintenance_run_latest(limit=5)
        assert run["status"] == "error"
        assert run["error"] == result.error

    async def test_dry_run_writes_no_row(self, storage):
        await storage.policy_add("p", "auto_archive", [1])
        policy = await storage.policy_get("p")

        result = await run_policy(storage, policy, dry_run=True)

        assert result.error
        assert await storage.maintenance_run_latest(limit=5) == []

    async def test_a_missing_config_still_means_the_defaults(self, storage):
        policy = {"name": "p", "policy_type": "auto_expire", "namespace_filter": None}
        result = await run_policy(storage, policy, dry_run=True)
        assert result.error is None

    async def test_the_batch_continues_past_it(self, storage):
        chunk = make_chunk("stale")
        await storage.upsert_chunks([chunk])
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old])
        db.commit()
        await storage.policy_add("bad", "auto_archive", [1])
        await storage.policy_add("good", "auto_archive", {"max_age_days": 30})
        db.execute(
            "UPDATE memory_policies SET created_at = CASE name "
            "WHEN 'bad' THEN '2026-01-01T00:00:00' ELSE '2026-01-02T00:00:00' END"
        )
        db.commit()

        results = await run_all_enabled(storage, dry_run=False)

        assert [r.policy_name for r in results] == ["bad", "good"]
        assert results[0].error and results[1].error is None
        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(chunk.id)]).fetchone()
        assert row[0] == "archive"
