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


def _result(name: str = "p1", affected: int = 0, mutated: bool | None = None) -> PolicyRunResult:
    # ``mutated`` mirrors what ``run_policy`` derives for a non-dry-run result
    # unless a test pins it explicitly (#2157).
    return PolicyRunResult(
        policy_name=name,
        policy_type="auto_archive",
        affected_count=affected,
        dry_run=False,
        details=f"{affected} chunks archived",
        mutated=affected > 0 if mutated is None else mutated,
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
                app.storage,
                dry_run=False,
                max_actions=100,
                llm_provider=app.llm_provider,
                # ``indexing.extract_entities`` reaches the consolidation
                # summary's entity write through the same forwarding (#2155).
                extract_entities=app.config.indexing.extract_entities,
                # Unattended runs are labelled so the maintenance run log can
                # tell them apart from manual mem_policy_run calls (#2132).
                source="scheduler",
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
    async def test_cache_invalidated_when_mutated_without_affected_count(self):
        """The #2157 shape: auto_consolidate deleted a stale summary chunk and
        then failed to regenerate it — the group is reported as failed with
        ``affected_count == 0``, but the delete is committed."""
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            return_value=[_result("p1", 0, mutated=True)],
        ):
            await sched._run_policies()
            app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_run_raises(self):
        """A raising run may have partly landed (``run_policy`` re-raises after
        partial handler writes, and earlier policies in the loop are already
        committed), so the exception path must invalidate too (#2157)."""
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            await sched._run_policies()
            app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_invalidated_when_run_is_cancelled(self):
        """``CancelledError`` derives from ``BaseException``, so a bare
        ``except Exception`` would let a cancelled run — which may have
        committed writes — skip the invalidation (#2141 batch rule)."""
        app = _make_app()
        config = PolicyConfig(enabled=True)
        sched = PolicyScheduler(app, config)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await sched._run_policies()

        app.search_pipeline.invalidate_cache.assert_called_once()

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
            # Poll until at least 2 ticks happen (CI can be slow).
            for _ in range(40):
                await asyncio.sleep(0.05)
                if call_count >= 2:
                    break
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


class TestMemPolicyRunInvalidation:
    """``mem_policy_run`` is the scheduler's manual twin — the cache-drop
    postconditions have to match (#2157)."""

    @staticmethod
    def _app() -> MagicMock:
        app = MagicMock()
        app.storage = AsyncMock()
        app.storage.policy_get = AsyncMock(
            return_value={"name": "p1", "policy_type": "auto_archive"}
        )
        app.search_pipeline = MagicMock()
        return app

    @staticmethod
    def _patch_app(monkeypatch, app):
        from memtomem.server.tools import policy as policy_tool

        async def _fake_app(_ctx):
            return app

        monkeypatch.setattr(policy_tool, "_get_app_initialized", _fake_app)
        return policy_tool

    @pytest.mark.asyncio
    async def test_named_run_invalidates_before_bookkeeping_write(self, monkeypatch):
        """``policy_update_last_run`` is bookkeeping — a committed policy run
        must not lose its cache drop when that write fails."""
        app = self._app()
        app.storage.policy_update_last_run = AsyncMock(side_effect=RuntimeError("db down"))
        policy_tool = self._patch_app(monkeypatch, app)

        with patch(
            "memtomem.tools.policy_engine.run_policy",
            new_callable=AsyncMock,
            return_value=_result("p1", 3),
        ):
            # ``@tool_handler`` converts the failure into an error response
            # rather than propagating it.
            out = await policy_tool.mem_policy_run(name="p1", dry_run=False, ctx=None)
        assert "db down" in out

        app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_named_run_gates_on_mutated(self, monkeypatch):
        app = self._app()
        policy_tool = self._patch_app(monkeypatch, app)

        with patch(
            "memtomem.tools.policy_engine.run_policy",
            new_callable=AsyncMock,
            return_value=_result("p1", 0),
        ):
            await policy_tool.mem_policy_run(name="p1", dry_run=False, ctx=None)

        app.search_pipeline.invalidate_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_enabled_invalidates_when_run_raises(self, monkeypatch):
        """Policies before the failing one are already committed."""
        app = self._app()
        policy_tool = self._patch_app(monkeypatch, app)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            out = await policy_tool.mem_policy_run(dry_run=False, ctx=None)
        assert "boom" in out

        app.search_pipeline.invalidate_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_dry_run_failure_does_not_invalidate(self, monkeypatch):
        """A preview writes nothing, however it ends."""
        app = self._app()
        policy_tool = self._patch_app(monkeypatch, app)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            out = await policy_tool.mem_policy_run(dry_run=True, ctx=None)
        assert "boom" in out

        app.search_pipeline.invalidate_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancellation_invalidates(self, monkeypatch):
        """Same ``BaseException`` gap on the manual path (#2141)."""
        app = self._app()
        policy_tool = self._patch_app(monkeypatch, app)

        with patch(
            "memtomem.tools.policy_engine.run_all_enabled",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await policy_tool.mem_policy_run(dry_run=False, ctx=None)

        app.search_pipeline.invalidate_cache.assert_called_once()
