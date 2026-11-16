"""MCP server lifespan management."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import logging.config
import os
import sys
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from memtomem.config_signature import build_fresh_config
from memtomem.server.context import AppContext, _stop_quietly

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
    # Set when the owner's own close raised, so its sequence did not finish;
    # read by the shared daemon, which must exit non-zero then. A stage that
    # close() logs and continues past (webhook, watcher, embedder) is not a
    # failure here: the process exit releases it, as for a standalone server.
    close_failed: bool = False


_SERVER_RUNTIMES: dict[int, _ServerRuntimeState] = {}

# The working directory of the client on the connection whose lifespan is being
# entered. Set by the shared daemon in the task that calls ``Server.run`` for
# that connection, immediately before the call; ``Server.run`` enters the
# lifespan in the same task, so ``app_lifespan`` sees it and copies it onto the
# connection's context. Never read by a handler: handlers take their context
# from the request. Unset on every other transport.
connection_client_cwd: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "memtomem_connection_client_cwd", default=None
)


def _runtime_state(server: object) -> _ServerRuntimeState:
    key = id(server)
    state = _SERVER_RUNTIMES.get(key)
    if state is not None and state.server_ref() is server:
        return state
    state = _ServerRuntimeState(server_ref=weakref.ref(server))
    _SERVER_RUNTIMES[key] = state
    return state


async def _acquire_server_runtime(
    server: object,
    configure_owner: Callable[[AppContext], None] | None = None,
) -> tuple[_ServerRuntimeState, AppContext]:
    """Take one reference on ``server``'s runtime, creating its owner if none exists.

    ``configure_owner`` runs on a newly created owner before anything can use
    it, warmup included. The shared daemon uses it to freeze the store before
    any initialization can start.
    """
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
            if configure_owner is not None:
                try:
                    configure_owner(owner)
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
            if not await _stop_quietly(owner, "app_context"):
                state.close_failed = True
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


# python-dotenv's own truthy spellings for ``PYTHON_DOTENV_DISABLED``
# (``dotenv.main._load_dotenv_disabled``), copied because that helper is private.
_DOTENV_DISABLED_VALUES = frozenset({"1", "true", "t", "yes", "y"})

# The left-out ``.env`` names are logged once per process: every SSE
# connection enters its own lifespan and reloads the file.
_dotenv_left_out_logged = False


def _load_dotenv() -> list[str]:
    """Apply the ``MEMTOMEM_*`` keys of the nearest ``.env``; return the names left out.

    Only the prefixed keys reach ``os.environ`` (#2824): the file found is
    usually ``~/.env``, and its other keys — API keys for unrelated tools —
    would otherwise enter this process and every subprocess it starts,
    whatever environment filtering the MCP client applied.

    Everything else matches ``load_dotenv()``. ``find_dotenv()`` is called
    from here, so the search still starts at this file rather than the CWD.
    Values are resolved by python-dotenv's own ``DotEnv`` with
    ``load_dotenv()``'s arguments spelled out: ``override=False`` keeps an
    exported value ahead of the file inside ``${VAR}`` (``dotenv_values()``
    would invert that), and the class's own defaults are a locale encoding
    and ``override=True``. Resolving writes nothing, so an unrelated key never
    enters the environment, even briefly. The prefix match is
    case-insensitive like the settings binding, an exported value wins, and a
    key with no value is not set.
    """
    from dotenv import find_dotenv
    from dotenv.main import DotEnv

    if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() in _DOTENV_DISABLED_VALUES:
        return []
    path = find_dotenv()
    if not path:
        return []
    values = DotEnv(path, encoding="utf-8", interpolate=True, override=False).dict()
    left_out: list[str] = []
    for key, value in values.items():
        if not key.upper().startswith("MEMTOMEM_"):
            left_out.append(key)
            continue
        if value is None or key in os.environ:
            continue
        try:
            os.environ[key] = value
        except ValueError:
            # A quoted key containing ``=`` is not a legal variable name.
            # ``load_dotenv()`` raised here, out of server startup.
            left_out.append(key)
    return left_out


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
    global _dotenv_left_out_logged
    dotenv_left_out = _load_dotenv()
    _setup_logging()
    if dotenv_left_out and not _dotenv_left_out_logged:
        _dotenv_left_out_logged = True
        logger.info(
            ".env: applied only MEMTOMEM_* keys; left out %d: %s",
            len(dotenv_left_out),
            ", ".join(dotenv_left_out),
        )

    state, ctx = await _acquire_server_runtime(_server)
    client_cwd = connection_client_cwd.get()
    if client_cwd is not None:
        ctx.client_cwd = client_cwd

    try:
        yield ctx
    finally:
        # Accumulate-and-defer (#1935): a cancellation during the webhook
        # stop must not skip ``ctx.close()`` — the context owns the
        # instance-registry settlement, which has to run. The first caught
        # cancellation re-raises only when no exception is already in
        # flight (raising inside ``finally`` would mask the original —
        # typically the very cancellation that triggered shutdown).
        first_cancel = await _release_server_runtime(state)
        if first_cancel is not None and sys.exc_info()[1] is None:
            raise first_cancel
