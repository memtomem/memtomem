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
from memtomem.indexing.watcher import FileWatcher
from memtomem.server.context import AppContext

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
    _load_dotenv()
    _setup_logging()

    config = Mem2MemConfig()

    # Webhook manager is storage-free; safe to construct before component init.
    webhook_mgr = None
    if config.webhook.enabled and config.webhook.url:
        from memtomem.server.webhooks import WebhookManager

        webhook_mgr = WebhookManager(config.webhook)

    ctx = AppContext(config=config, webhook_manager=webhook_mgr)

    # Phase 1 of #399 keeps init eager: the rest of startup (watcher,
    # schedulers, watchdog) needs storage/embedder ready. Phase 3 will move
    # this call (and the watcher/scheduler startup below) into the first
    # tool-call path.
    comp = await ctx.ensure_initialized()

    # When the server came up in degraded mode (embedding mismatch, see
    # issue #349) don't start the file watcher — indexing goes through
    # ``upsert_chunks`` which needs ``chunks_vec`` and would crash on
    # every file change. Recovery happens via ``mem_embedding_reset``.
    watcher = FileWatcher(comp.index_engine, config.indexing)
    if comp.embedding_broken is None:
        await watcher.start()

    # Background schedulers are skipped in degraded mode (see issue #349) —
    # they walk the index / re-embed chunks and would hit the same missing
    # ``chunks_vec`` cascade as the watcher. They resume after a restart
    # once ``mem_embedding_reset`` has fixed the DB.
    degraded = comp.embedding_broken is not None

    # Auto-consolidation scheduler
    scheduler = None
    if config.consolidation_schedule.enabled and not degraded:
        from memtomem.server.scheduler import ConsolidationScheduler

        scheduler = ConsolidationScheduler(ctx, config.consolidation_schedule)
        await scheduler.start()

    # Policy scheduler
    policy_scheduler = None
    if config.policy.enabled and not degraded:
        from memtomem.server.scheduler import PolicyScheduler

        policy_scheduler = PolicyScheduler(ctx, config.policy)
        await policy_scheduler.start()

    # Health watchdog
    watchdog = None
    if config.health_watchdog.enabled and not degraded:
        from memtomem.server.health_watchdog import HealthWatchdog

        watchdog = HealthWatchdog(ctx, config.health_watchdog)
        await watchdog.start()
        ctx.set_health_watchdog(watchdog)

    try:
        yield ctx
    finally:
        if watchdog:
            try:
                await watchdog.stop()
            except Exception:
                logger.warning("Failed to stop health watchdog", exc_info=True)
        if policy_scheduler:
            try:
                await policy_scheduler.stop()
            except Exception:
                logger.warning("Failed to stop policy scheduler", exc_info=True)
        if scheduler:
            try:
                await scheduler.stop()
            except Exception:
                logger.warning("Failed to stop scheduler", exc_info=True)
        if webhook_mgr:
            try:
                await webhook_mgr.close()
            except Exception:
                logger.warning("Failed to close webhook manager", exc_info=True)
        try:
            await watcher.stop()
        except Exception:
            logger.warning("Shutdown step 'watcher' failed", exc_info=True)
        try:
            await ctx.close()
        except Exception:
            logger.warning("Shutdown step 'components' failed", exc_info=True)
