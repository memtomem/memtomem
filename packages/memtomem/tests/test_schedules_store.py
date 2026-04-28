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
        # Hourly schedule. Created "now"; due check just before next hour
        # should be empty, just after should include it.
        sid = await storage.schedule_insert("0 * * * *", "compaction")
        sched = await storage.schedule_get(sid)
        created = datetime.fromisoformat(sched["created_at"])

        # 30s after creation → no slot yet
        early = created + timedelta(seconds=30)
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
