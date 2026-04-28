"""Tests for HealthWatchdog._dispatch_schedules (P2 cron Phase A.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from memtomem.config import HealthWatchdogConfig, Mem2MemConfig, SchedulerConfig
from memtomem.scheduler.jobs import JobSpec
from memtomem.server.health_watchdog import HealthWatchdog


class _NoParams(BaseModel):
    pass


class _FakeApp:
    """Bare AppContext stand-in: only `.storage` is needed by the dispatcher."""

    def __init__(self, storage) -> None:
        self.storage = storage
        self.config = Mem2MemConfig()


def _make_watchdog(
    storage,
    *,
    scheduler_enabled: bool = True,
    max_concurrent: int = 1,
    timeout: float = 5.0,
) -> HealthWatchdog:
    app = _FakeApp(storage)
    wd_cfg = HealthWatchdogConfig(enabled=True)
    sch_cfg = SchedulerConfig(
        enabled=scheduler_enabled,
        max_concurrent_jobs=max_concurrent,
        runner_timeout_seconds=timeout,
    )
    return HealthWatchdog(app, wd_cfg, sch_cfg)


@pytest.fixture
def patch_jobs():
    """Replace JOB_KINDS with a controllable registry for the test scope."""

    def _apply(registry: dict[str, JobSpec]):
        return patch.dict(
            "memtomem.scheduler.jobs.JOB_KINDS",
            registry,
            clear=True,
        )

    return _apply


# ── Test cases ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_due_fires_once_status_ok(storage, patch_jobs):
    calls: list[str] = []

    async def runner(app):
        calls.append("ran")
        return {"ok": True}

    spec = JobSpec("compaction", "test", _NoParams, runner)
    sid = await storage.schedule_insert("* * * * *", "compaction")

    # Backdate created_at so the next cron slot has elapsed
    db = storage._get_db()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db.execute("UPDATE schedules SET created_at=? WHERE id=?", (past, sid))
    db.commit()

    wd = _make_watchdog(storage)
    with patch_jobs({"compaction": spec}):
        await wd._dispatch_schedules()

    assert calls == ["ran"]
    sched = await storage.schedule_get(sid)
    assert sched["last_run_status"] == "ok"
    assert sched["last_run_error"] is None


@pytest.mark.asyncio
async def test_two_due_serialized_with_concurrency_one(storage, patch_jobs):
    """max_concurrent_jobs=1 forces serial execution (B starts after A finishes)."""
    in_flight = 0
    max_seen = 0

    async def runner(app):
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return {}

    spec = JobSpec("compaction", "t", _NoParams, runner)

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db = storage._get_db()
    for _ in range(2):
        sid = await storage.schedule_insert("* * * * *", "compaction")
        db.execute("UPDATE schedules SET created_at=? WHERE id=?", (past, sid))
    db.commit()

    wd = _make_watchdog(storage, max_concurrent=1)
    with patch_jobs({"compaction": spec}):
        await wd._dispatch_schedules()

    assert max_seen == 1


@pytest.mark.asyncio
async def test_runner_raises_marks_error(storage, patch_jobs):
    async def runner(app):
        raise RuntimeError("boom")

    spec = JobSpec("compaction", "t", _NoParams, runner)
    sid = await storage.schedule_insert("* * * * *", "compaction")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db = storage._get_db()
    db.execute("UPDATE schedules SET created_at=? WHERE id=?", (past, sid))
    db.commit()

    wd = _make_watchdog(storage)
    with patch_jobs({"compaction": spec}):
        await wd._dispatch_schedules()  # must not propagate

    sched = await storage.schedule_get(sid)
    assert sched["last_run_status"] == "error"
    assert "boom" in (sched["last_run_error"] or "")


@pytest.mark.asyncio
async def test_runner_timeout_marks_timeout(storage, patch_jobs):
    async def runner(app):
        await asyncio.sleep(1.0)

    spec = JobSpec("compaction", "t", _NoParams, runner)
    sid = await storage.schedule_insert("* * * * *", "compaction")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db = storage._get_db()
    db.execute("UPDATE schedules SET created_at=? WHERE id=?", (past, sid))
    db.commit()

    wd = _make_watchdog(storage, timeout=0.05)
    with patch_jobs({"compaction": spec}):
        await wd._dispatch_schedules()

    sched = await storage.schedule_get(sid)
    assert sched["last_run_status"] == "timeout"


@pytest.mark.asyncio
async def test_dispatch_noop_when_scheduler_disabled(storage, patch_jobs):
    calls: list[str] = []

    async def runner(app):
        calls.append("ran")

    spec = JobSpec("compaction", "t", _NoParams, runner)
    sid = await storage.schedule_insert("* * * * *", "compaction")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db = storage._get_db()
    db.execute("UPDATE schedules SET created_at=? WHERE id=?", (past, sid))
    db.commit()

    wd = _make_watchdog(storage, scheduler_enabled=False)
    with patch_jobs({"compaction": spec}):
        await wd._dispatch_schedules()

    assert calls == []
    sched = await storage.schedule_get(sid)
    assert sched["last_run_status"] is None


@pytest.mark.asyncio
async def test_unknown_job_kind_marks_error(storage, patch_jobs):
    sid = await storage.schedule_insert("* * * * *", "ghost_kind")
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    db = storage._get_db()
    db.execute("UPDATE schedules SET created_at=? WHERE id=?", (past, sid))
    db.commit()

    wd = _make_watchdog(storage)
    with patch_jobs({}):
        await wd._dispatch_schedules()

    sched = await storage.schedule_get(sid)
    assert sched["last_run_status"] == "error"
    assert "unknown job_kind" in (sched["last_run_error"] or "")


# ── Config + warning tests ────────────────────────────────────────────


class TestSchedulerConfig:
    def test_defaults(self):
        c = SchedulerConfig()
        assert c.enabled is False
        assert c.max_concurrent_jobs == 1
        assert c.default_timezone == "utc"
        assert c.runner_timeout_seconds == 300.0

    def test_max_concurrent_must_be_positive(self):
        with pytest.raises(ValueError):
            SchedulerConfig(max_concurrent_jobs=0)

    def test_runner_timeout_must_be_positive(self):
        with pytest.raises(ValueError):
            SchedulerConfig(runner_timeout_seconds=0)

    def test_in_mem2mem_root(self):
        cfg = Mem2MemConfig()
        assert hasattr(cfg, "scheduler")
        assert cfg.scheduler.enabled is False


class TestStatusMismatchWarning:
    @pytest.mark.asyncio
    async def test_status_surfaces_scheduler_watchdog_mismatch(self, storage, monkeypatch):
        """mem_status surfaces the silent-footgun warning."""
        from memtomem.server.tools.status_config import format_status_report

        class _App:
            def __init__(self, st):
                self.storage = st
                self.config = Mem2MemConfig()
                self.config.scheduler.enabled = True
                self.config.health_watchdog.enabled = False

        out = await format_status_report(_App(storage))
        assert "scheduler_watchdog_disabled" in out
