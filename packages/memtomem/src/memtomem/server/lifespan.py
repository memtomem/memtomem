"""MCP server lifespan management."""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from memtomem.config import Mem2MemConfig
from memtomem.indexing.engine import dirs_needing_initial_scan
from memtomem.indexing.watcher import FileWatcher
from memtomem.search.dedup import DedupScanner
from memtomem.server.component_factory import Components, close_components, create_components
from memtomem.server.context import AppContext

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig
    from memtomem.indexing.engine import IndexEngine
    from memtomem.storage.base import StorageBackend

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
    for label, coro in [("watcher", watcher.stop()), ("components", close_components(comp))]:
        try:
            await coro
        except Exception:
            logger.warning("Shutdown step '%s' failed", label, exc_info=True)


async def _initial_scan_missing_dirs(
    engine: IndexEngine,
    storage: StorageBackend,
    cfg_indexing: IndexingConfig,
) -> None:
    """Index any ``memory_dirs`` that have no chunks yet.

    Runs once at startup as a background task. The ``FileWatcher`` only
    reacts to change events, so pre-existing files in a newly added
    ``memory_dir`` (e.g., added by the ``mm init`` wizard or the deprecated
    ``auto_discover`` migration) would otherwise never make it into the
    index. This helper closes that gap without forcing a full re-scan on
    every startup — dirs that already contain indexed files are skipped.
    """
    try:
        needing = await dirs_needing_initial_scan(storage, cfg_indexing.memory_dirs)
    except Exception:
        logger.warning("Initial-scan gating check failed; skipping", exc_info=True)
        return

    if not needing:
        return

    logger.info("Initial scan: indexing %d memory_dir(s) with no prior chunks", len(needing))
    for d in needing:
        try:
            await engine.index_path(d, recursive=True)
            logger.info("Initial scan complete for %s", d)
        except Exception:
            logger.warning("Initial scan failed for %s", d, exc_info=True)


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


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

    initial_scan_task: asyncio.Task[None] | None = asyncio.create_task(
        _initial_scan_missing_dirs(comp.index_engine, comp.storage, config.indexing),
        name="memtomem-initial-scan",
    )

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
        llm_provider=comp.llm,
    )

    # Auto-consolidation scheduler
    scheduler = None
    if config.consolidation_schedule.enabled:
        from memtomem.server.scheduler import ConsolidationScheduler

        scheduler = ConsolidationScheduler(ctx, config.consolidation_schedule)
        await scheduler.start()

    # Policy scheduler
    policy_scheduler = None
    if config.policy.enabled:
        from memtomem.server.scheduler import PolicyScheduler

        policy_scheduler = PolicyScheduler(ctx, config.policy)
        await policy_scheduler.start()

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
        await _cancel_task(initial_scan_task)
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
        await _shutdown(watcher, comp)
