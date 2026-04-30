"""Atomic write primitives for context-gateway fan-out targets.

A crash, SIGKILL, or OOM between the truncate and the flush of a plain
``Path.write_text`` leaves the target file empty or half-written — which, for
``~/.claude/settings.json`` or ``.claude/agents/<name>.md``, reloads on the
next runtime start as "no hooks / no agents configured". Every gateway write
site funnels through the helpers in this module so the worst a crash can do
is leave a ``.<name>.*.tmp`` sibling that the next successful write will
overwrite.

The pattern is ``tempfile.mkstemp`` in the same directory + ``os.replace``,
which is an atomic rename on POSIX and Windows.

Threat model is **accidental** (crash / kill), not adversarial — the
``context/`` package is the boundary where that hardening lives.

See also: :func:`memtomem.config._atomic_write_json`, which predates this
module and covers ``~/.memtomem/config.json`` specifically.
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "COPY_SKIP_NAMES",
    "atomic_write_bytes",
    "atomic_write_text",
    "copy_tree_atomic",
]


COPY_SKIP_NAMES: frozenset[str] = frozenset({".git", ".DS_Store", "__pycache__"})
"""Entry names :func:`copy_tree_atomic` refuses to mirror.

- ``.git`` — wiki asset trees should never carry a nested git dir, but a
  stray one would otherwise get copied verbatim into ``<project>/.memtomem/``.
- ``.DS_Store`` — macOS Finder side-effect; quietly skipped so wikis stay
  clean even when curated through the GUI.
- ``__pycache__`` — Python bytecode caches that test/automation runs may
  drop into a wiki tree; never wanted in a project's canonical surface.
"""


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Cross-process exclusive lock on a sidecar lockfile.

    Locking the data file directly does **not** survive ``os.replace`` —
    the lock is on the inode, and the rename swaps the inode mid-operation
    so concurrent writers race on stale fds. The fix is to lock a sibling
    (``feedback_sidecar_lockfile_for_replaced_files.md``, PR #548). The
    lockfile itself is never renamed, so its inode is stable.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _lock_path_for(data_path: Path) -> Path:
    """Sidecar lockfile path for *data_path* (``.{name}.lock`` next to it)."""
    return data_path.parent / f".{data_path.name}.lock"


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write *data* to *path* with an explicit file mode.

    ``mode`` is applied via ``os.fchmod`` on the tempfile before the rename,
    so the result is independent of the process umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(tmp_fd, mode)
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    mode: int = 0o600,
    encoding: str = "utf-8",
) -> None:
    """Atomically write *text* to *path* with an explicit file mode."""
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def copy_tree_atomic(src: Path, dst: Path, *, mode: int = 0o644) -> int:
    """Recursively mirror *src* → *dst*, each file via :func:`atomic_write_bytes`.

    Returns the number of files written. ``mode`` (default ``0o644``) is the
    permission bits applied to copied files — ``0o644`` matches the
    convention for content meant to be read by other tools (e.g. fan-out
    target runtimes), unlike the ``0o600`` default of ``atomic_write_bytes``
    which is tuned for state files.

    Entries named in :data:`COPY_SKIP_NAMES` are skipped silently. Symlinks
    are skipped with a warning — this helper promises a *byte-for-byte tree
    mirror*, and silently dereferencing a symlink to ``/etc/passwd`` (or any
    out-of-tree target) would violate that contract. Callers who want to
    mirror symlinks must do so explicitly.
    """
    dst.mkdir(parents=True, exist_ok=True)
    written = 0
    for entry in src.iterdir():
        if entry.name in COPY_SKIP_NAMES:
            continue
        if entry.is_symlink():
            logger.warning("copy_tree_atomic: skipping symlink %s", entry)
            continue
        target = dst / entry.name
        if entry.is_file():
            atomic_write_bytes(target, entry.read_bytes(), mode=mode)
            written += 1
        elif entry.is_dir():
            written += copy_tree_atomic(entry, target, mode=mode)
    return written
