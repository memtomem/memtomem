"""Maintenance run log — mixin behavior and the mem_policy_list read surface (#2132)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memtomem.storage.mixins.maintenance_runs import _MAINTENANCE_RUN_MAX_AGE_DAYS


class TestMaintenanceRunMixin:
    @pytest.mark.asyncio
    async def test_start_then_finish_ok_roundtrip(self, storage):
        run_id = await storage.maintenance_run_start(
            "auto_expire", policy_name="expire-old", source="scheduler"
        )
        assert run_id > 0

        await storage.maintenance_run_finish(
            run_id,
            status="ok",
            affected_count=3,
            namespaces=["work", "default", "work", ""],
            summary={"deleted_ids": ["a", "b", "c"]},
        )

        (run,) = await storage.maintenance_run_latest(policy_name="expire-old")
        assert run["id"] == run_id
        assert run["kind"] == "auto_expire"
        assert run["source"] == "scheduler"
        assert run["status"] == "ok"
        assert run["affected_count"] == 3
        # sorted, deduped, empty dropped
        assert run["namespaces"] == ["default", "work"]
        assert run["summary"] == {"deleted_ids": ["a", "b", "c"]}
        assert run["error"] is None
        assert run["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_finish_error_records_text(self, storage):
        run_id = await storage.maintenance_run_start("auto_tag", policy_name="tagger")
        await storage.maintenance_run_finish(run_id, status="error", error="boom")

        (run,) = await storage.maintenance_run_latest(kind="auto_tag")
        assert run["status"] == "error"
        assert run["error"] == "boom"
        assert run["affected_count"] == 0

    @pytest.mark.asyncio
    async def test_finish_rejects_non_final_status(self, storage):
        run_id = await storage.maintenance_run_start("auto_expire")
        with pytest.raises(ValueError):
            await storage.maintenance_run_finish(run_id, status="running")

    @pytest.mark.asyncio
    async def test_running_row_has_no_completed_at(self, storage):
        """An interrupted run stays visible as evidence, not as a lost record."""
        run_id = await storage.maintenance_run_start("auto_archive", policy_name="arch")

        (run,) = await storage.maintenance_run_latest(policy_name="arch")
        assert run["id"] == run_id
        assert run["status"] == "running"
        assert run["completed_at"] is None

    @pytest.mark.asyncio
    async def test_latest_filters_and_orders_newest_first(self, storage):
        first = await storage.maintenance_run_start("auto_expire", policy_name="p1")
        second = await storage.maintenance_run_start("auto_expire", policy_name="p1")
        other = await storage.maintenance_run_start("auto_tag", policy_name="p2")
        for rid in (first, second, other):
            await storage.maintenance_run_finish(rid, status="ok")

        runs = await storage.maintenance_run_latest(kind="auto_expire", limit=10)
        assert [r["id"] for r in runs] == [second, first]

        runs = await storage.maintenance_run_latest(policy_name="p2", limit=10)
        assert [r["id"] for r in runs] == [other]

        runs = await storage.maintenance_run_latest(limit=10)
        assert [r["id"] for r in runs] == [other, second, first]

        assert await storage.maintenance_run_latest(limit=0) == []

    @pytest.mark.asyncio
    async def test_prune_drops_rows_older_than_max_age(self, storage):
        stale = await storage.maintenance_run_start("auto_expire", policy_name="old")
        await storage.maintenance_run_finish(stale, status="ok")

        backdated = (
            datetime.now(timezone.utc) - timedelta(days=_MAINTENANCE_RUN_MAX_AGE_DAYS + 1)
        ).isoformat(timespec="seconds")
        db = storage._get_db()
        db.execute("UPDATE maintenance_runs SET started_at = ? WHERE id = ?", (backdated, stale))
        db.commit()

        fresh = await storage.maintenance_run_start("auto_expire", policy_name="new")

        ids = {r["id"] for r in await storage.maintenance_run_latest(limit=50)}
        assert stale not in ids
        assert fresh in ids


class TestPolicyListRunsRendering:
    """mem_policy_list(runs=N) is the read surface for the run log (#2132)."""

    @pytest.mark.asyncio
    async def test_runs_zero_keeps_the_default_output(self, components):
        from memtomem.server.tools.policy import mem_policy_list

        from tests.test_tools_logic import _fake_ctx

        await components.storage.policy_add("expire", "auto_expire", {"max_age_days": 90})
        run_id = await components.storage.maintenance_run_start(
            "auto_expire", policy_name="expire", source="scheduler"
        )
        await components.storage.maintenance_run_finish(run_id, status="ok", affected_count=1)

        out = await mem_policy_list(ctx=_fake_ctx(components))
        assert "**expire**" in out
        assert "Runs:" not in out

    @pytest.mark.asyncio
    async def test_runs_renders_history_with_truncated_ids(self, components):
        from memtomem.server.tools.policy import mem_policy_list

        from tests.test_tools_logic import _fake_ctx

        storage = components.storage
        await storage.policy_add("expire", "auto_expire", {"max_age_days": 90})

        first = await storage.maintenance_run_start(
            "auto_expire", policy_name="expire", source="scheduler"
        )
        await storage.maintenance_run_finish(
            first,
            status="ok",
            affected_count=8,
            namespaces=["default", "work"],
            summary={"deleted_ids": [f"id-{i}" for i in range(8)]},
        )
        second = await storage.maintenance_run_start(
            "auto_expire", policy_name="expire", source="mcp"
        )
        await storage.maintenance_run_finish(second, status="error", error="disk full")
        interrupted = await storage.maintenance_run_start(
            "auto_expire", policy_name="expire", source="scheduler"
        )

        out = await mem_policy_list(runs=5, ctx=_fake_ctx(components))

        assert "Runs:" in out
        # newest first
        assert out.index(f"#{interrupted}") < out.index(f"#{second}") < out.index(f"#{first}")
        assert f"#{interrupted} running " in out
        assert "disk full" in out
        assert "ns=default,work" in out
        assert "deleted_ids: 8 — id-0, id-1, id-2, id-3, id-4, … (+3 more)" in out

    @pytest.mark.asyncio
    async def test_runs_surfaces_agent_path_rows_and_empty_history(self, components):
        from memtomem.server.tools.policy import mem_policy_list

        from tests.test_tools_logic import _fake_ctx

        storage = components.storage
        await storage.policy_add("expire", "auto_expire", {"max_age_days": 90})
        agent_run = await storage.maintenance_run_start("consolidate_apply", source="mcp")
        await storage.maintenance_run_finish(
            agent_run,
            status="ok",
            affected_count=3,
            namespaces=["default"],
            summary={"summary_id": "abc123", "linked": 3},
        )

        out = await mem_policy_list(runs=3, ctx=_fake_ctx(components))

        assert "Runs: none recorded" in out
        assert "Other maintenance runs:" in out
        assert "summary abc123 — 3 originals linked" in out

    @pytest.mark.asyncio
    async def test_deleted_policy_history_stays_reachable(self, components):
        """Records outlive the policy that wrote them (#2132 review)."""
        from memtomem.server.tools.policy import mem_policy_list

        from tests.test_tools_logic import _fake_ctx

        storage = components.storage
        run_id = await storage.maintenance_run_start(
            "auto_expire", policy_name="deleted-policy", source="scheduler"
        )
        await storage.maintenance_run_finish(run_id, status="ok", affected_count=4)

        # No policy rows at all — the early return must not swallow history.
        out = await mem_policy_list(runs=3, ctx=_fake_ctx(components))
        assert "No policies configured" in out
        assert "Other maintenance runs:" in out
        assert f"#{run_id}" in out

        # …and it stays visible once an unrelated policy exists.
        await storage.policy_add("other", "auto_tag", {})
        out = await mem_policy_list(runs=3, ctx=_fake_ctx(components))
        assert f"#{run_id}" in out


class TestConsolidateApplyRunRecord:
    """The agent path bypasses run_policy, so it records its own run (#2132).

    These cover the branches that need no embedding backend; the linked happy
    path is asserted in the ollama-marked integration test in
    ``test_tools_logic.py::TestMemConsolidateApplyIntegration``.
    """

    @staticmethod
    async def _stash_group(storage, chunk_ids: list[str]) -> None:
        import json as _json

        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        await storage.scratch_set(
            "consolidation_groups",
            _json.dumps(
                [
                    {
                        "group_id": 0,
                        "source": "/tmp/notes.md",
                        "chunk_ids": chunk_ids,
                        "chunk_count": len(chunk_ids),
                        "namespace": "work",
                        "previews": [],
                    }
                ]
            ),
            expires_at=expires_at,
        )

    @pytest.mark.asyncio
    async def test_refused_call_leaves_no_row(self, components):
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        from tests.test_tools_logic import _fake_ctx

        await self._stash_group(components.storage, ["not-a-uuid"])

        out = await mem_consolidate_apply(group_id=0, summary="x", ctx=_fake_ctx(components))
        assert "invalid chunk_ids" in out
        assert await components.storage.maintenance_run_latest(kind="consolidate_apply") == []

    @pytest.mark.asyncio
    async def test_unlinked_branch_records_ok_row_with_warning(self, components):
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        from tests.test_tools_logic import _fake_ctx

        await self._stash_group(components.storage, [])

        # Empty content short-circuits _mem_add_core with stats=None, which is
        # the unlinked branch — no summary chunk id to link originals to.
        out = await mem_consolidate_apply(group_id=0, summary="", ctx=_fake_ctx(components))
        assert "unlinked" in out

        (run,) = await components.storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "ok"
        assert run["source"] == "mcp"
        assert run["affected_count"] == 0
        assert run["namespaces"] == ["work"]
        assert run["summary"]["warning"] == "unlinked"
        assert run["summary"]["summary_id"] is None
        assert run["summary"]["group_id"] == 0

    @pytest.mark.asyncio
    async def test_failure_records_error_row(self, components, monkeypatch):
        import memtomem.server.tools.memory_crud as memory_crud
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        from tests.test_tools_logic import _fake_ctx

        await self._stash_group(components.storage, [])

        async def boom(*args, **kwargs):
            raise RuntimeError("write failed")

        monkeypatch.setattr(memory_crud, "_mem_add_core", boom)

        # The re-raise is caught by @tool_handler, which renders it as an
        # error string — the run row is what survives the failure.
        out = await mem_consolidate_apply(group_id=0, summary="x", ctx=_fake_ctx(components))
        assert "write failed" in out

        (run,) = await components.storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "error"
        assert run["error"] == "RuntimeError: write failed"
        assert run["completed_at"] is not None
