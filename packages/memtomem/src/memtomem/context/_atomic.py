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

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Iterator

import portalocker

logger = logging.getLogger(__name__)

__all__ = [
    "COPY_SKIP_NAMES",
    "DIRTY_SKIP_SUFFIXES",
    "async_file_lock",
    "atomic_write_bytes",
    "atomic_write_text",
    "copy_tree_atomic",
    "installed_at_from_dest",
    "is_copy_skipped_rel",
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


# --- Memory-file cross-process CRUD serialization (issue #1587) -------------
#
# Lock-ordering invariant for the memory-file domain. Levels are acquired
# low→high; NEVER acquire a lower level while holding a higher one; within a
# level, multi-acquires MUST use sorted key order.
#
#   L0  debounce queue _Lock (indexing/debounce.py) — CLI hook path only.
#   L1  per-file asyncio.Lock — AppContext.get_memory_file_lock. Multi: sorted
#       keys. In-process, MCP-server-scope only.
#   L2  cross-process sidecar lock — ``async_file_lock(_lock_path_for(f))``.
#       Multi: sorted by str(lock_path) (memory-migrate). From ANY event loop
#       acquire ONLY via ``async_file_lock`` (never the blocking ``_file_lock``
#       synchronously on a loop): a sync ``LOCK_EX`` here can block the loop
#       while the current holder is a *suspended task on the same loop*
#       (portalocker/flock contends between fds within one process) = permanent
#       deadlock. ``async_file_lock`` is itself two-layered — an in-process
#       asyncio guard THEN the cross-process flock — because Windows
#       ``LockFileEx`` does not reliably block a second handle from one process
#       (same reason ``debounce._Lock`` #759 pairs a threading.Lock with the
#       flock). #1566: if the parent dir is gone, the sidecar is SKIPPED (never
#       mkdir-resurrect it); that decision is made once by the outermost
#       acquirer and flows down as ``index_file(lock_held=True)``.
#   L3  IndexEngine._index_lock. ``index_file(lock_held=True)`` asserts the
#       caller already holds (or #1566-skipped) this file's L2 sidecar and
#       enters at L3 directly; the sidecar is HOISTED above ``_index_lock`` so
#       no path ever acquires L2 while holding L3.
#   L4  storage / embedder / LLM — leaves; must never acquire L0–L3.
#
# Disjoint domains (config.json sidecar, web _gateway_lock/_config_lock) never
# nest with this memory-file domain.
#
# The context-artifact domain has its OWN two-level order (ADR-0030 §6,
# ``context/_canonical_txn.py``), also disjoint from this one:
#
#   C0  canonical name-keyed sidecar — ``<canonical_root>/.{name}.lock`` (the
#       layout-independent identity: flat ``<name>.md`` and dir ``<name>/``
#       share it). Multi: sorted by str(lock_path) (cross-scope transfer).
#       Held across resolve → snapshot → write so a concurrent flat→dir migrate
#       can't strand a stale-path write.
#   C1  the child sidecar the op needs — ``versions.json`` (version/label ops)
#       or the wiki ``lock.json`` (install/update). NEVER acquire a canonical
#       sidecar (C0) while holding a child (C1); the two children never nest
#       with each other. ``create_version``/``promote_label``/``Lockfile`` take
#       C1 internally, so callers hold C0 first.

# Per-hold-span acquisition budgets (seconds). Monkeypatchable by dotted path
# in tests, matching the ``config._CONFIG_LOCK_BUDGET_S`` convention. Fail-fast
# for interactive CRUD, longer for the internal reindex acquire (which may wait
# behind one CRUD span), longest for a whole migrate batch.
_CRUD_SIDECAR_LOCK_BUDGET_S: float = 5.0
_MEMORY_SIDECAR_LOCK_BUDGET_S: float = 10.0
_MIGRATE_SIDECAR_LOCK_BUDGET_S: float = 30.0

# Layer-1 (in-process) guard for ``async_file_lock``: per-lockfile-path, keyed
# by event loop. A bare module-level ``asyncio.Lock`` binds to the first loop
# that acquires it and then raises "bound to a different event loop" when reused
# from another loop — production runs one loop, but pytest gives each async test
# its own. Keying by (path, loop) keeps same-loop callers sharing one lock while
# distinct loops get distinct locks; closed loops are pruned on the new-loop
# path (a WeakKeyDictionary can't reclaim them — a contended lock strongly refs
# its bound loop). Mirrors ``web/routes/_locks._LoopLocalLock``, inlined here to
# keep ``context`` free of a ``web`` import.
_intra_async_locks: dict[str, dict[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _intra_async_lock_for(lock_path: Path) -> asyncio.Lock:
    """Return the in-process asyncio guard for *lock_path* on the running loop."""
    loop = asyncio.get_running_loop()
    per_loop = _intra_async_locks.setdefault(str(lock_path), {})
    lock = per_loop.get(loop)
    if lock is None:
        for dead in [lp for lp in per_loop if lp.is_closed()]:
            del per_loop[dead]
        lock = asyncio.Lock()
        per_loop[loop] = lock
    return lock


@asynccontextmanager
async def async_file_lock(lock_path: Path, *, timeout: float) -> AsyncIterator[None]:
    """Async, bounded, two-layer sidecar lock — the L2 primitive of the
    memory-file lock order above.

    Unlike :func:`_file_lock`, this never blocks the event loop: the
    cross-process ``portalocker`` acquire polls ``LOCK_NB`` and yields with
    ``await asyncio.sleep`` between attempts, so a task waiting here (or another
    process holding the sidecar) can never freeze the loop that a suspended
    lock-holder needs to make progress on. The whole acquisition is bounded by a
    single ``timeout`` budget shared across both layers; on expiry it raises
    ``TimeoutError`` having acquired nothing.

    Two layers, released in reverse order:

    1. **In-process** per-path ``asyncio.Lock`` (``_intra_async_lock_for``).
       Acquired first with the shared deadline. Required because Windows
       ``LockFileEx`` does not reliably block a second handle from the same
       process, so the flock alone would let two concurrent in-process handlers
       (e.g. two ``mm web`` chunk-edit requests) race. This is also what gives
       web/CLI callers — which hold no L1 ``AppContext`` lock — in-process
       serialization.
    2. **Cross-process** ``portalocker.LOCK_EX`` on the sidecar. Acquired
       second; serializes against other processes (a second server, the CLI,
       ``memory-migrate``).

    The caller passes the sidecar path (``_lock_path_for(data_file)``), not the
    data file. ``**Never**`` unlink the sidecar — deleting it reintroduces the
    ``os.replace`` inode race (see
    ``feedback_sidecar_lockfile_for_replaced_files``).
    """
    deadline = time.monotonic() + timeout
    intra = _intra_async_lock_for(lock_path)
    try:
        await asyncio.wait_for(intra.acquire(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        raise TimeoutError(
            f"could not acquire {lock_path} within {timeout:g}s (in-process contention)"
        ) from None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fp = os.fdopen(fd, "rb+")
        except BaseException:
            os.close(fd)
            raise
        try:
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
                    await asyncio.sleep(min(delay, remaining))
                    delay = min(delay * 2, 0.5)
            try:
                yield
            finally:
                portalocker.unlock(fp)
        finally:
            fp.close()
    finally:
        intra.release()


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
) -> dict[str, str]:
    """Recursively mirror *src* → *dst*, each file via :func:`atomic_write_bytes`.

    Returns ``{posix_relpath: sha256_hex}`` over the files written, relpaths
    relative to this call's *dst*. The digest is computed from the same
    in-memory ``bytes`` object handed to ``atomic_write_bytes`` — never from
    a re-read of *dst*, which would bless a concurrent edit landing between
    write and hash (the #1247 id 15 TOCTOU this map exists to close). The
    map IS the written set: ``len(map)`` == files written, skipped entries
    never appear. ``mode`` (default ``0o644``) is the permission bits
    applied to copied files — ``0o644`` matches the convention for content
    meant to be read by other tools (e.g. fan-out target runtimes), unlike
    the ``0o600`` default of ``atomic_write_bytes`` which is tuned for
    state files.

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
    digests: dict[str, str] = {}
    _copy_tree_collect(
        src,
        dst,
        "",
        digests,
        mode=mode,
        extra_skip=skip_top_level or frozenset(),
        skip_suffixes=skip_suffixes,
    )
    return digests


def _copy_tree_collect(
    src: Path,
    dst: Path,
    rel_prefix: str,
    digests: dict[str, str],
    *,
    mode: int,
    extra_skip: frozenset[str],
    skip_suffixes: frozenset[str],
) -> None:
    """Recursive body of :func:`copy_tree_atomic`.

    Threads the accumulating rel→digest map and the rel prefix (the old
    self-recursion restarted relpaths at each level, which was fine for a
    count but not for a map keyed by root-relative paths). ``extra_skip``
    is passed empty on recursion — ``skip_top_level`` is root-only by
    contract.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name in COPY_SKIP_NAMES or entry.name in extra_skip:
            continue
        if entry.suffix in skip_suffixes:
            continue
        if entry.is_symlink():
            logger.warning("copy_tree_atomic: skipping symlink %s", entry)
            continue
        target = dst / entry.name
        rel = f"{rel_prefix}{entry.name}"
        if entry.is_file():
            data = entry.read_bytes()
            atomic_write_bytes(target, data, mode=mode)
            digests[rel] = hashlib.sha256(data).hexdigest()
        elif entry.is_dir():
            _copy_tree_collect(
                entry,
                target,
                f"{rel}/",
                digests,
                mode=mode,
                extra_skip=frozenset(),
                skip_suffixes=skip_suffixes,
            )


def is_copy_skipped_rel(rel: str | PurePosixPath) -> bool:
    """True when :func:`copy_tree_atomic` (with ``DIRTY_SKIP_SUFFIXES``) would not mirror *rel*.

    Rel-path form of the walker skip rules, for callers that enumerate
    relative paths instead of dirents (the pinned-install scan and
    extraction over ``git ls-tree`` output, #1247). A rel is skipped when
    ANY path segment is named in :data:`COPY_SKIP_NAMES` or carries a
    suffix in :data:`DIRTY_SKIP_SUFFIXES` — matching the copier, which
    applies both checks to files and directories at every depth. Keep the
    three rule carriers in lockstep: this predicate,
    :func:`copy_tree_atomic`, and :func:`iter_installed_files` (symlink
    skipping is dirent-only and cannot be expressed on a rel string).
    """
    parts = PurePosixPath(rel).parts
    return any(
        part in COPY_SKIP_NAMES or PurePosixPath(part).suffix in DIRTY_SKIP_SUFFIXES
        for part in parts
    )


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

    Enumeration is FAIL-CLOSED by design: an unreadable directory or entry
    raises ``OSError`` rather than silently shrinking the result. The
    ``--force`` update/restore paths walk this to enumerate the dest files
    to preserve as ``.bak`` before overwriting, so a silently-dropped file
    would be clobbered with NO ``.bak`` — the same fail-open hole the
    skills-import scanner avoids with
    its own fail-closed walker. Callers that must instead survive an
    unreadable subtree (the read-only ``is_asset_dirty`` status walk) wrap the
    iteration in their own ``try``/``except OSError`` and degrade to "dirty"
    (cannot prove clean) — they do NOT push that policy down here, because
    doing so would relax the gate's fail-closed contract too.
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
