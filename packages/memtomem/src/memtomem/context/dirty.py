"""Detect drift between installed asset bytes and their lockfile snapshot.

Pure classifier used by ``mm context update`` to decide whether the
on-disk tree at ``<project>/.memtomem/<type>/<name>/`` still matches the
wiki state recorded in :class:`memtomem.context.lockfile.Lockfile`.

The compare rule is **strict** ``mtime > installed_at_epoch`` — only files
whose modification time is *strictly* later than the lockfile's
``installed_at`` are flagged dirty. Equality is clean. ``installed_at``
is captured from the filesystem itself by
:func:`memtomem.context._atomic.installed_at_from_dest` (``max
st_mtime_ns`` ceiled to microsecond), so on every platform the install's
own writes round-trip to a value ``<= installed_at_epoch`` and the
classifier can't false-positive on a fresh install — including Windows,
where NTFS ``FILETIME`` is a different timer from Python's wall clock
(#634).

Skip rules live in :func:`memtomem.context._atomic.iter_installed_files`
(shared with the capture helper above): ``.git``, ``.DS_Store``,
``__pycache__``, ``.bak`` and symlinks are not part of the canonical
install surface, so user edits to such entries don't make the asset
dirty and don't perturb the captured ``installed_at`` either.

This module is read-only. The execute path (``mm context update``)
consumes a :class:`DirtyReport` and writes ``.bak`` files / overwrites
the dest tree separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from memtomem.context._atomic import iter_installed_files
from memtomem.context.lockfile import Lockfile

__all__ = [
    "DirtyReason",
    "DirtyReport",
    "is_asset_dirty",
]


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
    for file_path in iter_installed_files(dest):
        checked += 1
        if file_path.stat().st_mtime > installed_at_epoch:
            dirty.append(file_path)

    return DirtyReport(
        reason="dirty" if dirty else "clean",
        installed_at=installed_at,
        dirty_files=tuple(dirty),
        checked_files=checked,
    )
