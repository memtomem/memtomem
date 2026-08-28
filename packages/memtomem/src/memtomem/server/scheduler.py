"""Auto-consolidation scheduler — periodic memory consolidation scans."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from memtomem.server.background import loop_task_error_cb, stop_loop_task

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
        self._task = asyncio.create_task(self._run_loop(), name="memtomem-consolidation-scheduler")
        self._task.add_done_callback(loop_task_error_cb)
        logger.info(
            "Consolidation scheduler started (interval: %.1fh)", self._config.interval_hours
        )

    async def stop(self) -> None:
        """Stop the scan loop."""
        # ``finally``: a cancellation aimed at the caller propagates out of
        # ``stop_loop_task`` (#2213), and a stale handle left behind would make
        # ``get_status()`` report a loop that is gone.
        try:
            if self._task:
                await stop_loop_task(self._task)
        finally:
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
        self._task = asyncio.create_task(self._run_loop(), name="memtomem-policy-scheduler")
        self._task.add_done_callback(loop_task_error_cb)
        logger.info(
            "Policy scheduler started (interval: %.1fm, max_actions: %d)",
            self._config.scheduler_interval_minutes,
            self._config.max_actions_per_run,
        )

    async def stop(self) -> None:
        """Stop the policy loop."""
        # ``finally`` for the same reason as ``ConsolidationScheduler.stop``.
        try:
            if self._task:
                await stop_loop_task(self._task)
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        interval_seconds = self._config.scheduler_interval_minutes * 60
        while True:
            await asyncio.sleep(interval_seconds)
            # ``_run_policies`` swallows its own ``Exception``, but a bug outside
            # that inner try — or in a future edit to it — would otherwise end the
            # loop for the life of the process. ``CancelledError`` is a
            # ``BaseException`` and still propagates, so ``stop()`` keeps working.
            try:
                await self._run_policies()
            except Exception:
                logger.error("Policy scheduler run failed", exc_info=True)

    async def _run_policies(self) -> None:
        """Execute all enabled policies and invalidate cache if needed."""
        from memtomem.tools.policy_engine import run_all_enabled

        try:
            results = await run_all_enabled(
                self._app.storage,
                dry_run=False,
                max_actions=self._config.max_actions_per_run,
                llm_provider=getattr(self._app, "llm_provider", None),
                extract_entities=self._app.config.indexing.extract_entities,
                source="scheduler",
            )
            self._consecutive_failures = 0
        except asyncio.CancelledError:
            # ``CancelledError`` derives from ``BaseException``, so it would
            # slip past the handler below and skip the invalidation entirely
            # — the #2141 batch rule, on the path that most needs it.
            self._app.search_pipeline.invalidate_cache()
            raise
        except Exception:
            # The failing policy may have partly landed, and earlier policies
            # in ``run_all_enabled``'s loop are already committed — the
            # per-result ``mutated`` signal never reached us, so the only
            # sound postcondition is "may have written" (#2157, #2141 rules).
            self._app.search_pipeline.invalidate_cache()
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

        # Gate on the explicit write signal, not the user-facing counter: the
        # counter is a pre-scan estimate for some handlers and unreported by a
        # handler that raises mid-write, so a run can write with
        # ``affected_count == 0`` (#2157).
        if any(r.mutated for r in results):
            self._app.search_pipeline.invalidate_cache()
        if total_affected > 0:
            logger.info(
                "Policy scheduler completed: %d policies, %d total actions",
                len(results),
                total_affected,
            )
