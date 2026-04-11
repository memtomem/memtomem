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
from memtomem.search.dedup import DedupScanner
from memtomem.server.component_factory import Components, close_components, create_components
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


async def _shutdown(watcher: FileWatcher, comp: Components) -> None:
    await watcher.stop()
    await close_components(comp)


# ---------------------------------------------------------------------------
# Main lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    _load_dotenv()
    _setup_logging()

    config = Mem2MemConfig()
    comp = await create_components(config)

    watcher = FileWatcher(comp.index_engine, config.indexing)
    await watcher.start()

    dedup_scanner = DedupScanner(storage=comp.storage, embedder=comp.embedder)

    # Webhook manager
    webhook_mgr = None
    if config.webhook.enabled and config.webhook.url:
        from memtomem.server.webhooks import WebhookManager

        webhook_mgr = WebhookManager(config.webhook)

    ctx = AppContext(
        config=config,
        storage=comp.storage,
        embedder=comp.embedder,
        index_engine=comp.index_engine,
        search_pipeline=comp.search_pipeline,
        watcher=watcher,
        dedup_scanner=dedup_scanner,
        webhook_manager=webhook_mgr,
    )

    # Auto-consolidation scheduler
    scheduler = None
    if config.consolidation_schedule.enabled:
        from memtomem.server.scheduler import ConsolidationScheduler

        scheduler = ConsolidationScheduler(ctx, config.consolidation_schedule)
        await scheduler.start()

    # Health watchdog
    watchdog = None
    if config.health_watchdog.enabled:
        from memtomem.server.health_watchdog import HealthWatchdog

        watchdog = HealthWatchdog(ctx, config.health_watchdog)
        await watchdog.start()
        ctx.health_watchdog = watchdog

    try:
        yield ctx
    finally:
        if watchdog:
            try:
                await watchdog.stop()
            except Exception:
                logger.warning("Failed to stop health watchdog", exc_info=True)
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
        await _shutdown(watcher, comp)
