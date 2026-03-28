"""Auto-consolidation scheduler — periodic memory consolidation scans."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import ConsolidationScheduleConfig
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
