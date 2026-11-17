"""The scheduled ``dedup_scan`` job walks the store (#3432).

Each run scans the pool after the one the schedule's last finished run
scanned, and a run that reaches the end starts the next one from the first
pool. The position is stored on the schedule row with the outcome of the run
that moved it. Every path that runs a job (the watchdog dispatcher,
``mem_schedule_run_now``, ``mm schedule run-now``) reads and writes it the
same way, so each behaviour here is checked through all three, against a real
store.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import HealthWatchdogConfig, SchedulerConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.search.dedup import DedupScanner
from memtomem.server.context import AppContext
from memtomem.server.health_watchdog import HealthWatchdog
from memtomem.server.tools import schedule as schedule_tools

SURFACES = ["dispatcher", "mcp", "cli"]

# Five chunks and ``max_scan=2``: three pools, the last one short.
IDS = [UUID(int=n) for n in range(1, 6)]
PARAMS = {"max_scan": 2}
SECOND_POOL = {"after": str(IDS[1])}
THIRD_POOL = {"after": str(IDS[3])}


class _Store:
    """One schedule surface over the test store, driven from a sync test."""

    def __init__(self, surface, components, memory_dir, monkeypatch):
        self.surface = surface
        self.components = components
        self.storage = components.storage
        self.app = AppContext.from_components(components)
        self.timeout = 5.0
        self.cursors: list[str | None] = []
        self._cli = CliRunner()

        @asynccontextmanager
        async def fake_components():
            yield components

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
        monkeypatch.setattr(
            schedule_tools, "_get_app_initialized", AsyncMock(return_value=self.app)
        )
        # The CLI imports this at call time, so one patch covers both run-now paths.
        monkeypatch.setattr(schedule_tools, "_run_now_timeout", lambda app: self.timeout)

        real = self.storage.list_visible_chunks_by_id

        async def recording(limit, after=None):
            self.cursors.append(after)
            return await real(limit, after)

        monkeypatch.setattr(self.storage, "list_visible_chunks_by_id", recording)

        chunks = [
            Chunk(
                content=f"body number {n}",
                metadata=ChunkMetadata(source_file=memory_dir / "walk.md"),
                id=chunk_id,
                embedding=[],
            )
            for n, chunk_id in enumerate(IDS)
        ]
        asyncio.run(self.storage.upsert_chunks(chunks))

    def add(self, job_kind="dedup_scan", params=None, state=None) -> str:
        async def go():
            sid = await self.storage.schedule_insert("* * * * *", job_kind, params or {})
            if state is not None:
                await self.storage.schedule_mark_run(sid, "ok", result={}, state=state)
            return sid

        return asyncio.run(go())

    def get(self, sid) -> dict:
        return asyncio.run(self.storage.schedule_get(sid))

    def sql(self, statement, *values) -> None:
        db = self.storage._get_db()
        db.execute(statement, values)
        db.commit()

    def run(self, sid):
        """Run *sid* through the surface; return what the surface handed back."""
        if self.surface == "dispatcher":

            async def go():
                sched = await self.storage.schedule_get(sid)
                watchdog = HealthWatchdog(
                    self.app,
                    HealthWatchdogConfig(enabled=True),
                    SchedulerConfig(enabled=True, runner_timeout_seconds=self.timeout),
                )
                await watchdog._run_schedule(sched, self.timeout)

            asyncio.run(go())
            return None
        if self.surface == "mcp":
            return json.loads(asyncio.run(schedule_tools.mem_schedule_run_now(sid)))
        return self._cli.invoke(cli, ["schedule", "run-now", sid])

    def returned_result(self, sid, returned) -> dict:
        """The result the surface returned; the dispatcher's is only in the record."""
        if self.surface == "dispatcher":
            return self.get(sid)["last_run_result"]
        if self.surface == "mcp":
            assert (returned["ok"], returned["reason"]) == (True, "ran"), returned
            assert set(returned) == {"ok", "reason", "result"}
            return returned["result"]
        assert returned.exit_code == 0, returned.output
        head, shown_id, payload = returned.stdout.strip().split("  ", 2)
        assert (head, shown_id) == ("OK", sid)
        return json.loads(payload)


@pytest.fixture(params=SURFACES)
def store(request, components, memory_dir, monkeypatch):
    return _Store(request.param, components, memory_dir, monkeypatch)


@pytest.fixture
def mcp_store(components, memory_dir, monkeypatch):
    return _Store("mcp", components, memory_dir, monkeypatch)


class TestWalk:
    def test_each_run_continues_and_the_end_starts_over(self, store):
        sid = store.add(params=PARAMS)
        assert store.get(sid)["state"] is None

        # (cursor the run starts after, chunks in its pool, chunks left, next cursor)
        expected = [
            (None, 2, 3, str(IDS[1])),
            (str(IDS[1]), 2, 1, str(IDS[3])),
            (str(IDS[3]), 1, 0, None),
            (None, 2, 3, str(IDS[1])),
        ]
        for after, pool, remaining, next_after in expected:
            result = store.returned_result(sid, store.run(sid))
            sched = store.get(sid)
            assert sched["last_run_status"] == "ok"
            assert sched["last_run_result"] == result
            assert (result["after"], result["pool"], result["remaining"]) == (
                after,
                pool,
                remaining,
            )
            assert result["next_after"] == next_after
            assert sched["state"] == (None if next_after is None else {"after": next_after})

        # What the scanner asked the store for, run by run: the stored cursor.
        assert store.cursors == [after for after, *_ in expected]

    def test_an_empty_pool_above_the_cursor_clears_the_position(self, store):
        # Everything above the stored cursor is gone: the run reached the end,
        # so the next one starts from the first pool. Keeping the cursor here
        # would stop the schedule looking at any chunk again.
        beyond = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        sid = store.add(params=PARAMS, state={"after": beyond})

        result = store.returned_result(sid, store.run(sid))

        assert (result["after"], result["pool"], result["remaining"]) == (beyond, 0, 0)
        assert result["next_after"] is None
        assert store.get(sid)["state"] is None
        assert store.cursors == [beyond]

    def test_a_skipped_run_keeps_the_position(self, store, monkeypatch):
        sid = store.add(params=PARAMS, state=SECOND_POOL)
        monkeypatch.setattr(AppContext, "dedup_scanner", property(lambda self: None))

        store.run(sid)

        sched = store.get(sid)
        assert sched["last_run_status"] == "skipped"
        assert sched["last_run_result"] == {
            "candidates": 0,
            "skipped_reason": "dedup_scanner_not_initialized",
        }
        assert sched["state"] == SECOND_POOL
        assert store.cursors == []

    @pytest.mark.parametrize("failure", ["error", "timeout", "unknown_kind", "invalid_params"])
    def test_a_run_that_did_not_finish_keeps_the_position(self, store, monkeypatch, failure):
        sid = store.add(params=PARAMS, state=SECOND_POOL)
        status = "error"
        if failure == "error":

            async def boom(self, *args, **kwargs):
                raise RuntimeError("boom")

            monkeypatch.setattr(DedupScanner, "scan_with_coverage", boom)
        elif failure == "timeout":

            async def slow(self, *args, **kwargs):
                await asyncio.sleep(1.0)

            monkeypatch.setattr(DedupScanner, "scan_with_coverage", slow)
            store.timeout = 0.05
            status = "timeout"
        elif failure == "unknown_kind":
            store.sql("UPDATE schedules SET job_kind='gone' WHERE id=?", sid)
        else:
            store.sql("UPDATE schedules SET params_json=? WHERE id=?", '{"limit": 0}', sid)

        store.run(sid)

        sched = store.get(sid)
        assert sched["last_run_status"] == status
        assert sched["last_run_result"] is None
        assert sched["state"] == SECOND_POOL

    def test_a_position_that_is_not_a_chunk_id_starts_from_the_first_pool(self, store, caplog):
        sid = store.add(params=PARAMS, state={"after": "not-a-chunk-id"})

        with caplog.at_level(logging.WARNING, logger="memtomem.scheduler.jobs"):
            result = store.returned_result(sid, store.run(sid))

        assert (result["after"], result["next_after"]) == (None, str(IDS[1]))
        assert store.get(sid)["state"] == SECOND_POOL
        assert store.cursors == [None]
        assert [r.getMessage() for r in caplog.records if r.name == "memtomem.scheduler.jobs"] == [
            "dedup_scan: stored position {'after': 'not-a-chunk-id'} is not a chunk id; "
            "starting from the first pool"
        ]

    def test_a_position_too_deeply_nested_to_parse_starts_from_the_first_pool(self, store):
        # The parser raises RecursionError on this, not ValueError. The run
        # must still happen, from the first pool, and leave a usable position.
        sid = store.add(params=PARAMS)
        store.sql("UPDATE schedules SET job_state=? WHERE id=?", "[" * 10000, sid)

        result = store.returned_result(sid, store.run(sid))

        assert (result["after"], result["next_after"]) == (None, str(IDS[1]))
        sched = store.get(sid)
        assert (sched["last_run_status"], sched["state"]) == ("ok", SECOND_POOL)
        assert store.cursors == [None]


class TestSchedulesAreSeparate:
    def test_two_schedules_of_the_kind_keep_their_own_position(self, mcp_store):
        first = mcp_store.add(params=PARAMS)
        second = mcp_store.add(params=PARAMS)

        mcp_store.run(first)
        mcp_store.run(first)
        mcp_store.run(second)

        assert mcp_store.get(first)["state"] == THIRD_POOL
        assert mcp_store.get(second)["state"] == SECOND_POOL
        assert mcp_store.cursors == [None, str(IDS[1]), None]

    def test_a_job_that_keeps_no_state_leaves_the_column_alone(self, mcp_store):
        # No job but dedup_scan writes a state. A stored one (left by another
        # binary, say) is not this run's to clear.
        sid = mcp_store.add(job_kind="dead_chunk_link_cleanup", state={"left": "alone"})

        out = mcp_store.run(sid)

        assert out == {"ok": True, "reason": "ran", "result": {"dead_links_deleted": 0}}
        assert mcp_store.get(sid)["state"] == {"left": "alone"}


class TestListing:
    def test_every_listing_carries_the_position(self, mcp_store):
        walking = mcp_store.add(params=PARAMS)
        other = mcp_store.add(job_kind="dead_chunk_link_cleanup")
        mcp_store.run(walking)
        mcp_store.run(other)
        expected = {walking: SECOND_POOL, other: None}

        listed = json.loads(asyncio.run(schedule_tools.mem_schedule_list()))["schedules"]
        assert {row["id"]: row["state"] for row in listed} == expected

        cli_runner = CliRunner()
        rows = json.loads(cli_runner.invoke(cli, ["schedule", "list", "--json"]).stdout)
        assert {row["id"]: row["state"] for row in rows} == expected

    def test_the_plain_listing_is_unchanged(self, mcp_store):
        sid = mcp_store.add(params=PARAMS)
        mcp_store.run(sid)
        last = mcp_store.get(sid)["last_run_at"]

        listed = CliRunner().invoke(cli, ["schedule", "list"])

        assert listed.exit_code == 0, listed.output
        assert listed.output.splitlines() == [
            f"  {sid}  [on]  {'* * * * *':<15}  {'dedup_scan':<24}  last={last} (ok)"
        ]
