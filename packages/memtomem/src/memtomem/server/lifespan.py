"""MCP server lifespan management."""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from memtomem.config import Mem2MemConfig
from memtomem.server.context import AppContext, _stop_quietly

logger = logging.getLogger(__name__)


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
# Parent-death watchdog (#440)
# ---------------------------------------------------------------------------


def _watchdog_enabled() -> bool:
    """Gated by ``MEMTOMEM_PARENT_WATCHDOG`` env (default on).

    Accepts ``off`` / ``0`` / ``false`` (case-insensitive) to disable.
    Kept separate for testability — unit tests can monkey-patch the
    environment without spawning a real subprocess."""
    return os.environ.get("MEMTOMEM_PARENT_WATCHDOG", "on").lower() not in (
        "off",
        "0",
        "false",
    )


def _watchdog_interval() -> float:
    """Poll interval in seconds; ``MEMTOMEM_PARENT_WATCHDOG_INTERVAL`` override.

    Default 10s — small enough that an orphan releases its pid flock
    within seconds of the client exiting, large enough that idle CPU
    overhead is negligible."""
    try:
        return float(os.environ.get("MEMTOMEM_PARENT_WATCHDOG_INTERVAL", "10"))
    except ValueError:
        return 10.0


async def _watch_parent(original_ppid: int, poll_seconds: float) -> None:
    """Exit the server when the MCP client parent process disappears.

    Motivation — issue #440: Claude Code (and possibly other MCP stdio
    clients) sometimes terminate without closing our stdio unix sockets
    OR sending a signal, leaving ``memtomem-server`` alive as an orphan
    holding ``~/.memtomem/.server.pid``. The next client start loses
    the flock race against the zombie and reports "Failed to connect".

    POSIX gives us a portable defense: when a child's parent exits, the
    child is reparented (PID 1 on Linux, launchd on macOS). Polling
    ``os.getppid()`` and exiting on change is:

    - Client-agnostic: no dependency on stdio close, SIGTERM delivery,
      or a JSON-RPC ``exit`` notification.
    - False-positive-free: PPID changes only when the parent really
      exited — POSIX semantics guarantee this.
    - Works on macOS and Linux without PR_SET_PDEATHSIG (Linux-only).

    Exit mechanism: self-``SIGTERM`` rather than ``os._exit`` so the
    sigterm handler installed by ``server.main()`` fires, unlinking
    every pid file we own (issue #437 / PR #439). Going directly to
    ``os._exit`` would bypass ``atexit`` and re-create the stale-file
    class of bugs #439 closed.
    """
    while True:
        try:
            await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            return
        current_ppid = os.getppid()
        if current_ppid != original_ppid:
            logger.warning(
                "Parent process %d exited (reparented to %d). Self-SIGTERM "
                "to release MCP server state — issue #440.",
                original_ppid,
                current_ppid,
            )
            os.kill(os.getpid(), signal.SIGTERM)
            return  # Signal handler will take over; nothing more for us to do.


# ---------------------------------------------------------------------------
# Main lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Run the MCP server with lazy component init (Phase 3 of #399).

    Startup is deliberately minimal: load env, set up logging, build the
    optional webhook manager, allocate the ``AppContext`` itself. None of
    these touch ``~/.memtomem/`` — that's the whole point of the lazy
    init. The first tool-call path goes through
    :meth:`AppContext.ensure_initialized`, which opens storage/embedder
    and starts the file watcher + schedulers + health watchdog inside
    the context (which from then on owns their lifetime).

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

    config = Mem2MemConfig()

    webhook_mgr = None
    ctx: AppContext | None = None

    try:
        if config.webhook.enabled and config.webhook.url:
            from memtomem.server.webhooks import WebhookManager

            webhook_mgr = WebhookManager(config.webhook)
        ctx = AppContext(config=config, webhook_manager=webhook_mgr)
    except BaseException:
        # ``AppContext()`` is allocation-only and never touches storage,
        # so the webhook is the only thing that could be partially
        # allocated here. Close it before re-raising so we don't leak
        # the network state into the failure path.
        await _stop_quietly(webhook_mgr, "webhook_manager")
        raise

    watchdog_task: asyncio.Task[None] | None = None
    if _watchdog_enabled():
        watchdog_task = asyncio.create_task(
            _watch_parent(os.getppid(), _watchdog_interval()),
            name="memtomem-parent-watchdog",
        )

    try:
        yield ctx
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except (asyncio.CancelledError, Exception):
                # CancelledError is the expected path; any other exception
                # during watchdog teardown is informational — don't let it
                # mask the main lifespan cleanup below.
                pass
        await _stop_quietly(webhook_mgr, "webhook_manager")
        await _stop_quietly(ctx, "app_context")
