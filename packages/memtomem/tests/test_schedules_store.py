"""Tests for ScheduleMixin (P2 cron Phase A storage layer)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class TestScheduleStore:
    @pytest.mark.asyncio
    async def test_insert_and_get(self, storage):
        sid = await storage.schedule_insert("0 3 * * *", "compaction")
        assert sid

        sched = await storage.schedule_get(sid)
        assert sched is not None
        assert sched["cron_expr"] == "0 3 * * *"
        assert sched["job_kind"] == "compaction"
        assert sched["enabled"] is True
        assert sched["params"] == {}
        assert sched["last_run_at"] is None

    @pytest.mark.asyncio
    async def test_list_all_orders_by_created(self, storage):
        await storage.schedule_insert("0 1 * * *", "importance_decay")
        await storage.schedule_insert("0 2 * * *", "compaction")
        rows = await storage.schedule_list_all()
        assert [r["job_kind"] for r in rows] == ["importance_decay", "compaction"]

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, storage):
        assert await storage.schedule_get("no-such-id") is None

    @pytest.mark.asyncio
    async def test_delete(self, storage):
        sid = await storage.schedule_insert("0 0 * * *", "compaction")
        assert await storage.schedule_delete(sid) is True
        assert await storage.schedule_get(sid) is None

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, storage):
        assert await storage.schedule_delete("ghost") is False

    @pytest.mark.asyncio
    async def test_set_enabled_filters_list_due(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        # Disable before checking due — should be omitted entirely.
        await storage.schedule_set_enabled(sid, False)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        due = await storage.schedule_list_due(future)
        assert due == []

    @pytest.mark.asyncio
    async def test_mark_run_records_status(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok")
        sched = await storage.schedule_get(sid)
        assert sched["last_run_status"] == "ok"
        assert sched["last_run_at"] is not None

        await storage.schedule_mark_run(sid, "error", error="boom")
        sched = await storage.schedule_get(sid)
        assert sched["last_run_status"] == "error"
        assert sched["last_run_error"] == "boom"

    @pytest.mark.asyncio
    async def test_list_due_returns_only_due(self, storage):
        # Hourly schedule. Due check just before the next top-of-hour after
        # creation should be empty; well past it should include the row.
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        sched = await storage.schedule_get(sid)
        created = datetime.fromisoformat(sched["created_at"])

        # Anchor "early" to the next cron slot rather than ``created + 30s``
        # — the latter flakes for ~50 seconds every hour when ``created``
        # lands close to a top-of-hour and ``early`` crosses it (CI run
        # 25113291250 hit this at 13:59:48Z). 1 second before the next
        # slot is always strictly before it, regardless of where in the
        # hour creation happened.
        next_slot = created.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        early = next_slot - timedelta(seconds=1)
        assert await storage.schedule_list_due(early) == []

        # Well past the next top-of-hour → due
        late = created + timedelta(hours=2)
        due = await storage.schedule_list_due(late)
        assert len(due) == 1
        assert due[0]["id"] == sid

    @pytest.mark.asyncio
    async def test_list_due_at_most_once_catchup(self, storage):
        """If 3 cron slots elapsed, schedule fires once — not 3 times."""
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        # Simulate 3 hours elapsed: list_due returns the row exactly once
        # (it's a list, not a multiplied list). The dispatcher then calls
        # mark_run, which advances last_run_at, so subsequent ticks do
        # not re-fire for the missed slots.
        sched = await storage.schedule_get(sid)
        created = datetime.fromisoformat(sched["created_at"])
        far_future = created + timedelta(hours=3, minutes=30)

        due_first = await storage.schedule_list_due(far_future)
        assert len(due_first) == 1

        # Dispatcher would mark_run after firing — emulate that.
        await storage.schedule_mark_run(sid, "ok", when=far_future)

        # Second pass at the same `now`: last_run_at is now `far_future`,
        # so the next slot (4h after creation) is in the future relative
        # to `far_future` — schedule no longer due.
        due_second = await storage.schedule_list_due(far_future)
        assert due_second == []

    @pytest.mark.asyncio
    async def test_list_due_uses_utc(self, storage):
        """Naive `now` is treated as UTC (Phase A invariant)."""
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        sched = await storage.schedule_get(sid)
        created = datetime.fromisoformat(sched["created_at"])
        # Strip tz; mixin should re-attach UTC.
        naive_late = (created + timedelta(hours=2)).replace(tzinfo=None)
        due = await storage.schedule_list_due(naive_late)
        assert len(due) == 1 and due[0]["id"] == sid

    @pytest.mark.asyncio
    async def test_invalid_cron_in_db_skipped_not_raised(self, storage):
        """A malformed cron row must not crash the dispatcher path."""
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        # Corrupt the cron_expr directly to simulate a bad migration.
        db = storage._get_db()
        db.execute(
            "UPDATE schedules SET cron_expr=? WHERE id=?",
            ("not-a-cron", sid),
        )
        db.commit()
        # Should not raise.
        due = await storage.schedule_list_due(datetime.now(timezone.utc))
        assert due == []


class TestScheduleClaim:
    """Atomic run-claim CAS on ``last_run_at`` (issue #1564)."""

    @pytest.mark.asyncio
    async def test_claim_first_run_null_token_then_second_fails(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        # First run: last_run_at is NULL, so the CAS token is None.
        assert await storage.schedule_try_claim(sid, None) is True
        sched = await storage.schedule_get(sid)
        assert sched["last_run_at"] is not None
        assert sched["last_run_status"] == "running"
        # A second dispatcher reading the same (stale) NULL token loses.
        assert await storage.schedule_try_claim(sid, None) is False

    @pytest.mark.asyncio
    async def test_claim_clears_stale_error(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "error", error="boom")
        sched = await storage.schedule_get(sid)
        token = sched["last_run_at"]
        assert await storage.schedule_try_claim(sid, token) is True
        sched = await storage.schedule_get(sid)
        assert sched["last_run_status"] == "running"
        assert sched["last_run_error"] is None

    @pytest.mark.asyncio
    async def test_claim_with_nonnull_token_then_second_fails(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await storage.schedule_mark_run(sid, "ok", when=when)
        token = when.isoformat(timespec="seconds")
        assert await storage.schedule_try_claim(sid, token) is True
        # Re-claiming with the now-stale token loses.
        assert await storage.schedule_try_claim(sid, token) is False

    @pytest.mark.asyncio
    async def test_claim_wrong_token_fails(self, storage):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        await storage.schedule_mark_run(sid, "ok")
        # A token that never matched what's stored must not win.
        assert await storage.schedule_try_claim(sid, "1999-01-01T00:00:00+00:00") is False

    @pytest.mark.asyncio
    async def test_claim_without_terminal_mark_blocks_refire(self, storage):
        """Crash-re-fire pin: a claim advances last_run_at so the same slot
        is no longer due even if the process dies before mark_run."""
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        sched = await storage.schedule_get(sid)
        created = datetime.fromisoformat(sched["created_at"])
        now = created + timedelta(hours=2)

        # Row is due at `now`.
        assert len(await storage.schedule_list_due(now)) == 1
        # Claim (simulating a crash before the terminal mark_run).
        assert await storage.schedule_try_claim(sid, sched["last_run_at"], when=now) is True
        # Same `now`: last_run_at advanced to `now`, next slot is in the
        # future — no re-fire.
        assert await storage.schedule_list_due(now) == []

    @pytest.mark.asyncio
    async def test_two_backends_only_one_claims(self, storage, tmp_path):
        """Two SqliteBackends on the same DB file racing to claim one row —
        exactly one wins (cross-process double-fire guard, in-process)."""
        from memtomem.storage.sqlite_backend import SqliteBackend

        sid = await storage.schedule_insert("* * * * *", "compaction")

        # Second backend pointed at the same DB file, matching the fixture's
        # embedding meta so initialize() doesn't trip the strict dim check.
        other = SqliteBackend(
            storage._config,
            dimension=storage._dimension,
            embedding_provider=storage._embedding_provider,
            embedding_model=storage._embedding_model,
            strict_dim_check=False,
        )
        await other.initialize()
        try:
            r1 = await storage.schedule_try_claim(sid, None)
            r2 = await other.schedule_try_claim(sid, None)
            assert [r1, r2].count(True) == 1
        finally:
            await other.close()
