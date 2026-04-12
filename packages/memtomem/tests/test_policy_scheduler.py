"""Tests for the PolicyScheduler background loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem.config import PolicyConfig
from memtomem.server.scheduler import PolicyScheduler
from memtomem.tools.policy_engine import PolicyRunResult


def _make_app() -> MagicMock:
    app = MagicMock()
    app.storage = AsyncMock()
    app.search_pipeline = MagicMock()
    return app


def _result(name: str = "p1", affected: int = 0) -> PolicyRunResult:
    return PolicyRunResult(
        policy_name=name,
        policy_type="auto_archive",
        affected_count=affected,
        dry_run=False,
        details=f"{affected} chunks archived",
    )


class TestPolicyScheduler:
    def test_start_stop(self):
        app = _make_app()
        config = PolicyConfig(enabled=True, scheduler_interval_minutes=1.0)
        sched = PolicyScheduler(app, config)

        async def _go():
            await sched.start()
            assert sched._task is not None
            assert not sched._task.done()
            await sched.stop()
            assert sched._task is None

        asyncio.run(_go())

    def test_start_disabled(self):
        app = _make_app()
        config = PolicyConfig(enabled=False)
        sched = PolicyScheduler(app, config)

        async def _go():
            await sched.start()
            assert sched._task is None

        asyncio.run(_go())

    @pytest.mark.asyncio
    async def test_run_policies_calls_engine(self):
        app = _make_app()
        config = PolicyConfig(enabled=True, max_actions_per_run=100)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            return_value=[_result("p1", 0)],
        ) as mock_run:
            await sched._run_policies()
            mock_run.assert_called_once_with(
                app.storage, dry_run=False, max_actions=100
            )

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_affected(self):
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            return_value=[_result("p1", 5)],
        ):
            await sched._run_policies()
            app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_not_invalidated_when_no_changes(self):
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            return_value=[_result("p1", 0)],
        ):
            await sched._run_policies()
            app.search_pipeline.invalidate_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_does_not_crash_loop(self):
        app = _make_app()
        config = PolicyConfig(enabled=True, scheduler_interval_minutes=0.001)
        sched = PolicyScheduler(app, config)

        call_count = 0

        async def _failing(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        with patch("memtomem.tools.policy_engine.run_all_enabled", side_effect=_failing):
            await sched.start()
            # Wait long enough for at least 2 ticks
            await asyncio.sleep(0.2)
            assert not sched._task.done(), "loop crashed after error"
            assert call_count >= 2, f"expected >= 2 calls, got {call_count}"
            await sched.stop()

    @pytest.mark.asyncio
    async def test_consecutive_failure_counter(self):
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            for _ in range(3):
                await sched._run_policies()
            assert sched._consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_failure_counter_resets_on_success(self):
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        # Fail twice
        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            await sched._run_policies()
            await sched._run_policies()
        assert sched._consecutive_failures == 2

        # Succeed
        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            return_value=[_result("p1", 1)],
        ):
            await sched._run_policies()
        assert sched._consecutive_failures == 0
