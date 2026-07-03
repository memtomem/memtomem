"""Auto-maintenance actions triggered by health watchdog.

All operations are idempotent and non-destructive (only removes stale data).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memtomem.storage.orphan_detect import is_suspected_mass_orphan, scan_orphans

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
        """Delete chunks whose source files no longer exist.

        Uses a two-pass scan to avoid false positives from temporarily
        inaccessible files (e.g., network mounts, permission changes), and
        refuses a suspected *mass* orphan event (many sources vanishing at
        once — the tell-tale of a mount failure, not a real deletion) rather
        than wiping every chunk under the mount unattended. See #1565.
        """
        result = await scan_orphans(self._app.storage)

        if not result.confirmed_orphans:
            return {"orphaned": 0, "deleted_chunks": 0}

        if is_suspected_mass_orphan(result):
            logger.warning(
                "Auto-maintenance: skipping suspected mass orphan delete "
                "(%d/%d sources, %.0f%%) — likely a transient mount/permission "
                "failure. No chunks deleted.",
                len(result.confirmed_orphans),
                result.total_sources,
                100 * result.ratio,
            )
            return {
                "orphaned": len(result.confirmed_orphans),
                "deleted_chunks": 0,
                "skipped_reason": "orphan_ratio_exceeded",
            }

        total_deleted = 0
        for sf in result.confirmed_orphans:
            deleted = await self._app.storage.delete_by_source(sf)
            total_deleted += deleted

        logger.info(
            "Auto-maintenance: cleaned %d orphaned files (%d chunks)",
            len(result.confirmed_orphans),
            total_deleted,
        )
        return {"orphaned": len(result.confirmed_orphans), "deleted_chunks": total_deleted}

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
