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
from memtomem.server.background import (
    bg_task_error_cb,
    loop_task_error_cb,
    stop_loop_task,
    track_task,
)
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


class TestStopLoopTaskCancellation:
    """``stop_loop_task`` must tell the child's cancellation from the caller's (#2213).

    Swallowing both is the same shape the ``suppress(CancelledError)`` blocks it
    replaced had: a shutdown cancelled mid-teardown returned normally and kept
    working through the rest of ``AppContext.close``'s ordering.
    """

    @staticmethod
    def _slow_cleanup_loop(
        in_cleanup: asyncio.Event, released: asyncio.Event
    ) -> asyncio.Task[None]:
        """A loop whose ``CancelledError`` cleanup outlives the cancel request."""

        async def _loop() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                in_cleanup.set()
                await released.wait()
                raise

        return asyncio.create_task(_loop(), name="unit-loop")

    @pytest.mark.asyncio
    async def test_child_cancellation_is_swallowed(self):
        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever(), name="unit-loop")
        await _settle()
        await stop_loop_task(task)  # must not raise: this cancel was ours to make
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_already_dead_loop_is_tolerated(self, caplog):
        async def _boom() -> None:
            raise RuntimeError("loop died")

        task = asyncio.create_task(_boom(), name="unit-loop")
        task.add_done_callback(loop_task_error_cb)
        await asyncio.gather(task, return_exceptions=True)
        with caplog.at_level(logging.DEBUG, logger="memtomem.server.background"):
            await stop_loop_task(task)
        assert "had already died" in caplog.text

    @pytest.mark.asyncio
    async def test_caller_cancellation_propagates_after_the_child_settles(self):
        in_cleanup, released = asyncio.Event(), asyncio.Event()
        loop = self._slow_cleanup_loop(in_cleanup, released)
        stopper = asyncio.create_task(stop_loop_task(loop))
        await in_cleanup.wait()

        stopper.cancel()
        await _settle()
        assert not stopper.done(), "stopper must keep settling the child, not bail out early"

        released.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert stopper.cancelled(), "a cancel aimed at the stopper must not be swallowed"
        assert loop.done(), "the child must be settled before the cancellation propagates"

    @pytest.mark.asyncio
    async def test_cancellation_requested_before_entry_still_propagates(self):
        """The cancel may already be pending when ``stop_loop_task`` is entered.

        A ``cancelling()`` baseline taken at entry already counts that request,
        so the delivered ``CancelledError`` would read as the child's and be
        swallowed — which is why the child's outcome is normalized to a value
        instead.
        """
        outcome: list[str] = []

        async def _stopper() -> None:
            child = asyncio.create_task(asyncio.sleep(3600), name="unit-loop")
            await _settle()
            current = asyncio.current_task()
            assert current is not None
            current.cancel("pending")  # requested, not yet delivered
            try:
                await stop_loop_task(child)
                outcome.append("returned")
            except asyncio.CancelledError:
                outcome.append("propagated")
                raise

        stopper = asyncio.create_task(_stopper())
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert outcome == ["propagated"], "a cancel pending at entry must not be mistaken for ours"

    @pytest.mark.asyncio
    async def test_first_cancel_message_survives_repeated_cancellation(self):
        in_cleanup, released = asyncio.Event(), asyncio.Event()
        loop = self._slow_cleanup_loop(in_cleanup, released)
        stopper = asyncio.create_task(stop_loop_task(loop))
        await in_cleanup.wait()

        stopper.cancel("shutdown")
        await _settle()
        stopper.cancel("later")
        await _settle()
        assert not stopper.done()

        released.set()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await stopper
        assert excinfo.value.args == ("shutdown",), "the first cancellation's message must win"


class TestStopCleanupSurvivesCallerCancellation:
    """A propagating cancellation must not strand each ``stop()``'s own cleanup (#2213).

    ``_stop_quietly`` can defer the exception *between* resources, but it cannot
    resume the half-finished cleanup inside one — so each ``stop()`` finishes
    its own before letting the cancellation travel on.
    """

    @staticmethod
    async def _cancel_during_stop(stop, in_cleanup: asyncio.Event) -> asyncio.Task[None]:
        stopper = asyncio.create_task(stop())
        await in_cleanup.wait()
        stopper.cancel()
        await _settle()
        return stopper

    @pytest.mark.asyncio
    async def test_scheduler_clears_its_task_handle(self):
        in_cleanup, released = asyncio.Event(), asyncio.Event()
        sched = PolicyScheduler(_app(), PolicyConfig(enabled=True))

        async def _loop() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                in_cleanup.set()
                await released.wait()
                raise

        sched._run_loop = _loop
        await sched.start()
        stopper = await self._cancel_during_stop(sched.stop, in_cleanup)
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert sched._task is None, "a cancelled stop must still drop the dead task handle"

    @pytest.mark.asyncio
    async def test_health_watchdog_still_closes_its_store(self, tmp_path):
        from memtomem.server.health_watchdog import HealthWatchdog

        in_cleanup, released = asyncio.Event(), asyncio.Event()
        app = _app()
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "health.db"
        wd = HealthWatchdog(app, HealthWatchdogConfig(enabled=True))

        async def _loop() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                in_cleanup.set()
                await released.wait()
                raise

        wd._run_loop = _loop
        await wd.start()
        store = wd._store
        assert store is not None
        store.close = MagicMock(wraps=store.close)

        stopper = await self._cancel_during_stop(wd.stop, in_cleanup)
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        store.close.assert_called_once()
        assert wd._task is None and wd._store is None

    @pytest.mark.asyncio
    async def test_health_watchdog_store_close_failure_keeps_the_cancellation(
        self, tmp_path, caplog
    ):
        """A failing store close must not *replace* the propagating cancellation.

        ``_stop_quietly`` swallows ordinary exceptions and re-raises only
        ``CancelledError``, so an exception escaping this ``finally`` would turn
        a cancelled shutdown back into one that looks orderly.
        """
        from memtomem.server.health_watchdog import HealthWatchdog

        in_cleanup, released = asyncio.Event(), asyncio.Event()
        app = _app()
        app.config = MagicMock()
        app.config.storage.sqlite_path = tmp_path / "health.db"
        wd = HealthWatchdog(app, HealthWatchdogConfig(enabled=True))

        async def _loop() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                in_cleanup.set()
                await released.wait()
                raise

        wd._run_loop = _loop
        await wd.start()
        assert wd._store is not None
        wd._store.close = MagicMock(side_effect=RuntimeError("close failed"))

        stopper = await self._cancel_during_stop(wd.stop, in_cleanup)
        released.set()
        with caplog.at_level(logging.WARNING, logger="memtomem.server.health_watchdog"):
            with pytest.raises(asyncio.CancelledError):
                await stopper
        assert stopper.cancelled(), "the close failure must not mask the cancellation"
        assert "Health store close failed" in caplog.text


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
