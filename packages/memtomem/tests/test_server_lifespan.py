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
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.server import lifespan as lifespan_mod
from .helpers import set_home


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


@pytest.mark.asyncio
async def test_handshake_defers_legacy_config_migration_until_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import memtomem.config as config_mod
    import memtomem.server.component_factory as factory_mod

    home = tmp_path / "home"
    config_dir = home / ".memtomem"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    provider_dir = tmp_path / "provider-memory"
    provider_dir.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(config_mod, "_canonical_provider_dirs", lambda: [provider_dir])
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")

    async def stop_after_migration(*_args, **_kwargs):
        raise RuntimeError("stop after migration")

    monkeypatch.setattr(factory_mod, "create_components", stop_after_migration)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert config_path.read_text(encoding="utf-8") == "{}"
        ctx.register_server_instance = False
        with pytest.raises(RuntimeError, match="stop after migration"):
            await ctx.ensure_initialized()

        migrated = json.loads(config_path.read_text(encoding="utf-8"))
        assert migrated["indexing"]["auto_discover"] is False
        assert provider_dir.resolve() in {
            Path(value).expanduser().resolve() for value in migrated["indexing"]["memory_dirs"]
        }


@pytest.mark.asyncio
async def test_first_initialization_reloads_fragments_changed_after_handshake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import memtomem.server.component_factory as factory_mod

    home = tmp_path / "home"
    fragment_dir = home / ".memtomem" / "config.d"
    fragment_dir.mkdir(parents=True)
    fragment = fragment_dir / "10-search.json"
    fragment.write_text(json.dumps({"search": {"default_top_k": 11}}), encoding="utf-8")
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")

    captured: dict[str, object] = {}

    async def stop_after_reload(config, **kwargs):  # type: ignore[no-untyped-def]
        captured["top_k"] = config.search.default_top_k
        captured["load_ambient_config"] = kwargs["load_ambient_config"]
        raise RuntimeError("stop after reload")

    monkeypatch.setattr(factory_mod, "create_components", stop_after_reload)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert ctx.config.search.default_top_k == 11
        fragment.write_text(json.dumps({"search": {"default_top_k": 37}}), encoding="utf-8")

        ctx.register_server_instance = False
        with pytest.raises(RuntimeError, match="stop after reload"):
            await ctx.ensure_initialized()

        assert ctx.config.search.default_top_k == 37
        assert captured == {"top_k": 37, "load_ambient_config": False}


@pytest.mark.asyncio
async def test_handshake_tolerates_malformed_config_without_rewriting_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".memtomem"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.json"
    malformed = "{not-json"
    config_path.write_text(malformed, encoding="utf-8")
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert ctx._components is None

    assert config_path.read_text(encoding="utf-8") == malformed


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
async def test_same_server_overlapping_lifespans_share_one_runtime_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE connections keep session state separate but refcount one runtime."""
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__ENABLED", raising=False)
    monkeypatch.delenv("MEMTOMEM_WEBHOOK__URL", raising=False)
    server = MagicMock()
    close_calls: list[object] = []

    async def record_close(self) -> None:  # type: ignore[no-untyped-def]
        close_calls.append(self)

    import memtomem.server.context as context_mod

    monkeypatch.setattr(context_mod.AppContext, "close", record_close)

    async with lifespan_mod.app_lifespan(server) as first:
        first.current_session_id = "first-session"
        async with lifespan_mod.app_lifespan(server) as second:
            assert second is not first
            assert second._runtime_owner is first
            assert second.config is first.config
            assert second.current_session_id is None
            second.current_session_id = "second-session"
            assert first.current_session_id == "first-session"
        # Disconnecting one SSE client must not stop services still used by
        # the other connection.
        assert close_calls == []

    assert close_calls == [first]


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


class _RecordingWebhookManager:
    instances: list["_RecordingWebhookManager"] = []

    def __init__(self, config: object, *_args: object, **_kwargs: object) -> None:
        self.config = config
        self.closed = False
        type(self).instances.append(self)

    async def close(self) -> None:
        self.closed = True


def _record_webhook_managers(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingWebhookManager]:
    import memtomem.server.webhooks as webhooks_mod

    _RecordingWebhookManager.instances = []
    monkeypatch.setattr(webhooks_mod, "WebhookManager", _RecordingWebhookManager)
    return _RecordingWebhookManager.instances


def _stop_before_components(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem.server.component_factory as factory_mod

    async def stop(config, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("stop after reload")

    monkeypatch.setattr(factory_mod, "create_components", stop)


@pytest.mark.asyncio
async def test_first_initialization_drops_a_webhook_disabled_after_handshake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Handshake reads config before migration — that staleness is the whole
    reason the migration is deferred. The webhook manager is built from that
    same stale config, so without reconciliation a webhook the user disabled
    between handshake and the first tool call keeps delivering to the endpoint
    they removed."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")
    _enable_webhook(monkeypatch)
    managers = _record_webhook_managers(monkeypatch)
    _stop_before_components(monkeypatch)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        assert ctx.webhook_manager is managers[0]

        monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
        ctx.register_server_instance = False
        with pytest.raises(RuntimeError, match="stop after reload"):
            await ctx.ensure_initialized()

        assert ctx.config.webhook.enabled is False
        assert ctx.webhook_manager is None
        assert managers[0].closed is True


@pytest.mark.asyncio
async def test_first_initialization_repoints_a_webhook_url_and_teardown_follows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The replacement manager is the one teardown has to stop. The lifespan
    caches the handshake-era object, so reading that cache would close the
    stale manager and leave the live one's client open."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")
    _enable_webhook(monkeypatch)
    managers = _record_webhook_managers(monkeypatch)
    _stop_before_components(monkeypatch)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        monkeypatch.setenv("MEMTOMEM_WEBHOOK__URL", "https://moved.invalid/hook")
        ctx.register_server_instance = False
        with pytest.raises(RuntimeError, match="stop after reload"):
            await ctx.ensure_initialized()

        assert len(managers) == 2
        assert managers[0].closed is True
        assert str(managers[1].config.url) == "https://moved.invalid/hook"
        assert ctx.webhook_manager is managers[1]

    assert managers[1].closed is True, "teardown stopped the stale manager, not the live one"


@pytest.mark.asyncio
async def test_facade_picks_up_the_reconciled_webhook_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second SSE connection opened before first initialization must not keep
    the handshake-era manager.

    ``from_runtime_owner`` copies ``config`` and ``webhook_manager`` by value.
    The owner replaces both during the deferred config rebuild, so a facade
    that only re-read ``_components`` would go on firing at an endpoint whose
    manager the owner has already closed.
    """
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")
    _enable_webhook(monkeypatch)
    managers = _record_webhook_managers(monkeypatch)
    _stop_before_components(monkeypatch)

    server = MagicMock()
    async with lifespan_mod.app_lifespan(server) as owner:
        async with lifespan_mod.app_lifespan(server) as facade:
            assert facade._runtime_owner is owner
            assert facade.webhook_manager is managers[0]

            monkeypatch.setenv("MEMTOMEM_WEBHOOK__URL", "https://moved.invalid/hook")
            owner.register_server_instance = False
            with pytest.raises(RuntimeError, match="stop after reload"):
                await facade.ensure_initialized()

            assert owner.webhook_manager is managers[1]
            assert facade.webhook_manager is managers[1]
            assert facade.config is owner.config
            assert managers[0].closed is True


@pytest.mark.asyncio
async def test_aborted_initialization_still_reloads_config_on_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An aborted first initialization must not bank a config it never read.

    ``defer_config_migration`` used to be cleared next to the rebuild, but
    everything after it can raise — the webhook close is a cancellation point,
    and so is every component start. The retry then skipped the rebuild, kept
    the config from the aborted attempt, and recorded the *current* signature
    over it, so an edit made in between counted as applied while never being
    read.
    """
    home = tmp_path / "home"
    fragment_dir = home / ".memtomem" / "config.d"
    fragment_dir.mkdir(parents=True)
    fragment = fragment_dir / "10-search.json"
    fragment.write_text(json.dumps({"search": {"default_top_k": 11}}), encoding="utf-8")
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_WEBHOOK__ENABLED", "false")
    monkeypatch.setenv("MEMTOMEM_WARMUP__ENABLED", "false")

    seen: list[int] = []

    async def stop_after_reload(config, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(config.search.default_top_k)
        raise RuntimeError("stop after reload")

    import memtomem.server.component_factory as factory_mod

    monkeypatch.setattr(factory_mod, "create_components", stop_after_reload)

    async with lifespan_mod.app_lifespan(MagicMock()) as ctx:
        ctx.register_server_instance = False
        with pytest.raises(RuntimeError, match="stop after reload"):
            await ctx.ensure_initialized()

        # A config edit lands between the aborted attempt and the retry.
        fragment.write_text(json.dumps({"search": {"default_top_k": 37}}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="stop after reload"):
            await ctx.ensure_initialized()

        assert seen == [11, 37], "the retry reused the aborted attempt's config"
        assert ctx.config.search.default_top_k == 37
