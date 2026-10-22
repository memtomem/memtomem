"""Auto-maintenance actions triggered by health watchdog.

All operations are idempotent and non-destructive (only removes stale data).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memtomem.storage.orphan_detect import hold_orphans, scan_orphans

if TYPE_CHECKING:
    from memtomem.config import HealthWatchdogConfig
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


class MaintenanceExecutor:
    """Executes safe auto-maintenance actions when thresholds are exceeded."""

    def __init__(self, app: AppContext, config: HealthWatchdogConfig) -> None:
        self._app = app
        self._config = config

    async def cleanup_orphans(self) -> dict:
        """Hold uncertain sources without deleting indexed chunks (#2498)."""
        result = await scan_orphans(self._app.storage)
        outcome = await hold_orphans(self._app.storage, result, self._app.config.indexing)
        held = outcome.missing_held + outcome.unavailable_held
        if outcome.changed:
            self._app.search_pipeline.invalidate_cache()
            logger.warning(
                "Auto-maintenance: held %d unavailable source(s), kept %d archived; "
                "no chunks deleted",
                held,
                outcome.archived,
            )
        return {
            "orphaned": outcome.missing_held,
            "held_sources": held,
            "archived_sources": outcome.archived,
            "deleted_chunks": 0,
        }

    async def trim_search_cache(self, max_entries: int = 30) -> dict:
        """Evict oldest entries from the search pipeline cache."""
        cache = self._app.search_pipeline._search_cache
        before = len(cache)
        if before <= max_entries:
            return {"before": before, "after": before, "evicted": 0}

        sorted_keys = sorted(cache, key=lambda k: cache[k][0])
        to_remove = sorted_keys[: before - max_entries]
        for k in to_remove:
            del cache[k]

        evicted = len(to_remove)
        logger.info("Auto-maintenance: trimmed search cache from %d to %d", before, len(cache))
        return {"before": before, "after": len(cache), "evicted": evicted}

    async def checkpoint_wal(self) -> dict:
        """Run a passive WAL checkpoint."""
        db = self._app.storage._get_db()
        row = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        busy, log_pages, checkpointed = row if row else (0, 0, 0)
        logger.info(
            "Auto-maintenance: WAL checkpoint — %d/%d pages checkpointed",
            checkpointed,
            log_pages,
        )
        return {"busy": busy, "log_pages": log_pages, "checkpointed": checkpointed}

    async def cleanup_old_sessions(self, max_age_days: int = 90) -> dict:
        """Delete ended sessions older than max_age_days."""
        deleted = await self._app.storage.cleanup_old_sessions(max_age_days)
        if deleted:
            logger.info(
                "Auto-maintenance: cleaned %d old sessions (>%d days)", deleted, max_age_days
            )
        return {"deleted_sessions": deleted}
