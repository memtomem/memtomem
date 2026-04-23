"""MCP server lifespan management."""

from __future__ import annotations

import logging
import logging.config
import os
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

    try:
        yield ctx
    finally:
        await _stop_quietly(webhook_mgr, "webhook_manager")
        await _stop_quietly(ctx, "app_context")
