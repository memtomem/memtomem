"""Health watchdog — periodic background health monitoring and auto-maintenance."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from memtomem.server.health_checks import (
    DEEP_CHECKS,
    DIAGNOSTIC_CHECKS,
    HEARTBEAT_CHECKS,
    check_trend_comparison,
)
from memtomem.server.health_maintenance import MaintenanceExecutor
from memtomem.server.health_store import HealthSnapshot, HealthStore

if TYPE_CHECKING:
    from memtomem.config import HealthWatchdogConfig
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)

_CHECK_TIMEOUT = 30.0  # per-check timeout


class HealthWatchdog:
    """Coordinates periodic health checks and auto-maintenance."""

    def __init__(self, app: AppContext, config: HealthWatchdogConfig) -> None:
        self._app = app
        self._config = config
        self._store: HealthStore | None = None
        self._maintenance: MaintenanceExecutor | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self._config.enabled:
            return

        from pathlib import Path

        db_path = Path(self._app.config.storage.sqlite_path).expanduser().resolve()
        self._store = HealthStore(db_path, self._config.max_snapshots)
        self._store.initialize()
        self._maintenance = MaintenanceExecutor(self._app, self._config)
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Health watchdog started (heartbeat: %.0fs, diagnostic: %.0fs, deep: %.0fs)",
            self._config.heartbeat_interval_seconds,
            self._config.diagnostic_interval_seconds,
            self._config.deep_interval_seconds,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._store:
            self._store.close()
            self._store = None

    async def _run_loop(self) -> None:
        last_heartbeat = 0.0
        last_diagnostic = 0.0
        last_deep = 0.0

        while True:
            now = time.monotonic()

            if now - last_heartbeat >= self._config.heartbeat_interval_seconds:
                await self._run_tier("heartbeat", HEARTBEAT_CHECKS)
                last_heartbeat = now

            if now - last_diagnostic >= self._config.diagnostic_interval_seconds:
                await self._run_tier("diagnostic", DIAGNOSTIC_CHECKS)
                last_diagnostic = now

            if now - last_deep >= self._config.deep_interval_seconds:
                await self._run_tier("deep", DEEP_CHECKS)
                # Trend comparison needs store
                if self._store:
                    await self._run_check(
                        lambda app: check_trend_comparison(app, self._store)
                    )
                last_deep = now

            await asyncio.sleep(min(self._config.heartbeat_interval_seconds, 10.0))

    async def _run_tier(self, tier: str, checks: list) -> None:
        for check_fn in checks:
            await self._run_check(check_fn)

    async def _run_check(self, check_fn) -> None:
        if not self._store:
            return
        try:
            snapshot = await asyncio.wait_for(check_fn(self._app), timeout=_CHECK_TIMEOUT)
            self._store.record(snapshot)

            if snapshot.status == "critical":
                logger.warning(
                    "Health check CRITICAL: %s — %s", snapshot.check_name, snapshot.value
                )
                if self._config.auto_maintenance and self._maintenance:
                    await self._auto_maintain(snapshot)
            elif snapshot.status == "warning":
                logger.info(
                    "Health check WARNING: %s — %s", snapshot.check_name, snapshot.value
                )
        except asyncio.TimeoutError:
            logger.warning("Health check timed out: %s", getattr(check_fn, "__name__", "?"))
        except Exception:
            logger.error("Health check failed: %s", getattr(check_fn, "__name__", "?"), exc_info=True)

    async def _auto_maintain(self, snapshot: HealthSnapshot) -> None:
        if not self._maintenance:
            return

        try:
            if snapshot.check_name == "orphan_count":
                orphaned = snapshot.value.get("orphaned", 0)
                if orphaned >= self._config.orphan_cleanup_threshold:
                    result = await self._maintenance.cleanup_orphans()
                    logger.info("Auto-maintenance orphan cleanup: %s", result)

            elif snapshot.check_name == "wal_status":
                result = await self._maintenance.checkpoint_wal()
                logger.info("Auto-maintenance WAL checkpoint: %s", result)

            elif snapshot.check_name == "search_cache_size":
                result = await self._maintenance.trim_search_cache()
                logger.info("Auto-maintenance cache trim: %s", result)
        except Exception:
            logger.error("Auto-maintenance failed for %s", snapshot.check_name, exc_info=True)

    async def run_now(self) -> dict:
        """Force immediate execution of all checks. Returns results dict."""
        if not self._store:
            return {"error": "watchdog not initialized"}

        results: dict[str, dict] = {}
        all_checks = [
            ("heartbeat", HEARTBEAT_CHECKS),
            ("diagnostic", DIAGNOSTIC_CHECKS),
            ("deep", DEEP_CHECKS),
        ]
        for _tier, checks in all_checks:
            for check_fn in checks:
                try:
                    snap = await asyncio.wait_for(check_fn(self._app), timeout=_CHECK_TIMEOUT)
                    self._store.record(snap)
                    results[snap.check_name] = {"status": snap.status, "value": snap.value}
                except Exception as exc:
                    name = getattr(check_fn, "__name__", "unknown")
                    results[name] = {"status": "error", "value": {"error": str(exc)}}

        # Trend comparison
        try:
            snap = await check_trend_comparison(self._app, self._store)
            self._store.record(snap)
            results[snap.check_name] = {"status": snap.status, "value": snap.value}
        except Exception as exc:
            results["trend_comparison"] = {"status": "error", "value": {"error": str(exc)}}

        return results

    def get_status(self) -> dict:
        if not self._store:
            return {"enabled": False}
        return {
            "enabled": True,
            "running": self._task is not None and not self._task.done(),
            "checks": self._store.get_summary(),
        }

    def get_trends(self, check_name: str, hours: float = 24.0) -> list[dict]:
        if not self._store:
            return []
        return [
            {"check": s.check_name, "status": s.status, "value": s.value, "at": s.created_at}
            for s in self._store.get_trend(check_name, hours)
        ]
