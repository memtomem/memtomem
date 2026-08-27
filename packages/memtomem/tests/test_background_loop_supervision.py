"""A supervisory loop that dies must say so (#2185).

``asyncio.create_task`` swallows the exception that ends a loop coroutine until
someone awaits the task — and nobody awaits these. Before the fix the only trace
of a dead watchdog or scheduler was ``get_status()["running"]`` flipping to
``False``, which nothing polls.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from memtomem.config import ConsolidationScheduleConfig, HealthWatchdogConfig, PolicyConfig
from memtomem.server.background import bg_task_error_cb, loop_task_error_cb, track_task
from memtomem.server.scheduler import ConsolidationScheduler, PolicyScheduler


async def _settle() -> None:
    """Let done-callbacks (scheduled via ``call_soon``) actually run."""
    for _ in range(3):
        await asyncio.sleep(0)


class TestErrorCallbacks:
    @pytest.mark.asyncio
    async def test_loop_cb_logs_at_error(self, caplog):
        async def _boom():
            raise RuntimeError("loop died")

        task = asyncio.create_task(_boom(), name="unit-loop")
        task.add_done_callback(loop_task_error_cb)
        with caplog.at_level(logging.ERROR, logger="memtomem.server.background"):
            await asyncio.gather(task, return_exceptions=True)
            await _settle()
        assert "unit-loop" in caplog.text
        assert "loop died" in caplog.text

    @pytest.mark.asyncio
    async def test_loop_cb_silent_on_cancel(self, caplog):
        async def _forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever(), name="unit-loop")
        task.add_done_callback(loop_task_error_cb)
        task.cancel()
        with caplog.at_level(logging.DEBUG, logger="memtomem.server.background"):
            await asyncio.gather(task, return_exceptions=True)
            await _settle()
        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_track_task_holds_a_reference_and_logs(self, caplog):
        tasks: set[asyncio.Task] = set()

        async def _boom():
            raise RuntimeError("bg died")

        task = track_task(asyncio.create_task(_boom(), name="unit-bg"), tasks)
        assert task in tasks, "tracked task must be strongly referenced while in flight"
        with caplog.at_level(logging.WARNING, logger="memtomem.server.background"):
            await asyncio.gather(task, return_exceptions=True)
            await _settle()
        assert "unit-bg" in caplog.text
        assert tasks == set(), "finished task must be discarded, not leaked"

    @pytest.mark.asyncio
    async def test_bg_cb_logs_at_warning(self, caplog):
        async def _boom():
            raise RuntimeError("one-shot died")

        task = asyncio.create_task(_boom(), name="unit-oneshot")
        task.add_done_callback(bg_task_error_cb)
        with caplog.at_level(logging.WARNING, logger="memtomem.server.background"):
            await asyncio.gather(task, return_exceptions=True)
            await _settle()
        assert "one-shot died" in caplog.text


def _app() -> MagicMock:
    app = MagicMock()
    app.search_pipeline = MagicMock()
    return app


class TestLoopDeathIsLogged:
    """Each supervisory ``start()`` must attach the logging done-callback."""

    @pytest.mark.asyncio
    async def test_policy_scheduler(self, caplog):
        sched = PolicyScheduler(_app(), PolicyConfig(enabled=True))

        async def _boom():
            raise RuntimeError("policy loop died")

        sched._run_loop = _boom
        with caplog.at_level(logging.ERROR, logger="memtomem.server.background"):
            await sched.start()
            await asyncio.gather(sched._task, return_exceptions=True)
            await _settle()
        assert "memtomem-policy-scheduler" in caplog.text
        assert "policy loop died" in caplog.text
        await sched.stop()

    @pytest.mark.asyncio
    async def test_consolidation_scheduler(self, caplog):
        sched = ConsolidationScheduler(_app(), ConsolidationScheduleConfig(enabled=True))

        async def _boom():
            raise RuntimeError("consolidation loop died")

        sched._run_loop = _boom
        with caplog.at_level(logging.ERROR, logger="memtomem.server.background"):
            await sched.start()
            await asyncio.gather(sched._task, return_exceptions=True)
            await _settle()
        assert "memtomem-consolidation-scheduler" in caplog.text
        assert "consolidation loop died" in caplog.text
        await sched.stop()

    @pytest.mark.asyncio
    async def test_health_watchdog(self, caplog, tmp_path):
        from memtomem.server.health_watchdog import HealthWatchdog

        app = _app()
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "health.db"
        wd = HealthWatchdog(app, HealthWatchdogConfig(enabled=True))

        async def _boom():
            raise RuntimeError("watchdog loop died")

        wd._run_loop = _boom
        with caplog.at_level(logging.ERROR, logger="memtomem.server.background"):
            await wd.start()
            await asyncio.gather(wd._task, return_exceptions=True)
            await _settle()
        assert "memtomem-health-watchdog" in caplog.text
        assert "watchdog loop died" in caplog.text
        await wd.stop()


class TestPolicyLoopBodyGuard:
    @pytest.mark.asyncio
    async def test_error_outside_run_policies_does_not_kill_the_loop(self):
        """``_run_policies`` guards its own body; the loop must guard the call.

        The existing coverage patches ``run_all_enabled``, which
        ``_run_policies`` already catches. This raises from ``_run_policies``
        itself — the gap that ended the loop before the fix.
        """
        sched = PolicyScheduler(_app(), PolicyConfig(enabled=True, scheduler_interval_minutes=1e-5))

        calls = 0

        async def _boom():
            nonlocal calls
            calls += 1
            raise RuntimeError("boom outside the inner try")

        sched._run_policies = _boom

        await sched.start()
        try:
            for _ in range(40):
                await asyncio.sleep(0.05)
                if calls >= 2:
                    break
            assert not sched._task.done(), "loop died on an error outside _run_policies"
            assert calls >= 2, f"expected >= 2 ticks, got {calls}"
        finally:
            await sched.stop()
