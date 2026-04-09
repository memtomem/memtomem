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
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
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

    # STM proxy gateway (integrated mode)
    stm_proxy_manager = None
    stm_surfacing_engine = None
    stm_feedback_tracker = None
    stm_metrics_store = None
    stm_proxy_cache = None
    if config.stm_proxy.enabled:
        try:
            from memtomem_stm.config import STMConfig
            from memtomem_stm.proxy.cache import ProxyCache
            from memtomem_stm.proxy.manager import ProxyManager
            from memtomem_stm.proxy.metrics import TokenTracker
            from memtomem_stm.proxy.metrics_store import MetricsStore
            from memtomem_stm.surfacing.engine import SurfacingEngine
            from memtomem_stm.surfacing.feedback import FeedbackTracker

            stm_config = STMConfig()

            # Persistent metrics
            if stm_config.proxy.metrics.enabled:
                stm_metrics_store = MetricsStore(
                    stm_config.proxy.metrics.db_path.expanduser().resolve(),
                    max_history=stm_config.proxy.metrics.max_history,
                )
                stm_metrics_store.initialize()

            # Response cache
            if stm_config.proxy.cache.enabled:
                stm_proxy_cache = ProxyCache(
                    stm_config.proxy.cache.db_path.expanduser().resolve(),
                    max_entries=stm_config.proxy.cache.max_entries,
                )
                stm_proxy_cache.initialize()

            # Feedback tracker must be created before engine
            if stm_config.surfacing.feedback_enabled:
                stm_feedback_tracker = FeedbackTracker(stm_config.surfacing)

            stm_surfacing_engine = SurfacingEngine(
                stm_config.surfacing,
                search_pipeline=comp.search_pipeline,
                storage=comp.storage,
                webhook_manager=webhook_mgr,
                feedback_tracker=stm_feedback_tracker,
            )

            stm_tracker = TokenTracker(metrics_store=stm_metrics_store)
            stm_proxy_manager = ProxyManager(
                stm_config.proxy,
                stm_tracker,
                index_engine=comp.index_engine,
                surfacing_engine=stm_surfacing_engine,
                cache=stm_proxy_cache,
            )
            await stm_proxy_manager.start()

            # Register proxy tools on the memtomem MCP server
            from memtomem.server import mcp as _mcp
            from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

            def _make_proxy_handler(pm: ProxyManager, srv: str, tool_name: str):  # noqa: ANN202
                async def proxy_tool(**kwargs: object) -> str | list:
                    return await pm.call_tool(srv, tool_name, dict(kwargs))

                return proxy_tool

            for info in stm_proxy_manager.get_proxy_tools():
                register_proxy_tool(
                    _mcp,
                    _make_proxy_handler(stm_proxy_manager, info.server, info.original_name),
                    info,
                )

            logger.info(
                "STM proxy started: %d upstream tools, surfacing=%s",
                len(stm_proxy_manager.get_proxy_tools()),
                "enabled" if stm_config.surfacing.enabled else "disabled",
            )
        except ImportError:
            logger.info(
                "memtomem-stm not installed — STM proxy disabled (pip install memtomem-stm)"
            )
        except Exception:
            logger.warning("STM proxy initialization failed", exc_info=True)

    ctx = AppContext(
        config=config,
        storage=comp.storage,
        embedder=comp.embedder,
        index_engine=comp.index_engine,
        search_pipeline=comp.search_pipeline,
        watcher=watcher,
        dedup_scanner=dedup_scanner,
        webhook_manager=webhook_mgr,
        stm_proxy_manager=stm_proxy_manager,
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
        if stm_proxy_manager is not None:
            from memtomem.server import mcp as _mcp

            for info in stm_proxy_manager.get_proxy_tools():
                try:
                    _mcp.remove_tool(info.prefixed_name)
                except Exception:
                    pass
            try:
                await stm_proxy_manager.stop()
            except Exception:
                logger.warning("Failed to stop STM proxy manager", exc_info=True)
        for resource, name in [
            (stm_feedback_tracker, "stm_feedback_tracker"),
            (stm_proxy_cache, "stm_proxy_cache"),
            (stm_metrics_store, "stm_metrics_store"),
        ]:
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Failed to close %s", name, exc_info=True)
        if webhook_mgr:
            try:
                await webhook_mgr.close()
            except Exception:
                logger.warning("Failed to close webhook manager", exc_info=True)
        await _shutdown(watcher, comp)
