"""Auto-consolidation scheduler — periodic memory consolidation scans."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import ConsolidationScheduleConfig, PolicyConfig
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


class ConsolidationScheduler:
    """Runs consolidation scans at configurable intervals."""

    def __init__(self, app: AppContext, config: ConsolidationScheduleConfig):
        self._app = app
        self._config = config
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the periodic scan loop."""
        if not self._config.enabled:
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Consolidation scheduler started (interval: %.1fh)", self._config.interval_hours
        )

    async def stop(self) -> None:
        """Stop the scan loop."""
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_loop(self) -> None:
        interval_seconds = self._config.interval_hours * 3600
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self._run_scan()
            except Exception:
                logger.error("Consolidation scan failed", exc_info=True)

    async def _run_scan(self) -> None:
        """Run a consolidation scan and store results in working memory."""
        import json

        storage = self._app.storage
        groups = await storage.get_consolidation_groups(
            min_size=self._config.min_group_size,
            max_groups=self._config.max_groups,
        )

        if not groups:
            logger.debug("Consolidation scan: no groups found")
            return

        await storage.scratch_set(
            "consolidation_queue",
            json.dumps(groups, default=str),
            session_id=None,
        )

        logger.info("Consolidation scan found %d groups", len(groups))


class PolicyScheduler:
    """Runs memory lifecycle policies at configurable intervals."""

    def __init__(self, app: AppContext, config: PolicyConfig):
        self._app = app
        self._config = config
        self._task: asyncio.Task | None = None
        self._consecutive_failures: int = 0

    async def start(self) -> None:
        """Start the periodic policy loop."""
        if not self._config.enabled:
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Policy scheduler started (interval: %.1fm, max_actions: %d)",
            self._config.scheduler_interval_minutes,
            self._config.max_actions_per_run,
        )

    async def stop(self) -> None:
        """Stop the policy loop."""
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run_loop(self) -> None:
        interval_seconds = self._config.scheduler_interval_minutes * 60
        while True:
            await asyncio.sleep(interval_seconds)
            await self._run_policies()

    async def _run_policies(self) -> None:
        """Execute all enabled policies and invalidate cache if needed."""
        from memtomem.tools.policy_engine import run_all_enabled

        try:
            results = await run_all_enabled(
                self._app.storage,
                dry_run=False,
                max_actions=self._config.max_actions_per_run,
                llm_provider=getattr(self._app, "llm_provider", None),
            )
            self._consecutive_failures = 0
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                logger.warning(
                    "Policy scheduler: %d consecutive failures",
                    self._consecutive_failures,
                    exc_info=True,
                )
            else:
                logger.error("Policy scheduler run failed", exc_info=True)
            return

        if not results:
            logger.debug("Policy scheduler: no enabled policies")
            return

        total_affected = sum(r.affected_count for r in results)
        for r in results:
            if r.affected_count > 0:
                logger.info("Policy '%s' (%s): %s", r.policy_name, r.policy_type, r.details)
            else:
                logger.debug("Policy '%s' (%s): %s", r.policy_name, r.policy_type, r.details)

        if total_affected > 0:
            self._app.search_pipeline.invalidate_cache()
            logger.info(
                "Policy scheduler completed: %d policies, %d total actions",
                len(results),
                total_affected,
            )
