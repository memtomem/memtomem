"""Transient-safe orphan-source detection (issue #1565).

An *orphan source* is an indexed ``source_file`` whose path no longer exists
on disk — its chunks are candidates for deletion. Several call sites detect
orphans the same way (``[sf for sf in get_all_source_files() if not sf.exists()]``)
and then delete them: the scheduled ``compaction`` job, the health-watchdog
auto-maintenance path, and the interactive ``mem_cleanup_orphans`` tool.

A single ``exists()`` check is unsafe when sources live under a cloud-sync or
network mount (Dropbox/iCloud reconnect, VPN blip, sleep/wake remount): if the
mount is briefly absent at the moment the check runs, *every* source under it
reports ``exists() == False`` and an unattended delete wipes all their chunks
as "orphans". This module centralizes two independent guards:

1. **Two-pass re-check** (:func:`scan_orphans`) — a path is only reported
   orphaned if it fails ``exists()`` on an initial pass *and* again after a
   short delay, filtering sub-second transient absences. Mirrors the guard
   already living inline in ``health_maintenance.py``.
2. **Mass-delete brake** (:func:`is_suspected_mass_orphan`) — even a stable
   two-pass result can be a mount that stayed down past the re-check window,
   so unattended callers additionally refuse to delete when the confirmed
   orphans are both numerous (absolute floor) *and* a large fraction of all
   sources. "Everything vanished at once" is far likelier a mount failure than
   a real mass deletion; the safe move is to skip and warn, not delete.

The thresholds are module constants (not config) — they mirror the previously
hardcoded 0.5 s delay and add no new config/docs surface. Callers read them at
call time, so tests can monkeypatch them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.storage.base import StorageBackend

# Delay between the two ``exists()`` passes. Matches the value that lived inline
# in ``MaintenanceExecutor.cleanup_orphans`` before this module existed.
ORPHAN_RECHECK_DELAY_SECONDS = 0.5

# Mass-delete brake. The absolute floor keeps small corpora deleting normally
# (deleting the only indexed file is a ratio of 1.0 but not a "mass" event);
# the brake only engages once *both* thresholds are crossed.
MASS_DELETE_MIN_ORPHANS = 10
MASS_DELETE_RATIO = 0.5


@dataclass(frozen=True)
class OrphanScanResult:
    """Outcome of a two-pass orphan scan.

    ``confirmed_orphans`` are the paths that failed ``exists()`` on *both*
    passes; ``first_pass_orphans`` records how many failed the initial pass
    (``>= len(confirmed_orphans)``) for observability.
    """

    total_sources: int
    first_pass_orphans: int
    confirmed_orphans: list[Path] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """Confirmed orphans as a fraction of all indexed sources (0.0 if none)."""
        if self.total_sources <= 0:
            return 0.0
        return len(self.confirmed_orphans) / self.total_sources


async def scan_orphans(
    storage: StorageBackend,
    *,
    recheck_delay_seconds: float | None = None,
) -> OrphanScanResult:
    """Two-pass scan for indexed sources whose files no longer exist.

    A source is only confirmed orphaned if it fails :meth:`pathlib.Path.exists`
    on an initial pass *and* again after ``recheck_delay_seconds`` (defaults to
    :data:`ORPHAN_RECHECK_DELAY_SECONDS`, read at call time). When the first
    pass finds nothing there is no second pass and no delay.

    Both passes run the (potentially slow, blocking) ``exists()`` stats off the
    event loop via :func:`asyncio.to_thread`, matching ``check_orphan_count`` —
    a hanging network mount must not stall the whole server.
    """
    delay = ORPHAN_RECHECK_DELAY_SECONDS if recheck_delay_seconds is None else recheck_delay_seconds

    sources = await storage.get_all_source_files()
    total = len(sources)

    first_pass = await asyncio.to_thread(lambda: [sf for sf in sources if not sf.exists()])
    if not first_pass:
        return OrphanScanResult(total_sources=total, first_pass_orphans=0)

    await asyncio.sleep(delay)
    confirmed = await asyncio.to_thread(lambda: [sf for sf in first_pass if not sf.exists()])
    return OrphanScanResult(
        total_sources=total,
        first_pass_orphans=len(first_pass),
        confirmed_orphans=confirmed,
    )


def is_suspected_mass_orphan(result: OrphanScanResult) -> bool:
    """Whether ``result`` looks like a mount failure rather than a real deletion.

    True only when the confirmed orphans clear *both* the absolute floor
    (:data:`MASS_DELETE_MIN_ORPHANS`) and the fraction-of-all-sources ratio
    (:data:`MASS_DELETE_RATIO`). Unattended callers use this to skip the delete
    and warn; the constants are read here so tests can monkeypatch them.
    """
    n = len(result.confirmed_orphans)
    if n < MASS_DELETE_MIN_ORPHANS:
        return False
    return result.total_sources > 0 and result.ratio >= MASS_DELETE_RATIO
