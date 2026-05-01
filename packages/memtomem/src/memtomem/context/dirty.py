"""Detect drift between installed asset bytes and their lockfile snapshot.

Pure classifier used by ``mm context update`` to decide whether the
on-disk tree at ``<project>/.memtomem/<type>/<name>/`` still matches the
wiki state recorded in :class:`memtomem.context.lockfile.Lockfile`.

The compare rule is **strict** ``mtime > installed_at_epoch`` — only files
whose modification time is *strictly* later than the lockfile's
``installed_at`` are flagged dirty. Equality is clean. PR-D C2a (#630)
captures ``installed_at`` after :func:`copy_tree_atomic
<memtomem.context._atomic.copy_tree_atomic>` finishes, so the install's
own writes can never land at a strictly-later mtime than the captured
timestamp; the dirty classifier therefore can't false-positive on a fresh
install.

Skip rules mirror :data:`memtomem.context._atomic.COPY_SKIP_NAMES`:
``.git``, ``.DS_Store``, ``__pycache__`` are not part of the canonical
install surface, so user edits to such entries don't make the asset
dirty. Symlinks are skipped with a warning — ``copy_tree_atomic`` won't
mirror them into dest in the first place, and dereferencing one here
would defeat the byte-for-byte tree contract.

This module is read-only. The execute path (``mm context update``)
consumes a :class:`DirtyReport` and writes ``.bak`` files / overwrites
the dest tree separately.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from memtomem.context._atomic import COPY_SKIP_NAMES
from memtomem.context.lockfile import Lockfile

__all__ = [
    "DirtyReason",
    "DirtyReport",
    "is_asset_dirty",
]

logger = logging.getLogger(__name__)


DirtyReason = Literal["clean", "dirty", "never_installed", "missing_dest"]


@dataclass(frozen=True)
class DirtyReport:
    """Outcome of a dirty check on a single installed asset.

    - ``reason="clean"`` — dest exists and every checked file's mtime is
      ``<= installed_at_epoch``.
    - ``reason="dirty"`` — at least one checked file has
      ``mtime > installed_at_epoch``; the offending paths are in
      ``dirty_files``.
    - ``reason="never_installed"`` — no usable lockfile entry;
      ``installed_at`` is ``None`` and ``dirty_files`` / ``checked_files``
      are empty. Returned for both "no entry at all" and "entry exists
      but missing/non-string ``installed_at``" — both are unrecoverable
      states for a strict mtime compare.
    - ``reason="missing_dest"`` — lockfile entry exists but
      ``<project>/.memtomem/<type>/<name>/`` was deleted; ``installed_at``
      is the lockfile value, ``dirty_files`` empty.
    """

    reason: DirtyReason
    installed_at: str | None
    dirty_files: tuple[Path, ...]
    checked_files: int


def is_asset_dirty(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    lock_entry: dict[str, Any] | None = None,
) -> DirtyReport:
    """Classify an installed asset as clean / dirty / never_installed / missing_dest.

    Pure: walks the dest tree, reads file mtimes, returns a
    :class:`DirtyReport`. No writes, no lockfile mutations.

    ``lock_entry`` is optional caller injection — pass it when the caller
    already loaded the lockfile (e.g. ``mm context update --all``
    classification reuses one read across N projects). When omitted,
    we read the entry from ``<project>/.memtomem/lock.json``.
    """
    project_root_path = Path(project_root).expanduser()

    if lock_entry is None:
        lock_entry = Lockfile.at(project_root_path).read_entry(asset_type, name)

    if lock_entry is None:
        return DirtyReport(
            reason="never_installed",
            installed_at=None,
            dirty_files=(),
            checked_files=0,
        )

    installed_at = lock_entry.get("installed_at")
    if not isinstance(installed_at, str):
        return DirtyReport(
            reason="never_installed",
            installed_at=None,
            dirty_files=(),
            checked_files=0,
        )

    dest = project_root_path / ".memtomem" / asset_type / name
    if not dest.is_dir():
        return DirtyReport(
            reason="missing_dest",
            installed_at=installed_at,
            dirty_files=(),
            checked_files=0,
        )

    installed_at_epoch = datetime.fromisoformat(installed_at).timestamp()

    dirty: list[Path] = []
    checked = 0
    for file_path in _iter_files(dest):
        checked += 1
        if file_path.stat().st_mtime > installed_at_epoch:
            dirty.append(file_path)

    return DirtyReport(
        reason="dirty" if dirty else "clean",
        installed_at=installed_at,
        dirty_files=tuple(dirty),
        checked_files=checked,
    )


def _iter_files(root: Path) -> Iterator[Path]:
    """Yield non-skipped, non-symlink files under *root* recursively.

    Mirrors :func:`memtomem.context._atomic.copy_tree_atomic` traversal
    rules: skip entries named in :data:`COPY_SKIP_NAMES`, skip symlinks
    with a warning. Caller is responsible for the count semantics
    (:attr:`DirtyReport.checked_files` reflects yielded entries only).
    """
    for entry in root.iterdir():
        if entry.name in COPY_SKIP_NAMES:
            continue
        if entry.is_symlink():
            logger.warning("is_asset_dirty: skipping symlink %s", entry)
            continue
        if entry.is_file():
            yield entry
        elif entry.is_dir():
            yield from _iter_files(entry)
