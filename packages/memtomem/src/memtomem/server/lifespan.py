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


async def _teardown_startup_resources(
    *,
    watchdog: object | None,
    policy_scheduler: object | None,
    scheduler: object | None,
    webhook_mgr: object | None,
    watcher: FileWatcher | None,
    ctx: AppContext | None,
) -> None:
    """Tear down startup-allocated resources; called on both shutdown paths.

    Invoked from the normal post-``yield`` ``finally`` and from the
    startup-failure ``except BaseException`` (before re-raising, #404).
    Each step catches ``Exception`` so a later failure doesn't skip
    earlier ones — shutdown must always complete whatever it can — but
    ``CancelledError`` is re-raised so task cancellation propagates and
    the caller can decide whether to mask the original startup failure.

    Order (matches the existing post-yield shutdown sequence since
    before #404; #404 only centralised it into this helper):

        watchdog → policy_scheduler → scheduler → webhook_mgr → watcher → ctx

    This is *not* strict reverse-allocation order: ``webhook_mgr`` is
    stopped before ``watcher`` and ``ctx`` because it has no dependency
    on storage/index — closing it early drops network state that might
    otherwise hold retries open during the slower component teardown.
    ``ctx.close()`` is last so the schedulers/watchdog can still touch
    storage while they stop.
    """
    import asyncio

    def _log_teardown_failure(stage: str, exc: BaseException) -> None:
        # CancelledError is BaseException-derived in Python 3.8+; we still
        # want to record that a teardown step was cancelled (otherwise the
        # cancellation silently drops the original startup exception if it
        # happens mid-teardown), then re-raise so cancellation propagates
        # and the caller decides what to do.
        if isinstance(exc, asyncio.CancelledError):
            logger.warning("Shutdown step '%s' cancelled", stage)
            raise exc
        logger.warning("Shutdown step '%s' failed", stage, exc_info=True)

    if watchdog is not None:
        try:
            await watchdog.stop()  # type: ignore[attr-defined]
        except BaseException as exc:
            _log_teardown_failure("health_watchdog", exc)
    if policy_scheduler is not None:
        try:
            await policy_scheduler.stop()  # type: ignore[attr-defined]
        except BaseException as exc:
            _log_teardown_failure("policy_scheduler", exc)
    if scheduler is not None:
        try:
            await scheduler.stop()  # type: ignore[attr-defined]
        except BaseException as exc:
            _log_teardown_failure("scheduler", exc)
    if webhook_mgr is not None:
        try:
            await webhook_mgr.close()  # type: ignore[attr-defined]
        except BaseException as exc:
            _log_teardown_failure("webhook_manager", exc)
    if watcher is not None:
        try:
            await watcher.stop()
        except BaseException as exc:
            _log_teardown_failure("watcher", exc)
    if ctx is not None:
        try:
            await ctx.close()
        except BaseException as exc:
            _log_teardown_failure("components", exc)


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    _load_dotenv()
    _setup_logging()

    config = Mem2MemConfig()

    webhook_mgr = None
    watcher: FileWatcher | None = None
    scheduler = None
    policy_scheduler = None
    watchdog = None
    ctx: AppContext | None = None

    # Startup is wrapped in a single try/except so a failure at any stage
    # tears down everything allocated so far (#404). ``ensure_initialized``
    # has its own internal cleanup (PR #400) and leaves ``ctx._components``
    # unset on failure, so ``ctx.close()`` from the teardown path is a
    # safe no-op in that case.
    try:
        # Webhook manager is storage-free; safe to construct before component init.
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
        if config.consolidation_schedule.enabled and not degraded:
            from memtomem.server.scheduler import ConsolidationScheduler

            scheduler = ConsolidationScheduler(ctx, config.consolidation_schedule)
            await scheduler.start()

        # Policy scheduler
        if config.policy.enabled and not degraded:
            from memtomem.server.scheduler import PolicyScheduler

            policy_scheduler = PolicyScheduler(ctx, config.policy)
            await policy_scheduler.start()

        # Health watchdog
        if config.health_watchdog.enabled and not degraded:
            from memtomem.server.health_watchdog import HealthWatchdog

            watchdog = HealthWatchdog(ctx, config.health_watchdog)
            await watchdog.start()
            ctx.set_health_watchdog(watchdog)
    except BaseException:
        await _teardown_startup_resources(
            watchdog=watchdog,
            policy_scheduler=policy_scheduler,
            scheduler=scheduler,
            webhook_mgr=webhook_mgr,
            watcher=watcher,
            ctx=ctx,
        )
        raise

    try:
        yield ctx
    finally:
        await _teardown_startup_resources(
            watchdog=watchdog,
            policy_scheduler=policy_scheduler,
            scheduler=scheduler,
            webhook_mgr=webhook_mgr,
            watcher=watcher,
            ctx=ctx,
        )
