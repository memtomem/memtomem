"""Scheduled job results reach the schedule record (#2471).

Runners return a summary dict. Every path that runs a job (the watchdog
dispatcher, ``mem_schedule_run_now``, ``mm schedule run-now``) records it as
``last_run_result``, and a run that reports ``skipped_reason`` is recorded as
``skipped``. The result is stored with the ``last_run_at`` of the same write, so
a result an older binary's later run left untouched is never shown as current.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import sqlite_vec
from click.testing import CliRunner
from pydantic import BaseModel

from memtomem.cli import cli
from memtomem.config import HealthWatchdogConfig, Mem2MemConfig, SchedulerConfig
from memtomem.errors import StorageError
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.scheduler.jobs import JOB_KINDS, JobSpec, run_outcome
from memtomem.search.dedup import DedupScanner
from memtomem.storage.base import VisiblePool
from memtomem.storage.mixins.schedules import KEEP_STATE
from memtomem.server.health_watchdog import HealthWatchdog
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import create_tables

# The SQL an older binary (before ``last_run_result``) runs for claim and
# completion: whole-second stamps, and the result column is never touched.
_LEGACY_CLAIM_SQL = (
    "UPDATE schedules SET last_run_at=?, last_run_status='running', "
    "last_run_error=NULL WHERE id=? AND last_run_at IS ?"
)
_LEGACY_MARK_SQL = (
    "UPDATE schedules SET last_run_at=?, last_run_status=?, last_run_error=? WHERE id=?"
)


class _NoParams(BaseModel):
    pass


def _spec(result):
    async def runner(app):
        return result

    return JobSpec("compaction", "test", _NoParams, runner)


def _watchdog(storage, *, timeout: float = 5.0) -> HealthWatchdog:
    app = MagicMock()
    app.storage = storage
    app.config = Mem2MemConfig()
    return HealthWatchdog(
        app,
        HealthWatchdogConfig(enabled=True),
        SchedulerConfig(enabled=True, runner_timeout_seconds=timeout),
    )


def _jobs(registry):
    return patch.dict("memtomem.scheduler.jobs.JOB_KINDS", registry, clear=True)


# ── run_outcome ───────────────────────────────────────────────────────


class TestRunOutcome:
    def test_skipped_reason_is_skipped(self):
        result = {"candidates": 0, "skipped_reason": "dedup_scanner_not_initialized"}
        assert run_outcome(result) == ("skipped", result)

    def test_plain_result_is_ok(self):
        assert run_outcome({"chunks_deleted": 2}) == ("ok", {"chunks_deleted": 2})

    def test_empty_dict_is_recorded_not_dropped(self):
        assert run_outcome({}) == ("ok", {})

    def test_empty_skipped_reason_is_ok(self):
        assert run_outcome({"skipped_reason": ""})[0] == "ok"

    @pytest.mark.parametrize("value", [None, [1, 2], "done"])
    def test_non_dict_records_no_result(self, value):
        assert run_outcome(value) == ("ok", None)


# ── Storage ───────────────────────────────────────────────────────────


class TestStoredResult:
    @pytest.mark.asyncio
    async def test_result_round_trips(self, storage):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        await storage.schedule_mark_run(sid, "ok", result={"candidates": 3, "pool": 10})
        sched = await storage.schedule_get(sid)
        assert sched["last_run_result"] == {"candidates": 3, "pool": 10}
        listed = {r["id"]: r for r in await storage.schedule_list_all()}
        assert listed[sid]["last_run_result"] == {"candidates": 3, "pool": 10}

    @pytest.mark.asyncio
    async def test_due_rows_carry_the_result(self, storage):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await storage.schedule_mark_run(sid, "ok", when=when, result={"candidates": 1})
        due = await storage.schedule_list_due(when + timedelta(minutes=5))
        assert [d["last_run_result"] for d in due] == [{"candidates": 1}]

    @pytest.mark.asyncio
    async def test_empty_result_differs_from_none(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok", result={})
        assert (await storage.schedule_get(sid))["last_run_result"] == {}
        await storage.schedule_mark_run(sid, "error", error="boom")
        assert (await storage.schedule_get(sid))["last_run_result"] is None

    @pytest.mark.asyncio
    async def test_claim_clears_the_previous_result(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok", result={"chunks_deleted": 1})
        token = (await storage.schedule_get(sid))["last_run_at"]
        assert await storage.schedule_try_claim(sid, token) is True
        raw = (
            storage._get_db()
            .execute("SELECT last_run_result FROM schedules WHERE id=?", (sid,))
            .fetchone()[0]
        )
        assert raw is None

    @pytest.mark.asyncio
    async def test_lost_claim_keeps_the_winners_result(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        stale = (await storage.schedule_get(sid))["last_run_at"]
        assert await storage.schedule_try_claim(sid, stale) is True
        await storage.schedule_mark_run(sid, "ok", result={"chunks_deleted": 4})
        assert await storage.schedule_try_claim(sid, stale) is False
        assert (await storage.schedule_get(sid))["last_run_result"] == {"chunks_deleted": 4}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw",
        ["{not json", "[1, 2]", '{"run_at": "{at}"}', '{"run_at": "{at}", "result": [1]}'],
        ids=["unparseable", "non-object", "no-result", "non-object-result"],
    )
    async def test_malformed_payload_reads_as_none(self, storage, raw):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok")
        at = (await storage.schedule_get(sid))["last_run_at"]
        # The envelope carries the row's real stamp, so only the payload's shape
        # can make it unreadable; a mismatched stamp would hide it regardless.
        db = storage._get_db()
        db.execute(
            "UPDATE schedules SET last_run_result=? WHERE id=?", (raw.replace("{at}", at), sid)
        )
        db.commit()
        sched = await storage.schedule_get(sid)
        assert sched["last_run_result"] is None
        assert sched["last_run_status"] == "ok"

    @pytest.mark.asyncio
    async def test_new_stamps_are_microsecond_even_when_zero(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        when = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
        await storage.schedule_mark_run(sid, "ok", when=when, result={})
        assert (await storage.schedule_get(sid))["last_run_at"] == (
            "2026-01-01T06:00:00.000000+00:00"
        )


class TestLegacyWriterHidesStaleResult:
    """An older binary advances ``last_run_at`` without replacing the result."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("microsecond", [0, 250_000], ids=["zero-us", "nonzero-us"])
    async def test_same_second_legacy_completion(self, storage, microsecond):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        when = datetime(2026, 1, 1, 6, 0, 0, microsecond, tzinfo=timezone.utc)
        await storage.schedule_mark_run(sid, "ok", when=when, result={"chunks_deleted": 9})
        db = storage._get_db()
        db.execute(_LEGACY_MARK_SQL, (when.isoformat(timespec="seconds"), "error", "legacy", sid))
        db.commit()
        sched = await storage.schedule_get(sid)
        assert sched["last_run_status"] == "error"
        assert sched["last_run_result"] is None

    @pytest.mark.asyncio
    async def test_same_second_legacy_claim(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        when = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
        await storage.schedule_mark_run(sid, "ok", when=when, result={"chunks_deleted": 9})
        token = (await storage.schedule_get(sid))["last_run_at"]
        db = storage._get_db()
        cur = db.execute(_LEGACY_CLAIM_SQL, (when.isoformat(timespec="seconds"), sid, token))
        db.commit()
        assert cur.rowcount == 1
        sched = await storage.schedule_get(sid)
        assert sched["last_run_status"] == "running"
        assert sched["last_run_result"] is None

    @pytest.mark.asyncio
    async def test_legacy_seconds_token_claims_once(self, storage):
        """A row last written by an older binary still claims exactly once."""
        sid = await storage.schedule_insert("* * * * *", "compaction")
        legacy = "2026-01-01T06:00:00+00:00"
        db = storage._get_db()
        db.execute(_LEGACY_MARK_SQL, (legacy, "ok", None, sid))
        db.commit()
        assert (await storage.schedule_get(sid))["last_run_result"] is None
        assert await storage.schedule_try_claim(sid, legacy) is True
        assert await storage.schedule_try_claim(sid, legacy) is False


class TestCatchUpWithMicrosecondStamps:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("microsecond", [0, 500_000], ids=["zero-us", "nonzero-us"])
    async def test_no_refire_at_claim_time_and_due_next_slot(self, storage, microsecond):
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        created = datetime.fromisoformat((await storage.schedule_get(sid))["created_at"])
        claim_at = created.replace(minute=0, second=0, microsecond=microsecond) + timedelta(hours=2)
        assert await storage.schedule_try_claim(sid, None, when=claim_at) is True
        await storage.schedule_mark_run(sid, "ok", when=claim_at, result={})
        assert await storage.schedule_list_due(claim_at) == []
        next_slot = claim_at.replace(microsecond=0) + timedelta(hours=1)
        assert await storage.schedule_list_due(next_slot - timedelta(seconds=1)) == []
        assert [d["id"] for d in await storage.schedule_list_due(next_slot)] == [sid]


class TestResultDurability:
    @pytest.mark.asyncio
    async def test_mark_run_with_result_refuses_a_caller_transaction(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok", result={"kept": True})
        with pytest.raises(StorageError, match="schedule_mark_run.*transaction is active"):
            async with storage.transaction():
                await storage.schedule_mark_run(sid, "error", result={"kept": False})
        sched = await storage.schedule_get(sid)
        assert (sched["last_run_status"], sched["last_run_result"]) == ("ok", {"kept": True})

    @pytest.mark.asyncio
    async def test_commit_failure_rolls_back_status_and_result_together(self, storage, monkeypatch):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok", result={"run": 1})
        before = await storage.schedule_get(sid)

        def fail(db):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(storage, "_commit_if_standalone", fail)
        with pytest.raises(sqlite3.OperationalError):
            await storage.schedule_mark_run(sid, "error", error="x", result={"run": 2})
        monkeypatch.undo()

        after = await storage.schedule_get(sid)
        assert after == before


class TestMigration:
    def test_old_table_gains_the_column_and_reads_none(self):
        db = sqlite3.connect(":memory:")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute(
            """CREATE TABLE schedules (
                id TEXT PRIMARY KEY, cron_expr TEXT NOT NULL, job_kind TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, last_run_at TEXT, last_run_status TEXT,
                last_run_error TEXT)"""
        )
        db.execute(
            "INSERT INTO schedules (id, cron_expr, job_kind, created_at, last_run_at, "
            "last_run_status) VALUES ('s1', '* * * * *', 'compaction', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00', 'ok')"
        )
        meta = MetaManager(lambda: db)
        for _ in range(2):  # repeated init must stay idempotent
            create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
        columns = [r[1] for r in db.execute("PRAGMA table_info(schedules)")]
        assert columns.count("last_run_result") == 1
        row = db.execute("SELECT last_run_result FROM schedules WHERE id='s1'").fetchone()
        assert row == (None,)

    @pytest.mark.parametrize("has_result_column", [False, True])
    def test_old_table_gains_the_state_column_and_reads_none(self, has_result_column):
        # Both older shapes: before ``last_run_result``, and with it.
        db = sqlite3.connect(":memory:")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        result_column = ", last_run_result TEXT" if has_result_column else ""
        db.execute(
            "CREATE TABLE schedules ("
            "id TEXT PRIMARY KEY, cron_expr TEXT NOT NULL, job_kind TEXT NOT NULL, "
            "params_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL, last_run_at TEXT, last_run_status TEXT, "
            f"last_run_error TEXT{result_column})"
        )
        db.execute(
            "INSERT INTO schedules (id, cron_expr, job_kind, created_at) "
            "VALUES ('s1', '* * * * *', 'dedup_scan', '2026-01-01T00:00:00+00:00')"
        )
        meta = MetaManager(lambda: db)
        for _ in range(2):  # repeated init must stay idempotent
            create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
        columns = [r[1] for r in db.execute("PRAGMA table_info(schedules)")]
        assert columns.count("job_state") == 1
        assert db.execute("SELECT job_state FROM schedules WHERE id='s1'").fetchone() == (None,)


# ── Stored position (#3432) ───────────────────────────────────────────


def _raw_state(storage, sid):
    return (
        storage._get_db().execute("SELECT job_state FROM schedules WHERE id=?", (sid,)).fetchone()
    )


class TestStoredState:
    POSITION = {"after": "00000000-0000-0000-0000-0000000001f4"}

    @pytest.mark.asyncio
    async def test_a_new_schedule_has_none_on_every_read(self, storage):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        assert (await storage.schedule_get(sid))["state"] is None
        assert [r["state"] for r in await storage.schedule_list_all()] == [None]
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert [r["state"] for r in await storage.schedule_list_due(later)] == [None]
        assert _raw_state(storage, sid) == (None,)

    @pytest.mark.asyncio
    async def test_set_keep_and_clear(self, storage):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")

        await storage.schedule_mark_run(sid, "ok", result={}, state=self.POSITION)
        assert (await storage.schedule_get(sid))["state"] == self.POSITION
        assert [r["state"] for r in await storage.schedule_list_all()] == [self.POSITION]
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        assert [r["state"] for r in await storage.schedule_list_due(later)] == [self.POSITION]

        # No ``state`` argument: a run that did not finish, or a job with none.
        await storage.schedule_mark_run(sid, "error", error="boom")
        await storage.schedule_mark_run(sid, "ok", result={"x": 1})
        assert (await storage.schedule_get(sid))["state"] == self.POSITION

        await storage.schedule_mark_run(sid, "ok", result={}, state=None)
        assert (await storage.schedule_get(sid))["state"] is None
        assert _raw_state(storage, sid) == (None,)

    @pytest.mark.asyncio
    async def test_a_claim_leaves_it_alone_won_or_lost(self, storage):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        await storage.schedule_mark_run(sid, "ok", result={}, state=self.POSITION)
        token = (await storage.schedule_get(sid))["last_run_at"]

        assert await storage.schedule_try_claim(sid, token) is True
        claimed = await storage.schedule_get(sid)
        assert (claimed["last_run_status"], claimed["last_run_result"]) == ("running", None)
        assert claimed["state"] == self.POSITION

        assert await storage.schedule_try_claim(sid, token) is False
        assert (await storage.schedule_get(sid))["state"] == self.POSITION

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write", ["claim", "completion"])
    async def test_an_older_binary_run_leaves_it_readable(self, storage, write):
        """An older binary advances ``last_run_at`` and never names ``job_state``.

        The result stored before such a run stops being shown, because it no
        longer describes the latest run. The position is not a record of one
        run, so every read still returns it.

        Only a binary from before ``last_run_result`` leaves a result that is
        out of date next to a position. One that has the column runs the
        statements ``schedule_try_claim`` and a ``KEEP_STATE`` completion run.
        """
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        when = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
        await storage.schedule_mark_run(
            sid, "ok", when=when, result={"pool": 500}, state=self.POSITION
        )
        token = (await storage.schedule_get(sid))["last_run_at"]
        later = "2026-01-01T06:01:00+00:00"
        sql, args = {
            "claim": (_LEGACY_CLAIM_SQL, (later, sid, token)),
            "completion": (_LEGACY_MARK_SQL, (later, "ok", None, sid)),
        }[write]
        db = storage._get_db()
        cur = db.execute(sql, args)
        db.commit()
        assert cur.rowcount == 1

        sched = await storage.schedule_get(sid)
        assert (sched["last_run_at"], sched["last_run_result"]) == (later, None)
        assert sched["state"] == self.POSITION
        assert [r["state"] for r in await storage.schedule_list_all()] == [self.POSITION]
        due = await storage.schedule_list_due(datetime.now(timezone.utc))
        assert [r["state"] for r in due] == [self.POSITION]

    @pytest.mark.asyncio
    async def test_the_state_is_committed_with_the_outcome(self, components):
        storage = components.storage
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        await storage.schedule_mark_run(sid, "ok", result={}, state=self.POSITION)
        # Another connection sees it: the write did not stay in this one's cache.
        other = sqlite3.connect(components.config.storage.sqlite_path)
        try:
            row = other.execute(
                "SELECT last_run_status, job_state FROM schedules WHERE id=?", (sid,)
            ).fetchone()
        finally:
            other.close()
        assert row == ("ok", json.dumps(self.POSITION))

    @pytest.mark.asyncio
    async def test_commit_failure_rolls_back_the_state_with_the_outcome(self, storage, monkeypatch):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        await storage.schedule_mark_run(sid, "ok", result={"run": 1}, state=self.POSITION)
        before = await storage.schedule_get(sid)

        def fail(db):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(storage, "_commit_if_standalone", fail)
        for update in ({"after": "00000000-0000-0000-0000-0000000003e8"}, None):
            with pytest.raises(sqlite3.OperationalError):
                await storage.schedule_mark_run(sid, "ok", result={"run": 2}, state=update)
        monkeypatch.undo()

        assert await storage.schedule_get(sid) == before
        assert before["state"] == self.POSITION

    @pytest.mark.asyncio
    async def test_a_state_that_is_not_json_is_refused_before_any_write(self, storage):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        await storage.schedule_mark_run(sid, "ok", result={"run": 1}, state=self.POSITION)
        before = await storage.schedule_get(sid)
        for not_json in (object(), {"after": uuid4()}):
            with pytest.raises(TypeError):
                await storage.schedule_mark_run(sid, "ok", result={"run": 2}, state=not_json)
        assert await storage.schedule_get(sid) == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("{not json", "schedule {id} has an unreadable job_state; ignoring"),
            ("[1, 2]", "schedule {id} has a malformed job_state; ignoring"),
            ('"text"', "schedule {id} has a malformed job_state; ignoring"),
            ("null", "schedule {id} has a malformed job_state; ignoring"),
            # Nesting this deep raises RecursionError in the parser, whether
            # or not the brackets close and whatever the container is.
            ("[" * 10000, "schedule {id} has an unreadable job_state; ignoring"),
            (
                "[" * 10000 + "]" * 10000,
                "schedule {id} has an unreadable job_state; ignoring",
            ),
            (
                '{"a":' * 10000 + "1" + "}" * 10000,
                "schedule {id} has an unreadable job_state; ignoring",
            ),
        ],
        ids=[
            "unparseable",
            "list",
            "string",
            "json-null",
            "deep-unclosed",
            "deep-list",
            "deep-object",
        ],
    )
    async def test_a_stored_value_that_is_not_an_object_reads_as_none(
        self, storage, caplog, raw, message
    ):
        sid = await storage.schedule_insert("* * * * *", "dedup_scan")
        db = storage._get_db()
        db.execute("UPDATE schedules SET job_state=? WHERE id=?", (raw, sid))
        db.commit()
        with caplog.at_level(logging.WARNING, logger="memtomem.storage.mixins.schedules"):
            sched = await storage.schedule_get(sid)
        assert sched["state"] is None
        assert [r.getMessage() for r in caplog.records] == [message.replace("{id}", sid)]

    @pytest.mark.asyncio
    async def test_one_unreadable_state_does_not_hide_the_other_schedules(self, storage):
        # Every read decodes each row it returns. A row that raised would fail
        # ``schedule_list_due``, which the dispatcher logs and gives up on, so
        # no schedule in the store would run.
        bad = await storage.schedule_insert("* * * * *", "dedup_scan")
        good = await storage.schedule_insert("* * * * *", "dedup_scan")
        await storage.schedule_mark_run(good, "ok", result={}, state=self.POSITION)
        db = storage._get_db()
        db.execute("UPDATE schedules SET job_state=? WHERE id=?", ("[" * 10000, bad))
        db.commit()
        expected = {bad: None, good: self.POSITION}

        assert (await storage.schedule_get(bad))["state"] is None
        assert {r["id"]: r["state"] for r in await storage.schedule_list_all()} == expected
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        due = await storage.schedule_list_due(later)
        assert {r["id"]: r["state"] for r in due} == expected

    @pytest.mark.asyncio
    async def test_sql_null_reads_as_none_without_a_warning(self, storage, caplog):
        await storage.schedule_insert("* * * * *", "dedup_scan")
        with caplog.at_level(logging.WARNING, logger="memtomem.storage.mixins.schedules"):
            rows = await storage.schedule_list_all()
        assert [r["state"] for r in rows] == [None]
        assert caplog.records == []


# ── Dispatch paths ────────────────────────────────────────────────────


class TestWatchdogRecordsResult:
    @pytest.mark.asyncio
    async def test_skipped_reason_recorded(self, storage):
        result = {"candidates": 0, "skipped_reason": "dedup_scanner_not_initialized"}
        sid = await storage.schedule_insert("* * * * *", "compaction")
        sched = await storage.schedule_get(sid)
        with _jobs({"compaction": _spec(result)}):
            await _watchdog(storage)._run_schedule(sched, timeout=5.0)
        final = await storage.schedule_get(sid)
        assert final["last_run_status"] == "skipped"
        assert final["last_run_error"] is None
        assert final["last_run_result"] == result

    @pytest.mark.asyncio
    async def test_ok_result_recorded(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        sched = await storage.schedule_get(sid)
        with _jobs({"compaction": _spec({"chunks_deleted": 2})}):
            await _watchdog(storage)._run_schedule(sched, timeout=5.0)
        final = await storage.schedule_get(sid)
        assert (final["last_run_status"], final["last_run_result"]) == (
            "ok",
            {"chunks_deleted": 2},
        )

    @pytest.mark.asyncio
    async def test_error_and_timeout_keep_status_and_text(self, storage):
        async def boom(app):
            raise RuntimeError("boom")

        async def slow(app):
            await asyncio.sleep(1.0)
            return {"never": True}

        for runner, status, error in (
            (boom, "error", "boom"),
            (slow, "timeout", "exceeded 0.05s"),
        ):
            sid = await storage.schedule_insert("* * * * *", "compaction")
            await storage.schedule_mark_run(sid, "ok", result={"previous": True})
            sched = await storage.schedule_get(sid)
            spec = JobSpec("compaction", "t", _NoParams, runner)
            with _jobs({"compaction": spec}):
                await _watchdog(storage, timeout=0.05)._run_schedule(sched, timeout=0.05)
            final = await storage.schedule_get(sid)
            assert final["last_run_status"] == status
            assert final["last_run_error"] == error
            assert final["last_run_result"] is None


class TestRunNowRecordsResult:
    RESULT = {"orphan_files": 12, "chunks_deleted": 0, "skipped_reason": "orphan_ratio_exceeded"}

    @pytest.mark.asyncio
    async def test_mcp_run_now(self, components, monkeypatch):
        from memtomem.server.context import AppContext
        from memtomem.server.tools import schedule as schedule_tools

        app = AppContext.from_components(components)
        monkeypatch.setattr(schedule_tools, "_get_app_initialized", AsyncMock(return_value=app))
        sid = await components.storage.schedule_insert("* * * * *", "compaction")
        with _jobs({"compaction": _spec(self.RESULT)}):
            out = json.loads(await schedule_tools.mem_schedule_run_now(sid))
        assert out == {"ok": True, "reason": "ran", "result": self.RESULT}
        sched = await components.storage.schedule_get(sid)
        assert (sched["last_run_status"], sched["last_run_result"]) == ("skipped", self.RESULT)

    def test_cli_run_now_and_list(self, components, monkeypatch):
        @asynccontextmanager
        async def fake():
            yield components

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        runner = CliRunner()
        sid = asyncio.run(components.storage.schedule_insert("* * * * *", "compaction"))
        with _jobs({"compaction": _spec(self.RESULT)}):
            ran = runner.invoke(cli, ["schedule", "run-now", sid])
        assert ran.exit_code == 0, ran.output

        listed = runner.invoke(cli, ["schedule", "list"])
        assert listed.exit_code == 0, listed.output
        line = next(line for line in listed.output.splitlines() if sid in line)
        assert line.endswith("(skipped: orphan_ratio_exceeded)")

        rows = json.loads(runner.invoke(cli, ["schedule", "list", "--json"]).stdout)
        row = next(r for r in rows if r["id"] == sid)
        assert (row["last_run_status"], row["last_run_result"]) == ("skipped", self.RESULT)


# ── Dedup job coverage ────────────────────────────────────────────────


def _chunk(content: str) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/s.md")),
        embedding=[],
        created_at=datetime.now(timezone.utc),
    )


class TestDedupJobCoverage:
    @pytest.mark.asyncio
    async def test_result_reports_pool_chunks_without_a_vector(self):
        a, b, pending = _chunk("alpha"), _chunk("alpha, again"), _chunk("not vectorised")
        storage = MagicMock()
        storage.dense_enabled = True
        storage.is_source_held = AsyncMock(return_value=False)
        # Three of four scannable chunks: the result reports both numbers, and
        # the one the job did not look at.
        storage.list_visible_chunks_by_id = AsyncMock(
            return_value=VisiblePool(chunks=[a, b, pending], visible=4, above_cursor=4)
        )
        storage.get_embeddings_for_chunks = AsyncMock(
            return_value={str(a.id): [0.0], str(b.id): [1.0]}
        )

        async def dense_search(embedding, top_k, project_context_root=None):
            other = b if embedding == [0.0] else a
            return [SearchResult(chunk=other, score=0.97, rank=1, source="dense")]

        storage.dense_search = dense_search
        app = MagicMock()
        app.dedup_scanner = DedupScanner(storage)

        spec = JOB_KINDS["dedup_scan"]
        result, state = await spec.runner(app, **spec.params_model().model_dump())

        # No stored position: the run scans the first pool.
        storage.list_visible_chunks_by_id.assert_awaited_once_with(500, None)
        assert state == {"after": str(pending.id)}
        assert result == {
            "candidates": 1,
            "pairs_found": 1,
            "threshold": 0.92,
            "pool": 3,
            "eligible": 4,
            "remaining": 1,
            "probed": 2,
            "without_vector": 1,
            "near_search_enabled": True,
            "same_source_read_only_pairs": 0,
            "after": None,
            "next_after": str(pending.id),
        }

    @pytest.mark.asyncio
    async def test_result_reports_the_pairs_the_limit_cut(self):
        # ``candidates`` is capped by ``limit``; ``pairs_found`` is not, so a
        # run's record shows that its list was cut.
        copies = [_chunk("one body") for _ in range(3)]
        storage = MagicMock()
        storage.dense_enabled = False
        storage.list_visible_chunks_by_id = AsyncMock(
            return_value=VisiblePool(chunks=copies, visible=3, above_cursor=3)
        )
        app = MagicMock()
        app.dedup_scanner = DedupScanner(storage)

        spec = JOB_KINDS["dedup_scan"]
        result, _ = await spec.runner(app, **spec.params_model(limit=1).model_dump())

        assert (result["candidates"], result["pairs_found"]) == (1, 2)
        assert result["remaining"] == 0

    @pytest.mark.asyncio
    async def test_missing_scanner_is_skipped(self):
        app = MagicMock()
        app.dedup_scanner = None
        spec = JOB_KINDS["dedup_scan"]
        result, state = await spec.runner(
            app, state={"after": "kept"}, **spec.params_model().model_dump()
        )
        assert run_outcome(result) == (
            "skipped",
            {"candidates": 0, "skipped_reason": "dedup_scanner_not_initialized"},
        )
        # Nothing was read: the position is neither moved nor cleared.
        assert state is KEEP_STATE
