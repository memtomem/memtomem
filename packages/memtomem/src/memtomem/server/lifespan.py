"""MCP server lifespan management."""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import sys
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from memtomem.config_signature import build_fresh_config
from memtomem.server.context import (
    AppContext,
    _reset_active_app,
    _set_active_app,
    _stop_quietly,
)

logger = logging.getLogger(__name__)


@dataclass
class _ServerRuntimeState:
    """Ref-counted process services for one low-level MCP server.

    The MCP SDK enters its lifespan once per SSE connection.  The first
    connection owns the expensive component graph and background loops;
    later connections get an AppContext facade with independent session
    state.  The owner is closed only after the last connection exits.
    """

    server_ref: weakref.ReferenceType[Any]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: AppContext | None = None
    webhook_manager: object | None = None
    references: int = 0


_SERVER_RUNTIMES: dict[int, _ServerRuntimeState] = {}


def _runtime_state(server: object) -> _ServerRuntimeState:
    key = id(server)
    state = _SERVER_RUNTIMES.get(key)
    if state is not None and state.server_ref() is server:
        return state
    state = _ServerRuntimeState(server_ref=weakref.ref(server))
    _SERVER_RUNTIMES[key] = state
    return state


async def _acquire_server_runtime(server: object) -> tuple[_ServerRuntimeState, AppContext]:
    state = _runtime_state(server)
    async with state.lock:
        owner = state.owner
        if owner is None:
            # Handshake-time config discovery may read service flags, but it
            # must neither rewrite legacy config nor fail the handshake on a
            # malformed user override. The owner runs migration at first real
            # initialization, immediately before components are constructed.
            config = build_fresh_config(migrate=False, strict_overrides=False)
            webhook_mgr = None
            try:
                if config.webhook.enabled and config.webhook.url:
                    from memtomem.server.webhooks import WebhookManager

                    webhook_mgr = WebhookManager(config.webhook)
                owner = AppContext(
                    config=config,
                    webhook_manager=webhook_mgr,
                    register_server_instance=True,
                    ambient_config_loaded=True,
                    defer_config_migration=True,
                )
            except BaseException:
                await _stop_quietly(webhook_mgr, "webhook_manager")
                raise
            state.owner = owner
            state.webhook_manager = webhook_mgr
            if config.warmup.enabled:
                from memtomem.server.warmup import spawn_warmup

                owner._warmup_task = spawn_warmup(owner)
            context = owner
        else:
            context = AppContext.from_runtime_owner(owner)
        state.references += 1
        return state, context


async def _release_server_runtime(
    state: _ServerRuntimeState,
) -> asyncio.CancelledError | None:
    """Release one connection and settle shared services at refcount zero."""
    async with state.lock:
        if state.references <= 0:
            return None
        state.references -= 1
        if state.references:
            return None

        owner = state.owner
        # The owner's field, not this cached one, is authoritative once first
        # initialization has run: it reconciles the manager against the
        # post-migration config, so the handshake-era object cached here may
        # already have been stopped and replaced.  Stopping the stale one would
        # leave the live manager's client open.
        webhook_mgr = owner.webhook_manager if owner is not None else state.webhook_manager
        first_cancel: asyncio.CancelledError | None = None
        try:
            await _stop_quietly(webhook_mgr, "webhook_manager")
        except asyncio.CancelledError as exc:
            first_cancel = exc
        try:
            await _stop_quietly(owner, "app_context")
        except asyncio.CancelledError as exc:
            if first_cancel is None:
                first_cancel = exc
        state.owner = None
        state.webhook_manager = None
        return first_cancel


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    log_format = os.environ.get("MEMTOMEM_LOG_FORMAT", "text")
    log_level = os.environ.get("MEMTOMEM_LOG_LEVEL", "INFO").upper()

    if log_format == "json":
        logging.config.dictConfig(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {"json": {"()": "memtomem.server.lifespan._JsonFormatter"}},
                "handlers": {
                    "stderr": {
                        "class": "logging.StreamHandler",
                        "stream": "ext://sys.stderr",
                        "formatter": "json",
                    }
                },
                "root": {"level": log_level, "handlers": ["stderr"]},
            }
        )
    else:
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stderr,
        )


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        from datetime import datetime, timezone

        obj = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            obj["error"] = str(record.exc_info[1])
        return _json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Main lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    """Run the MCP server with lazy component init (Phase 3 of #399).

    Startup is deliberately minimal: load env, set up logging, build the
    optional webhook manager, allocate the ``AppContext`` itself. Persisted
    service flags may be read, but handshake startup does not create runtime
    files or migrate/rewrite config. The first tool-call path goes through
    :meth:`AppContext.ensure_initialized`, which opens storage/embedder
    and starts the file watcher + schedulers + health watchdog inside
    the context (which from then on owns their lifetime).

    One opt-in exception (#1621): when ``config.warmup.enabled`` is set,
    a background task runs the full init + local model preload right
    away so the first query doesn't pay the cold-start cost. That flag
    deliberately trades the handshake-only laziness for warm models —
    the default-off path stays byte-for-byte lazy.

    Shutdown closes the webhook manager first — dropping outstanding
    network retries before the slower DB teardown, see PR #404 — then
    ``ctx.close()`` stops anything ``ensure_initialized`` started and
    finally closes components. Both stop calls go through
    :func:`_stop_quietly` so a teardown failure on one side does not
    skip the other, and ``CancelledError`` propagates rather than being
    silently swallowed (see #406).
    """
    _load_dotenv()
    _setup_logging()

    state, ctx = await _acquire_server_runtime(_server)

    # Static resource handlers get no ``ctx`` from the 2.0 SDK; publish this
    # one for them (see ``context._get_active_app_initialized``). Scoped to
    # this lifespan's task, so overlapping SSE connections — which each enter
    # their own lifespan — don't share the handle. Published before the
    # ``try`` so the token always exists by the time ``finally`` retracts it.
    active_app_token = _set_active_app(ctx)
    try:
        yield ctx
    finally:
        _reset_active_app(active_app_token)
        # Accumulate-and-defer (#1935): a cancellation during the webhook
        # stop must not skip ``ctx.close()`` — the context owns the
        # instance-registry settlement, which has to run. The first caught
        # cancellation re-raises only when no exception is already in
        # flight (raising inside ``finally`` would mask the original —
        # typically the very cancellation that triggered shutdown).
        first_cancel = await _release_server_runtime(state)
        if first_cancel is not None and sys.exc_info()[1] is None:
            raise first_cancel
