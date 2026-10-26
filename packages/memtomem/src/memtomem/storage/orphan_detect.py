"""Two-pass indexed-source availability scan (#1565, #2498).

The scanner distinguishes missing paths from filesystem errors. Automatic
callers hold either state outside search; only confirmed missing paths are
candidates for an explicit, freshly checked purge. The old mass-ratio helper
remains for diagnostics and compatibility, never as an automatic delete gate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem.storage.sqlite_helpers import fold_path_form, norm_dir_prefix, norm_path

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)

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


async def orphan_candidate_sources(storage: StorageBackend) -> set[Path]:
    """Filesystem-backed sources, plus detached policy summaries for cleanup."""
    candidate_getter = (
        getattr(storage, "get_orphan_candidate_source_files", None)
        if hasattr(type(storage), "get_orphan_candidate_source_files")
        else None
    )
    return set(
        await candidate_getter()
        if candidate_getter is not None
        else await storage.get_all_source_files()
    )


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

    sources = await orphan_candidate_sources(storage)
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


@dataclass(frozen=True)
class OrphanGroup:
    """Confirmed orphans that share an owning index root.

    ``root`` is ``None`` for sources outside every configured root — removed
    worktrees, temp dirs, a root the user dropped from the config.
    """

    root: Path | None
    sources: list[Path]

    @property
    def label(self) -> str:
        return str(self.root) if self.root is not None else "outside every configured root"


def _resolvable_prefixes(dirs: Iterable[str | Path]) -> tuple[str, ...]:
    """``norm_dir_prefix`` of each dir, skipping one that cannot be resolved.

    Python 3.12 raises ``RuntimeError`` for a symlink loop, which ``norm_path``
    does not catch. One such entry must not stop every pass that prepares a
    rule from holding or showing the other sources; skipping it retains
    nothing under it, which is today's behavior.
    """
    prefixes: list[str] = []
    for d in dirs:
        try:
            prefixes.append(norm_dir_prefix(Path(d).expanduser()))
        except (OSError, RuntimeError):
            logger.warning("Cannot resolve %s; nothing under it is retained", d)
    return tuple(prefixes)


def _under_any(key: str, prefixes: tuple[str, ...]) -> bool:
    return bool(prefixes) and key.startswith(prefixes)


@dataclass(frozen=True)
class RetainRule:
    """Which missing sources ``indexing.retain_missing_dirs`` keeps (#2833).

    A source is retained when it lies under a retain dir **and** under an index
    root, judged by its stored key like :func:`group_orphans`. Without the
    root clause a dir dropped from the config would keep its sources visible
    with nothing left to manage them.

    Build one per pass with :meth:`prepare`; nothing caches it, so a config
    edited in place by hot reload is seen on the next pass. :meth:`prepare`
    and :meth:`retains` resolve paths, so async callers run them in a thread.
    :meth:`retains_key` takes a stored key, which ``norm_path`` already
    resolved on write, and only compares strings. It folds the key's Unicode
    form first, as the prefixes are folded: a key stored before keys were
    folded (#235) names the same file and must stay retained.
    """

    retain: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()

    @classmethod
    def prepare(cls, indexing: IndexingConfig) -> RetainRule:
        if not indexing.retain_missing_dirs:
            return cls()
        return cls(
            retain=_resolvable_prefixes(indexing.retain_missing_dirs),
            roots=_resolvable_prefixes(indexing.all_index_roots()),
        )

    def retains(self, source: Path) -> bool:
        return bool(self.retain) and self.retains_key(norm_path(source))

    def retains_key(self, key: str) -> bool:
        key = fold_path_form(key)
        return (
            bool(self.retain)
            and key.startswith(self.retain)
            and bool(self.roots)
            and key.startswith(self.roots)
        )

    def split(self, keys: Iterable[Path]) -> tuple[list[Path], list[Path]]:
        """Split stored keys into ``(retained, others)``, preserving order."""
        retained: list[Path] = []
        others: list[Path] = []
        for key in keys:
            (retained if self.retains_key(str(key)) else others).append(key)
        return retained, others


@dataclass(frozen=True)
class OrphanHoldOutcome:
    """What automatic cleanup did with one :func:`scan_orphans` result."""

    changed: bool
    missing_held: int
    unavailable_held: int
    archived: int


async def hold_orphans(
    storage: StorageBackend, result: OrphanScanResult, indexing: IndexingConfig
) -> OrphanHoldOutcome:
    """Hold scanned-out sources, except the retained ones, which are shown (#2498, #2833).

    Automatic cleanup never purges. A confirmed-missing source under
    ``retain_missing_dirs`` has its absence hold lifted instead, which is also
    how holds placed before the dir was retained come back into search.
    Unavailable sources are held either way: something may still be there.
    """
    retained: list[Path] = []
    missing = list(result.confirmed_orphans)
    if indexing.retain_missing_dirs:
        rule = await asyncio.to_thread(RetainRule.prepare, indexing)
        retained, missing = rule.split(result.confirmed_orphans)
    changed = False
    for sf in retained:
        changed |= await storage.release_absence_hold(str(sf))
    for sf in missing:
        changed |= await storage.hold_source(sf, "scan_missing", as_stored=True)
    for sf in result.unavailable_sources:
        changed |= await storage.hold_source(sf, "scan_unavailable", as_stored=True)
    return OrphanHoldOutcome(
        changed=changed,
        missing_held=len(missing),
        unavailable_held=len(result.unavailable_sources),
        archived=len(retained),
    )


def group_orphans(
    orphans: Iterable[Path],
    roots: Iterable[str | Path],
    *,
    under: Iterable[str | Path] = (),
) -> tuple[list[OrphanGroup], int]:
    """Group ``orphans`` by owning root, keeping only those under ``under``.

    Returns the groups (roots sorted, the outside-every-root group last) and
    how many orphans ``under`` left out. An empty ``under`` keeps everything.

    Ownership is the longest-prefix rule doctor and the Sources tree use, so
    each source lands in exactly one group. ``orphans`` are stored keys, and
    both rules judge each by that key, the rows the purge deletes with
    ``as_stored``. A stored path that has since become a symlink is judged
    where the link sits, where it was indexed, not where it points (#2835).
    The key is compared in its folded Unicode form, as the prefixes are, so a
    key stored before keys were folded (#235) groups with its root.
    The roots and ``under`` are resolved, so a symlink alias for a directory
    matches only while the link still exists, and async callers run this in a
    thread.
    """
    from memtomem.indexing.dir_stats import _longest_owning_prefix

    root_by_prefix: dict[str, Path] = {}
    for d in roots:
        root_by_prefix.setdefault(norm_dir_prefix(d), Path(d).expanduser())
    prefixes = tuple(norm_dir_prefix(Path(d).expanduser()) for d in under)
    kept: list[Path] = []
    excluded = 0
    for source in orphans:
        if prefixes and not _under_any(fold_path_form(str(source)), prefixes):
            excluded += 1
        else:
            kept.append(source)
    by_root: dict[Path | None, list[Path]] = {}
    for source in kept:
        owner = _longest_owning_prefix(fold_path_form(str(source)), root_by_prefix)
        by_root.setdefault(root_by_prefix[owner] if owner is not None else None, []).append(source)
    groups = [
        OrphanGroup(root, sorted(sources))
        for root, sources in sorted(by_root.items(), key=lambda kv: (kv[0] is None, str(kv[0])))
    ]
    return groups, excluded


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
