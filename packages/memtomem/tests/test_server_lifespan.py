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

import asyncio
import os
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


# ── dotenv loading (#1508) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_invokes_dotenv_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup must call ``_load_dotenv()`` exactly once. The suite-wide
    conftest fixture no-ops the loader (#1508), so this spy — layered on
    top of that no-op — is the only remaining coverage that the production
    startup path still wires it."""
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__ENABLED", raising=False)
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__URL", raising=False)

    calls: list[None] = []
    monkeypatch.setattr(lifespan_mod, "_load_dotenv", lambda: calls.append(None))

    async with lifespan_mod.app_lifespan(MagicMock()):
        pass

    assert len(calls) == 1, f"_load_dotenv must run once at startup; ran {len(calls)}×"


@pytest.mark.asyncio
async def test_lifespan_under_test_does_not_source_repo_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end wiring pin for #1508: with the conftest hermeticity
    fixture active, entering the *real* lifespan must leave the bare
    langfuse env absent — even on a dev machine whose repo-root ``.env``
    defines it. Before the fix, this test polluted ``os.environ`` for
    every later test in the run (the four ``test_session_tracing``
    validator failures)."""
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__ENABLED", raising=False)
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__URL", raising=False)

    async with lifespan_mod.app_lifespan(MagicMock()):
        pass

    assert "LANGFUSE_PUBLIC_KEY" not in os.environ
    assert "LANGFUSE_SECRET_KEY" not in os.environ


@pytest.mark.real_dotenv
def test_load_dotenv_does_not_override_existing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real loader must keep already-exported env vars (python-dotenv's
    ``override=False`` default): a repo-root ``.env`` may *add* to the
    environment but never replace explicit configuration. Marked
    ``real_dotenv`` to opt out of the conftest no-op; the loader resolves
    ``.env`` upward from the source tree, so on a dev machine it may add
    that file's other keys — the snapshot/restore keeps this test from
    becoming the very polluter #1508 fixed."""
    snapshot = dict(os.environ)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "sentinel-keep-me")
    try:
        lifespan_mod._load_dotenv()
        assert os.environ["LANGFUSE_PUBLIC_KEY"] == "sentinel-keep-me"
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


# ── static-resource AppContext handle ─────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_publishes_and_retracts_active_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2.0 SDK refuses to inject ``Context`` into a static resource
    handler, so those handlers read the lifespan's ``AppContext`` through
    ``context._ACTIVE_APP``. The lifespan owns that handle: published on
    entry, retracted on exit — otherwise a static resource read after
    shutdown would touch a closed context instead of failing."""
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__ENABLED", raising=False)
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__URL", raising=False)

    import memtomem.server.context as context_mod

    assert context_mod._ACTIVE_APP.get() is None

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert context_mod._ACTIVE_APP.get() is ctx

    assert context_mod._ACTIVE_APP.get() is None


@pytest.mark.asyncio
async def test_overlapping_lifespans_do_not_share_the_active_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over SSE the SDK runs the whole lowlevel server — lifespan included —
    once per *connection*, so two clients can hold overlapping lifespans in
    separate tasks. Each must see its own ``AppContext``, and one
    disconnecting must not blank the handle for the other: with a plain
    module global the second enter would clobber the first, and the first
    exit would leave a live connection reading ``None``."""
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__ENABLED", raising=False)
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__URL", raising=False)

    import memtomem.server.context as context_mod

    started = asyncio.Event()
    may_exit = asyncio.Event()
    seen: dict[str, object] = {}

    async def first_connection() -> None:
        async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
            seen["first"] = ctx
            started.set()
            await may_exit.wait()
            # Still this connection's own context, after the second
            # connection has come and gone.
            seen["first_after"] = context_mod._ACTIVE_APP.get()

    task = asyncio.create_task(first_connection())
    try:
        await started.wait()
        async with lifespan_mod.app_lifespan(MagicMock()) as second:
            assert second is not seen["first"]
            assert context_mod._ACTIVE_APP.get() is second
    finally:
        may_exit.set()
        await task

    assert seen["first_after"] is seen["first"]
    # Both connections gone → nothing left published in this task either.
    assert context_mod._ACTIVE_APP.get() is None


@pytest.mark.asyncio
async def test_get_active_app_initialized_raises_outside_lifespan() -> None:
    """Fail loudly rather than hand a static resource a ``None`` app."""
    import memtomem.server.context as context_mod

    assert context_mod._ACTIVE_APP.get() is None
    with pytest.raises(RuntimeError, match="lifespan is not running"):
        await context_mod._get_active_app_initialized()


@pytest.mark.asyncio
async def test_static_resource_reads_the_active_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End of that path: a static resource handler resolves the published
    context and returns its data with no ``ctx`` parameter in sight."""
    import memtomem.server.context as context_mod
    import memtomem.server.resources as resources_mod

    app = MagicMock()
    app.ensure_initialized = AsyncMock()
    app.storage.list_namespaces = AsyncMock(return_value=[("work", 3)])

    token = context_mod._set_active_app(app)
    try:
        payload = await resources_mod.namespaces_resource()
    finally:
        context_mod._reset_active_app(token)

    assert '"namespace": "work"' in payload
    app.ensure_initialized.assert_awaited_once()
