"""Two-pass indexed-source availability scan (#1565, #2498).

The scanner distinguishes missing paths from filesystem errors. Automatic
callers hold either state outside search; only confirmed missing paths are
candidates for an explicit, freshly checked purge. The old mass-ratio helper
remains for diagnostics and compatibility, never as an automatic delete gate.
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


def source_state(source: Path) -> str:
    """Return present, missing or unavailable without conflating OS errors."""
    try:
        source.stat()
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unavailable"
    return "present"


# Legacy mass-delete diagnostic thresholds. Absence alone no longer authorizes
# unattended deletion, so these are not a safety gate for automatic callers.
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
    unavailable_sources: list[Path] = field(default_factory=list)

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

    A source is only confirmed missing if ``stat`` fails with ENOENT or ENOTDIR
    on a second pass after ``recheck_delay_seconds`` (defaults to
    :data:`ORPHAN_RECHECK_DELAY_SECONDS`, read at call time). When the first
    pass finds nothing there is no second pass and no delay.

    Both passes run the (potentially slow, blocking) ``stat`` probes off the
    event loop via :func:`asyncio.to_thread`, matching ``check_orphan_count`` —
    a hanging network mount must not stall the whole server.
    """
    delay = ORPHAN_RECHECK_DELAY_SECONDS if recheck_delay_seconds is None else recheck_delay_seconds

    candidate_getter = (
        getattr(storage, "get_orphan_candidate_source_files", None)
        if hasattr(type(storage), "get_orphan_candidate_source_files")
        else None
    )
    sources = (
        await candidate_getter()
        if candidate_getter is not None
        else await storage.get_all_source_files()
    )
    total = len(sources)

    def probe(paths: set[Path] | list[Path]) -> dict[Path, str]:
        return {source: source_state(source) for source in paths}

    first = await asyncio.to_thread(probe, sources)
    suspect = [source for source, state in first.items() if state != "present"]
    if not suspect:
        return OrphanScanResult(total_sources=total, first_pass_orphans=0)

    await asyncio.sleep(delay)
    second = await asyncio.to_thread(probe, suspect)
    confirmed = [source for source, state in second.items() if state == "missing"]
    unavailable = [source for source, state in second.items() if state == "unavailable"]
    return OrphanScanResult(
        total_sources=total,
        first_pass_orphans=sum(state == "missing" for state in first.values()),
        confirmed_orphans=confirmed,
        unavailable_sources=unavailable,
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
