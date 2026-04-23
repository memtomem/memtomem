"""Tests for ``app_lifespan`` startup + shutdown semantics (#399 Phase 3).

Phase 3 slimmed ``app_lifespan`` to:

* startup = load env, set up logging, allocate webhook manager,
  allocate ``AppContext`` (no ``ensure_initialized`` call, no DB touch);
* shutdown = close webhook then ``ctx.close()`` (which itself stops any
  background loops and closes components ``ensure_initialized`` started).

These tests pin the new shape so a regression — e.g. someone adding a
``ctx.ensure_initialized()`` back into the startup path — fails loudly.
The earlier pre-Phase-3 helper ``_teardown_startup_resources`` is gone;
its order/idempotency invariants now live on ``AppContext.close`` and
are covered in ``test_server_app_context.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.server import lifespan as lifespan_mod


# ── helpers ───────────────────────────────────────────────────────────


class _FakeWebhook:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.close = AsyncMock()


def _enable_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__URL", "https://example.invalid/hook")


def _stub_webhook_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem.server.webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod, "WebhookManager", _FakeWebhook)


# ── handshake-only path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_yields_context_without_initializing_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of Phase 3: handshake (lifespan enter → exit
    without any tool call) must leave ``_components`` ``None`` so the
    SQLite DB is never opened. A regression where someone adds an
    ``await ctx.ensure_initialized()`` back into the startup path would
    flip this assertion."""
    # Disable webhook so we don't have to mock it for this minimal case.
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__ENABLED", raising=False)
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__URL", raising=False)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert ctx is not None
        assert ctx._components is None, "lifespan must not eagerly init components"
        assert ctx._watcher is None, "lifespan must not eagerly start watcher"
        assert ctx._scheduler is None
        assert ctx._policy_scheduler is None
        assert ctx._health_watchdog is None


# ── shutdown ordering ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_closes_webhook_before_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook closes first so outstanding network retries drop before
    the (slower) component teardown — same rationale as the pre-Phase-3
    ``_teardown_startup_resources`` doc."""
    _enable_webhook(monkeypatch)
    _stub_webhook_manager(monkeypatch)

    order: list[str] = []
    captured: dict[str, object] = {}

    async def _record_webhook_close() -> None:
        order.append("webhook")

    async def _record_ctx_close(self) -> None:  # type: ignore[no-untyped-def]
        order.append("ctx")

    # Stub WebhookManager.close to record without doing any work.
    import memtomem.server.webhooks as webhooks_mod

    class _RecordingWebhook:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.close = _record_webhook_close
            captured["webhook"] = self

    monkeypatch.setattr(webhooks_mod, "WebhookManager", _RecordingWebhook)

    # Stub AppContext.close so we don't have to drag in real components.
    import memtomem.server.context as context_mod

    monkeypatch.setattr(context_mod.AppContext, "close", _record_ctx_close)

    async with lifespan_mod.app_lifespan(MagicMock()):
        pass

    assert order == ["webhook", "ctx"], (
        f"webhook must close before ctx (PR #404 rationale); got {order}"
    )


@pytest.mark.asyncio
async def test_lifespan_continues_teardown_after_webhook_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If webhook ``close()`` raises ``Exception``, ``ctx.close()`` must
    still run — partial shutdown is worse than a logged failure."""
    _enable_webhook(monkeypatch)

    ctx_closed = False

    async def _bad_webhook_close() -> None:
        raise RuntimeError("webhook close boom")

    async def _record_ctx_close(self) -> None:  # type: ignore[no-untyped-def]
        nonlocal ctx_closed
        ctx_closed = True

    import memtomem.server.webhooks as webhooks_mod

    class _BadWebhook:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.close = _bad_webhook_close

    monkeypatch.setattr(webhooks_mod, "WebhookManager", _BadWebhook)

    import memtomem.server.context as context_mod

    monkeypatch.setattr(context_mod.AppContext, "close", _record_ctx_close)

    async with lifespan_mod.app_lifespan(MagicMock()):
        pass

    assert ctx_closed, "ctx.close must run even if webhook close raised"


@pytest.mark.asyncio
async def test_lifespan_reraises_cancellation_during_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CancelledError`` from a teardown step must propagate so task
    cancellation is observable — masking it would let shutdown look
    successful when in fact the loop was being torn down out from under
    us. Mirrors PR #406 / `feedback_cancelled_error_except_gap.md`."""
    import asyncio

    _enable_webhook(monkeypatch)

    async def _cancel_webhook_close() -> None:
        raise asyncio.CancelledError()

    import memtomem.server.webhooks as webhooks_mod

    class _CancelWebhook:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.close = _cancel_webhook_close

    monkeypatch.setattr(webhooks_mod, "WebhookManager", _CancelWebhook)

    with pytest.raises(asyncio.CancelledError):
        async with lifespan_mod.app_lifespan(MagicMock()):
            pass


# ── startup-failure path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_cleans_up_webhook_when_appcontext_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``AppContext()`` raises (very rare — it's allocation-only —
    but possible if dataclass field defaults change), the partially-
    constructed webhook must still be closed before the lifespan
    re-raises. This covers the startup-failure ``except BaseException``
    branch in ``app_lifespan``."""
    _enable_webhook(monkeypatch)

    closed = False

    async def _record_close() -> None:
        nonlocal closed
        closed = True

    import memtomem.server.webhooks as webhooks_mod

    class _RecordingWebhook:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.close = _record_close

    monkeypatch.setattr(webhooks_mod, "WebhookManager", _RecordingWebhook)

    # Force AppContext construction to raise. ``lifespan_mod`` already
    # imported the symbol with ``from .context import AppContext``, so we
    # patch the *binding inside lifespan_mod* — patching the source module
    # would leave the lifespan-local reference pointing at the real class.
    def _boom_init(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("appcontext boom")

    monkeypatch.setattr(lifespan_mod, "AppContext", _boom_init)

    with pytest.raises(RuntimeError, match="appcontext boom"):
        async with lifespan_mod.app_lifespan(MagicMock()):
            pytest.fail("yield should not be reached")

    assert closed, "webhook must be closed when AppContext construction fails"
