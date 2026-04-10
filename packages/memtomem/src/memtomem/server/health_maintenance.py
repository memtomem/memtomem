"""Auto-maintenance actions triggered by health watchdog.

All operations are idempotent and non-destructive (only removes stale data).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

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

        Uses a two-pass check to avoid false positives from temporarily
        inaccessible files (e.g., network mounts, permission changes).
        """
        import asyncio

        source_files = await self._app.storage.get_all_source_files()
        candidates: list[Path] = [sf for sf in source_files if not sf.exists()]

        if not candidates:
            return {"orphaned": 0, "deleted_chunks": 0}

        # Second check after short delay to avoid transient failures
        await asyncio.sleep(0.5)
        orphaned: list[Path] = [sf for sf in candidates if not sf.exists()]

        if not orphaned:
            return {"orphaned": 0, "deleted_chunks": 0}

        total_deleted = 0
        for sf in orphaned:
            deleted = await self._app.storage.delete_by_source(sf)
            total_deleted += deleted

        logger.info(
            "Auto-maintenance: cleaned %d orphaned files (%d chunks)",
            len(orphaned),
            total_deleted,
        )
        return {"orphaned": len(orphaned), "deleted_chunks": total_deleted}

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
