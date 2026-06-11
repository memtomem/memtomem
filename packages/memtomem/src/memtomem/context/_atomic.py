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

import logging
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import portalocker

logger = logging.getLogger(__name__)

__all__ = [
    "COPY_SKIP_NAMES",
    "DIRTY_SKIP_SUFFIXES",
    "atomic_write_bytes",
    "atomic_write_text",
    "copy_tree_atomic",
    "installed_at_from_dest",
    "iter_installed_files",
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


DIRTY_SKIP_SUFFIXES: frozenset[str] = frozenset({".bak"})
"""Suffix patterns the installed-file walker excludes.

Shared by :func:`iter_installed_files` (the filter for
:func:`memtomem.context.dirty.is_asset_dirty` and the install-time
capture helper) and, via ``skip_suffixes``, by the wiki-install
``copy_tree_atomic`` call sites — so the installed surface equals the
tracked surface and a wiki-shipped ``*.bak`` can neither dodge dirty
tracking nor collide with the ``--force`` preservation namespace
(#1247).

``.bak`` — sibling files created by ``mm context update --force`` to
preserve user edits before overwriting with wiki bytes. They live in the
dest tree by design and carry the user's pre-update mtime, so without
this skip they would trip the next ``mm context update`` into
``reason="dirty"`` purely on the prior backup, refusing every future
update until the user manually deletes the ``.bak``.
"""


@contextmanager
def _file_lock(lock_path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """Cross-process exclusive lock on a sidecar lockfile.

    Locking the data file directly does **not** survive ``os.replace`` —
    the lock is on the inode, and the rename swaps the inode mid-operation
    so concurrent writers race on stale fds. The fix is to lock a sibling
    (``feedback_sidecar_lockfile_for_replaced_files.md``, PR #548). The
    lockfile itself is never renamed, so its inode is stable.

    Cross-platform via ``portalocker`` (POSIX ``fcntl.flock`` / Windows
    ``LockFileEx``). Locks are per-open-file-description, so two acquisitions
    in the *same* process (separate ``os.open`` fds) still contend — which is
    what makes the in-process contention tests meaningful.

    ``timeout`` (seconds): ``None`` (default) blocks indefinitely on
    ``LOCK_EX``, matching POSIX semantics — correct for the narrow
    ``load → mutate → atomic_write_bytes`` windows in
    :mod:`memtomem.context.lockfile` / :mod:`memtomem.context.projects`. Pass a
    bound when the lock is acquired from a context that must not block forever
    — e.g. an async web handler offloading to a worker thread, where an
    unbounded wait would leave an un-cancellable thread writing after the
    handler's own timeout already returned (#1145 review). On expiry we raise
    ``TimeoutError`` having acquired nothing, so the caller can surface a
    clean "aborted, retry" instead of orphaning the thread.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # os.open + os.fdopen: pin 0o600 mode while still handing portalocker
    # a file object — its Windows backend calls .fileno() on the argument,
    # so a bare fd int won't do.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fp = os.fdopen(fd, "rb+")
    except BaseException:
        os.close(fd)
        raise
    try:
        if timeout is None:
            portalocker.lock(fp, portalocker.LOCK_EX)
        else:
            # Non-blocking poll with exponential backoff until the deadline.
            # On expiry we have NOT acquired the lock (every attempt used
            # ``LOCK_NB``), so raising here skips the ``yield``/``unlock`` pair
            # below — no spurious unlock of a lock we never held.
            deadline = time.monotonic() + timeout
            delay = 0.05
            while True:
                try:
                    portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    break
                except portalocker.LockException:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"could not acquire {lock_path} within {timeout:g}s "
                            f"(held by another process)"
                        ) from None
                    time.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.5)
        try:
            yield
        finally:
            portalocker.unlock(fp)
    finally:
        fp.close()


def _lock_path_for(data_path: Path) -> Path:
    """Sidecar lockfile path for *data_path* (``.{name}.lock`` next to it)."""
    return data_path.parent / f".{data_path.name}.lock"


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write *data* to *path* with an explicit file mode.

    ``mode`` is applied via ``os.fchmod`` on the tempfile before the rename
    where available, so the result is independent of the process umask.
    Windows Python < 3.13 lacks ``os.fchmod``; on those interpreters the
    file is created with the process default permissions, which NTFS
    largely ignores beyond the read-only flag. The user-private intent of
    ``mode=0o600`` for state files (e.g. ``~/.memtomem/config.json``) is
    preserved on Windows in practice via NTFS ACL inheritance from
    user-private parents like ``%LOCALAPPDATA%`` — the on-disk ACL is
    user-only by default in those locations, providing functionally
    equivalent access control.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
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


def copy_tree_atomic(
    src: Path,
    dst: Path,
    *,
    mode: int = 0o644,
    skip_top_level: frozenset[str] | None = None,
    skip_suffixes: frozenset[str] = frozenset(),
) -> int:
    """Recursively mirror *src* → *dst*, each file via :func:`atomic_write_bytes`.

    Returns the number of files written. ``mode`` (default ``0o644``) is the
    permission bits applied to copied files — ``0o644`` matches the
    convention for content meant to be read by other tools (e.g. fan-out
    target runtimes), unlike the ``0o600`` default of ``atomic_write_bytes``
    which is tuned for state files.

    Entries named in :data:`COPY_SKIP_NAMES` are skipped silently at every
    depth. ``skip_top_level`` names additional entries skipped ONLY at the
    root of this call (it is deliberately not propagated into the recursion),
    so e.g. a skill's top-level ``overrides/`` source can be excluded without
    also dropping a legitimate nested ``scripts/overrides/``. ``skip_suffixes``
    excludes entries by suffix at every depth — the wiki-install copies pass
    :data:`DIRTY_SKIP_SUFFIXES` so the installed surface equals the tracked
    surface (a wiki-shipped ``*.bak`` would be invisible to the dirty walk,
    the manifest, and reconcile, and would collide with the ``--force``
    preservation namespace; #1247). The skills staging mirror deliberately
    passes nothing — its diff parity compares full trees. Skipping during
    the copy (rather than deleting afterwards) means those bytes never touch
    *dst* — no crash window where they exist, no silent leak if a later delete
    fails. Symlinks are skipped with a warning — this helper promises a
    *byte-for-byte tree mirror*, and silently dereferencing a symlink to
    ``/etc/passwd`` (or any out-of-tree target) would violate that contract.
    Callers who want to mirror symlinks must do so explicitly.
    """
    extra_skip = skip_top_level or frozenset()
    dst.mkdir(parents=True, exist_ok=True)
    written = 0
    for entry in src.iterdir():
        if entry.name in COPY_SKIP_NAMES or entry.name in extra_skip:
            continue
        if entry.suffix in skip_suffixes:
            continue
        if entry.is_symlink():
            logger.warning("copy_tree_atomic: skipping symlink %s", entry)
            continue
        target = dst / entry.name
        if entry.is_file():
            atomic_write_bytes(target, entry.read_bytes(), mode=mode)
            written += 1
        elif entry.is_dir():
            # skip_top_level intentionally not threaded into the recursion.
            written += copy_tree_atomic(entry, target, mode=mode, skip_suffixes=skip_suffixes)
    return written


def iter_installed_files(root: Path) -> Iterator[Path]:
    """Yield non-skipped, non-symlink files under *root* recursively.

    Mirrors :func:`copy_tree_atomic` traversal rules: skip entries named
    in :data:`COPY_SKIP_NAMES`, skip suffixes in
    :data:`DIRTY_SKIP_SUFFIXES`, skip symlinks with a warning. Shared by
    :func:`memtomem.context.dirty.is_asset_dirty` (the dirty walker) and
    :func:`installed_at_from_dest` (the install-timestamp capture
    helper), so both consume the exact same set of files —
    ``installed_at`` cannot reference a file the dirty-checker will
    later ignore, and vice versa.
    """
    for entry in root.iterdir():
        if entry.name in COPY_SKIP_NAMES:
            continue
        if entry.suffix in DIRTY_SKIP_SUFFIXES:
            continue
        if entry.is_symlink():
            logger.warning("iter_installed_files: skipping symlink %s", entry)
            continue
        if entry.is_file():
            yield entry
        elif entry.is_dir():
            yield from iter_installed_files(entry)


def installed_at_from_dest(dst: Path) -> str:
    """Return ISO-8601Z timestamp >= max(st_mtime) of files under *dst*.

    Two-layer fix for the Windows dirty-cluster (#634):

    1. **Source** — read ``st_mtime_ns`` (int, lossless) from the
       filesystem rather than Python's wall clock. NTFS records mtimes
       from ``FILETIME``, a different timer from ``time.time()``;
       just-written files can carry mtimes strictly later than a
       wall-clock-captured timestamp, breaking the strict
       ``mtime > installed_at_epoch`` invariant in
       :func:`memtomem.context.dirty.is_asset_dirty`.
    2. **Precision** — ceiling-divide ``st_mtime_ns`` to microseconds
       before formatting. ISO-8601Z's ``%f`` directive is microsecond
       only, so truncating NTFS's 100-ns residual would leave the
       formatted ``installed_at`` up to 1µs **less than** some files'
       round-tripped mtimes, defeating the same invariant.

    Combined: the formatted timestamp round-trips through
    ``datetime.fromisoformat().timestamp()`` to a value ``>=`` every
    walked file's ``st_mtime`` — byte-identical to today on POSIX
    (where ``st_mtime_ns % 1000 == 0`` for ordinary writes; ceil is a
    no-op) and with a ``<= 1µs`` safety margin on NTFS.

    The walker is :func:`iter_installed_files`, so capture and
    dirty-check observe the same file set: a file the dirty-checker
    will later ignore (``.git``, ``.DS_Store``, ``__pycache__``,
    ``.bak``, symlinks) cannot bump the captured timestamp, and a
    file we walk here is one the dirty-checker will walk later.

    Empty install (0 files) — fall back to wall clock. With no
    filesystem source there is nothing to skew off, so the format
    still matches :func:`memtomem.context.lockfile.utcnow_iso8601_z`
    byte-for-byte. The strftime call is duplicated here rather than
    importing the helper to avoid a circular import (``lockfile``
    already imports from ``_atomic``).
    """
    files = list(iter_installed_files(dst))
    if not files:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    max_ns = max(p.stat().st_mtime_ns for p in files)
    max_us = -(-max_ns // 1000)  # math.ceil(max_ns / 1000) without the import
    return datetime.fromtimestamp(max_us / 1_000_000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
