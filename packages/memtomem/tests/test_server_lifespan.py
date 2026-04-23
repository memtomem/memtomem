"""Test ``app_lifespan`` startup-failure teardown (#404).

The original shape of ``app_lifespan`` had no guard between the first
resource allocation and the ``yield`` — any startup stage raising
before ``yield`` leaked everything already allocated, because the
``finally`` block only runs when ``yield`` was reached. This file
pins:

- :func:`_teardown_startup_resources` shape (order, idempotency, error
  tolerance), since both the normal shutdown path and the new
  startup-failure path funnel through it.
- A minimal integration check that a startup failure triggers
  reverse-order teardown of everything allocated so far and re-raises.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memtomem.server.lifespan import _teardown_startup_resources


def _fake_resource(stop_attr: str = "stop") -> object:
    """A fake with an async ``stop`` or ``close`` method that records calls."""

    class _R:
        def __init__(self) -> None:
            setattr(self, stop_attr, AsyncMock())

    return _R()


@pytest.mark.asyncio
async def test_teardown_all_none_is_noop() -> None:
    """No resources → no calls, no error."""
    await _teardown_startup_resources(
        watchdog=None,
        policy_scheduler=None,
        scheduler=None,
        webhook_mgr=None,
        watcher=None,
        ctx=None,
    )  # should not raise


@pytest.mark.asyncio
async def test_teardown_calls_each_in_reverse_allocation_order() -> None:
    """All resources present → stop/close called in reverse-allocation order."""
    order: list[str] = []

    class _Rec:
        def __init__(self, name: str, method: str) -> None:
            self.name = name
            self.method = method

            async def _call() -> None:
                order.append(name)

            setattr(self, method, _call)

    webhook = _Rec("webhook_mgr", "close")
    ctx = _Rec("ctx", "close")
    watcher = _Rec("watcher", "stop")
    scheduler = _Rec("scheduler", "stop")
    policy = _Rec("policy_scheduler", "stop")
    watchdog = _Rec("watchdog", "stop")

    await _teardown_startup_resources(
        watchdog=watchdog,
        policy_scheduler=policy,
        scheduler=scheduler,
        webhook_mgr=webhook,
        watcher=watcher,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert order == [
        "watchdog",
        "policy_scheduler",
        "scheduler",
        "webhook_mgr",
        "watcher",
        "ctx",
    ], "teardown must run in reverse-allocation order"


@pytest.mark.asyncio
async def test_teardown_continues_after_intermediate_failure() -> None:
    """A failing stop() must not skip later teardown steps."""
    called: list[str] = []

    async def _good_stop() -> None:
        called.append("good")

    async def _bad_stop() -> None:
        called.append("bad")
        raise RuntimeError("boom")

    class _R:
        pass

    watchdog = _R()
    watchdog.stop = _bad_stop  # type: ignore[attr-defined]
    scheduler = _R()
    scheduler.stop = _good_stop  # type: ignore[attr-defined]
    ctx = _R()
    ctx.close = _good_stop  # type: ignore[attr-defined]

    await _teardown_startup_resources(
        watchdog=watchdog,
        policy_scheduler=None,
        scheduler=scheduler,
        webhook_mgr=None,
        watcher=None,
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert called == ["bad", "good", "good"], (
        "watchdog failure must not skip scheduler/ctx teardown"
    )


@pytest.mark.asyncio
async def test_teardown_ctx_close_called_last() -> None:
    """``ctx.close()`` is the last step — DB handles must outlive the
    schedulers that hold references to them."""
    order: list[str] = []

    async def _record(name: str) -> None:
        order.append(name)

    class _R:
        pass

    watchdog = _R()

    async def _wd_stop() -> None:
        await _record("watchdog")

    watchdog.stop = _wd_stop  # type: ignore[attr-defined]

    ctx = _R()

    async def _ctx_close() -> None:
        await _record("ctx")

    ctx.close = _ctx_close  # type: ignore[attr-defined]

    await _teardown_startup_resources(
        watchdog=watchdog,
        policy_scheduler=None,
        scheduler=None,
        webhook_mgr=None,
        watcher=None,
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert order[-1] == "ctx", "ctx.close must be the final teardown step"


# ── integration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_failure_tears_down_prior_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If a late startup stage raises, earlier allocations get cleaned up.

    We drive ``app_lifespan`` through ``ensure_initialized`` and inject a
    failing health-watchdog ``start()`` — webhook_mgr, ctx, watcher, and
    scheduler must all see teardown, and the lifespan must re-raise.
    """
    from memtomem.server import lifespan as lifespan_mod

    # Track teardown calls
    teardown_calls: list[str] = []

    class _FakeWatcher:
        def __init__(self, *a, **k) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            teardown_calls.append("watcher")

    class _FakeWebhook:
        def __init__(self, *a, **k) -> None:
            pass

        async def close(self) -> None:
            teardown_calls.append("webhook_mgr")

    class _FakeScheduler:
        def __init__(self, *a, **k) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            teardown_calls.append("scheduler")

    class _FakePolicyScheduler(_FakeScheduler):
        async def stop(self) -> None:
            teardown_calls.append("policy_scheduler")

    class _FailingWatchdog:
        def __init__(self, *a, **k) -> None:
            pass

        async def start(self) -> None:
            raise RuntimeError("watchdog boom")

        async def stop(self) -> None:
            teardown_calls.append("watchdog")

    # Patch dependencies that the lifespan resolves via lazy imports.
    monkeypatch.setattr(lifespan_mod, "FileWatcher", _FakeWatcher)

    import memtomem.server.webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod, "WebhookManager", _FakeWebhook)

    import memtomem.server.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "ConsolidationScheduler", _FakeScheduler)
    monkeypatch.setattr(scheduler_mod, "PolicyScheduler", _FakePolicyScheduler)

    import memtomem.server.health_watchdog as watchdog_mod

    monkeypatch.setattr(watchdog_mod, "HealthWatchdog", _FailingWatchdog)

    # Stub AppContext.ensure_initialized so we don't need real storage.
    import memtomem.server.context as context_mod

    ensure_init_called = False
    ctx_closed = False

    class _FakeComponents:
        class _Engine:
            pass

        index_engine = _Engine()
        embedding_broken = None

    fake_comp = _FakeComponents()

    original_ensure = context_mod.AppContext.ensure_initialized
    original_close = context_mod.AppContext.close

    async def _fake_ensure(self) -> object:
        nonlocal ensure_init_called
        ensure_init_called = True
        self._components = fake_comp  # type: ignore[assignment]
        self._owns_components = True
        return fake_comp

    async def _fake_close(self) -> None:
        nonlocal ctx_closed
        ctx_closed = True

    monkeypatch.setattr(context_mod.AppContext, "ensure_initialized", _fake_ensure)
    monkeypatch.setattr(context_mod.AppContext, "close", _fake_close)

    # Force every optional subsystem on so we exercise webhook + both
    # schedulers + watchdog. We set env vars before Mem2MemConfig loads.
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__URL", "https://example.invalid/hook")
    monkeypatch.setenv("MEMTOMEM_CONSOLIDATION_SCHEDULE__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_POLICY__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_HEALTH_WATCHDOG__ENABLED", "true")
    # Keep storage path deterministic (but we don't actually hit disk, since
    # ensure_initialized is stubbed).
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "mm.db"))

    # Drive the lifespan and assert it re-raises the watchdog failure.
    with pytest.raises(RuntimeError, match="watchdog boom"):
        async with lifespan_mod.app_lifespan(object()):  # type: ignore[arg-type]
            pytest.fail("yield should not be reached — startup must fail first")

    # Everything allocated must have been torn down, including the
    # watchdog — it was constructed before ``start()`` raised, so the
    # ``watchdog`` local was already bound. Teardown steps must be
    # idempotent for this to be safe in general; our fake ``.stop()``
    # is trivially so.
    assert ensure_init_called, "ensure_initialized must have been called"
    assert ctx_closed, "ctx.close must have been called during teardown"
    assert teardown_calls == [
        "watchdog",
        "policy_scheduler",
        "scheduler",
        "webhook_mgr",
        "watcher",
    ], f"unexpected teardown order or missing steps: {teardown_calls}"

    _ = original_ensure, original_close
