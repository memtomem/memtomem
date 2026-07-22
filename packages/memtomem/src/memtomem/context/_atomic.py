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
import errno
import hashlib
import logging
import os
import stat
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Callable, Iterator

import portalocker

logger = logging.getLogger(__name__)

__all__ = [
    "COPY_SKIP_NAMES",
    "DIRTY_SKIP_SUFFIXES",
    "StrictTreeError",
    "async_file_lock",
    "atomic_write_bytes",
    "atomic_write_text",
    "copy_tree_atomic",
    "copy_tree_strict",
    "fsync_dir",
    "hardlink_tree_strict",
    "installed_at_from_dest",
    "is_copy_skipped_rel",
    "iter_installed_files",
    "link_or_copy_file",
    "rename_no_replace",
    "validate_tree_strict",
    "write_tree_payload",
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


def _fsync_fd(fd: int, *, full: bool) -> None:
    """``os.fsync`` on *fd*, upgraded to ``F_FULLFSYNC`` on macOS when *full*.

    Darwin's ``fsync(2)`` hands the data to the drive but does NOT flush the
    drive's own write cache, so a power loss can lose bytes an ``fsync``
    already acknowledged. ``F_FULLFSYNC`` is Apple's documented barrier for
    "must survive a power cut". It is not implemented on every filesystem
    (``ENOTSUP``/``EINVAL`` on some network mounts), so a rejection degrades
    to the plain ``fsync`` we would have done anyway rather than failing a
    write whose data is already correct.

    Only meaningful for FILE descriptors — see :func:`fsync_dir` for the
    directory-entry barrier, which deliberately never asks for ``F_FULLFSYNC``.
    """
    if full and sys.platform == "darwin":
        import fcntl

        # <sys/fcntl.h>: F_FULLFSYNC = 51. Not exposed by Python's fcntl on
        # every build, so read it defensively.
        f_fullfsync = getattr(fcntl, "F_FULLFSYNC", 51)
        try:
            fcntl.fcntl(fd, f_fullfsync, 0)
            return
        except OSError as exc:
            logger.debug("F_FULLFSYNC unavailable (%s); falling back to fsync", exc)
    os.fsync(fd)


def fsync_dir(path: Path) -> bool:
    """Best-effort durable flush of a DIRECTORY entry. ``True`` iff flushed.

    A ``rename`` into a directory is only durable once that directory's own
    entry reaches stable storage; without this barrier a power cut can leave a
    freshly promoted tree invisible even though every byte inside it was
    fsynced. Callers that promote by rename (the version-store snapshot) call
    this on the parent afterwards.

    **Never raises.** Windows cannot open a directory for ``fsync`` at all, and
    network/tmpfs mounts may reject it (``EINVAL``/``EPERM``/``EACCES``/
    ``ENOTSUP``/``EBADF``); there the guarantee degrades to process-crash
    consistency — exactly the posture :func:`atomic_write_bytes` already ships
    with. Returning a bool instead of raising is deliberate: the rename has
    already succeeded, and aborting a completed, correct operation because we
    could not *prove* its durability would trade a real failure for a
    hypothetical one. Tests assert the return value; production callers ignore
    it.

    Deliberately never ``F_FULLFSYNC``: it is defined for files and ``EINVAL``s
    on directory descriptors on several Darwin filesystems.
    """
    if sys.platform == "win32":
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        logger.debug("fsync_dir: cannot open %s (%s)", path, exc)
        return False
    try:
        os.fsync(fd)
        return True
    except OSError as exc:
        logger.debug("fsync_dir: fsync rejected for %s (%s)", path, exc)
        return False
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path, data: bytes, mode: int = 0o600, *, full_fsync: bool = False
) -> None:
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

    ``full_fsync`` (default OFF, so every existing call site keeps its current
    bytes and latency) upgrades the pre-rename flush to ``F_FULLFSYNC`` on
    macOS for content that must survive a power cut rather than merely a
    process crash — the immutable version snapshots, which are the only copy
    of history a later Pull can roll back to.
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
            _fsync_fd(f.fileno(), full=full_fsync)
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
    skip_top_level_pred: Callable[[str], bool] | None = None,
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
    also dropping a legitimate nested ``scripts/overrides/``.
    ``skip_top_level_pred`` is the same root-only exclusion expressed as a
    predicate over the entry NAME (skip when it returns ``True``), for callers
    whose exclusion set is not a fixed name list — the skills fan-out skips the
    version store's ``.versions.json.<rand>.tmp`` siblings, which no frozenset
    can enumerate, and pre-listing ``src`` to build one would leave a TOCTOU
    window against a concurrent version write. Both may be passed; an entry is
    skipped if either matches. ``skip_suffixes``
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
        extra_skip_pred=skip_top_level_pred,
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
    extra_skip_pred: Callable[[str], bool] | None = None,
    skip_suffixes: frozenset[str],
) -> None:
    """Recursive body of :func:`copy_tree_atomic`.

    Threads the accumulating rel→digest map and the rel prefix (the old
    self-recursion restarted relpaths at each level, which was fine for a
    count but not for a map keyed by root-relative paths). ``extra_skip``
    and ``extra_skip_pred`` are passed empty/``None`` on recursion —
    ``skip_top_level``/``skip_top_level_pred`` are root-only by contract.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name in COPY_SKIP_NAMES or entry.name in extra_skip:
            continue
        if extra_skip_pred is not None and extra_skip_pred(entry.name):
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
                extra_skip_pred=None,
                skip_suffixes=skip_suffixes,
            )


def _validate_payload_relpath(rel: str) -> PurePosixPath:
    """Validate one ``write_tree_payload`` relpath or raise ``ValueError``.

    Containment lives at the write primitive so no caller can forget it: every
    relpath must be a non-empty POSIX-relative path with no ``..`` / ``.`` /
    empty segment, no backslash and no colon-bearing segment.

    The last two are cross-platform containment, not pedantry. A backslash is a
    legal filename character on POSIX but a separator on Windows, so accepting
    one would land the same payload as a single file here and a nested tree
    there. A colon segment looks like an ordinary (if odd) directory name to
    ``PurePosixPath``, but Windows reads ``C:escape.txt`` as a DRIVE-RELATIVE
    path and ``joinpath`` discards the destination base entirely — the write
    escapes with no ``..`` anywhere in the string (see
    :data:`_COLON_SEGMENT_RE`). Both are refused on every platform, so a
    payload that validates here is safe everywhere — the check must not become
    ``sys.platform``-conditional, or a payload built on Linux would land
    unvalidated on Windows.
    """
    if not rel:
        raise ValueError("payload relpath is empty")
    if "\0" in rel:
        # No OS accepts a NUL in a filename, so this would raise from deep
        # inside the write loop — AFTER earlier entries already landed, which
        # is precisely the half-populated destination this preflight exists to
        # prevent. Reject it here, where nothing has been written yet.
        raise ValueError(f"payload relpath {rel!r} contains a NUL byte")
    if "\\" in rel:
        raise ValueError(f"payload relpath {rel!r} contains a backslash")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or pure.drive or pure.root:
        raise ValueError(f"payload relpath {rel!r} is not relative")
    parts = pure.parts
    if not parts:
        raise ValueError(f"payload relpath {rel!r} has no path segments")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"payload relpath {rel!r} contains a '.' or '..' segment")
    # ANY colon anywhere in a segment, deliberately broader than a ``^[A-Za-z]:$``
    # drive test. Windows also honors the DRIVE-RELATIVE form ``C:escape.txt``
    # (no separator, no ``..``), where ``joinpath`` discards the destination base
    # outright; ``file:stream`` is an NTFS alternate-data-stream reference rather
    # than a filename. A colon is not a legal filename character on Windows at
    # all, so refusing the whole class costs nothing and closes both holes.
    if any(":" in part for part in parts):
        raise ValueError(f"payload relpath {rel!r} contains a ':' in a path segment")
    # ``PurePosixPath`` collapses ``a//b`` and a trailing slash; compare against
    # the round-trip so a caller cannot smuggle a non-canonical form whose
    # written path differs from the key it believes it wrote.
    if pure.as_posix() != rel:
        raise ValueError(f"payload relpath {rel!r} is not canonical (got {pure.as_posix()!r})")
    return pure


def write_tree_payload(
    dst_dir: Path,
    payload: Sequence[tuple[str, bytes]],
    *,
    mode: int = 0o644,
    durable: bool = False,
) -> None:
    """Materialize a ``(posix_relpath, bytes)`` payload under *dst_dir*.

    The captured-bytes twin of :func:`copy_tree_atomic`: it writes the bytes
    the caller already read and judged (privacy-scanned, digested), never
    re-reading a source that a concurrent editor could have changed since. That
    is what lets a version snapshot promise "these exact bytes" — and why the
    snapshot path uses this instead of a tree copy.

    Files land through :func:`atomic_write_bytes` at *mode* (``0o644`` — the
    copier's content mode, which is also why the ADR-0030 §10 tree digest
    excludes the executable bit: preserving a bit the copier drops would make
    digests unreproducible).

    EVERY relpath is validated (see :func:`_validate_payload_relpath`) and
    duplicates rejected BEFORE anything is written, so a bad payload leaves
    *dst_dir* untouched rather than half-populated.

    ``durable=True`` adds the power-cut barrier: ``full_fsync`` per file, then
    an ``fsync`` of every directory this call created, deepest first, and
    finally *dst_dir* — so the file entries are on stable storage before the
    caller renames the tree into place. Directory-fsync failures degrade
    silently (:func:`fsync_dir`).
    """
    seen: set[str] = set()
    validated: list[tuple[PurePosixPath, bytes]] = []
    for rel, data in payload:
        pure = _validate_payload_relpath(rel)
        if rel in seen:
            raise ValueError(f"duplicate payload relpath {rel!r}")
        seen.add(rel)
        validated.append((pure, data))

    dst_dir.mkdir(parents=True, exist_ok=True)
    created_dirs: set[Path] = set()
    for pure, data in validated:
        target = dst_dir.joinpath(*pure.parts)
        # EVERY intermediate ancestor, not just the immediate parent: for
        # ``a/b/c.md``, syncing only ``a/b`` leaves ``a``'s entry for ``b``
        # unflushed, so a power cut can lose ``b`` (and everything under it)
        # from a tree the caller has already been told is complete.
        for depth in range(1, len(pure.parts)):
            created_dirs.add(dst_dir.joinpath(*pure.parts[:depth]))
        atomic_write_bytes(target, data, mode=mode, full_fsync=durable)

    if not durable:
        return
    # Deepest first: a child's entry must be durable before the parent that
    # names it, or a power cut could leave a synced parent pointing at an
    # unsynced child.
    for directory in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
        fsync_dir(directory)
    fsync_dir(dst_dir)


class StrictTreeError(Exception):
    """A tree an exclusive transaction must carry contains an entry it refuses
    to move: a symlink (even to a directory) or a non-regular special file
    (FIFO, socket, device node), at any depth.

    Deliberately NOT an ``OSError``: a caller maps it to a domain refusal
    (``target_conflict``, naming the offender) while still catching genuine
    I/O errors — disk full, ``EACCES`` — as a separate ``write_failed``. If it
    subclassed ``OSError`` the two would collapse into one ``except`` and the
    wrong result would ship. Carries the offending path.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{detail}: {path}")
        #: The first offending entry, so a refusal can name it.
        self.path = path


#: ``os.link`` errnos that mean "linking is impossible here, copy instead" —
#: a cross-device link (``EXDEV``, which a Linux bind mount returns even though
#: it reports the SAME ``st_dev``, so an ``st_dev`` pre-check would wrongly
#: attempt the link) or a filesystem that forbids hardlinks outright
#: (``EPERM`` on some FUSE/overlay mounts, ``ENOTSUP`` / ``EOPNOTSUPP``).
_HARDLINK_FALLBACK_ERRNOS: frozenset[int] = frozenset(
    {errno.EXDEV, errno.EPERM, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
)


def _full_fsync_file(path: Path) -> None:
    """``F_FULLFSYNC`` (macOS) / ``fsync`` a file so its bytes survive a power cut.

    Opened ``O_RDWR``, not ``O_RDONLY``: on Windows ``os.fsync`` is
    ``_commit`` → ``FlushFileBuffers``, which requires a writable handle
    (``GENERIC_WRITE``) and raises ``PermissionError`` on a read-only one. The
    only caller (:func:`link_or_copy_file`'s copy fallback) has just created a
    fresh file it owns, so requesting write access is always available and
    changes nothing on POSIX.
    """
    fd = os.open(path, os.O_RDWR)
    try:
        _fsync_fd(fd, full=True)
    finally:
        os.close(fd)


def link_or_copy_file(src: Path, dst: Path, *, durable: bool = False) -> None:
    """Hardlink *src* → *dst*, falling back to :func:`shutil.copy2` on a
    cross-device or link-unsupported errno.

    NO ``st_dev`` pre-check: a Linux bind mount reports the same ``st_dev`` as
    its origin yet ``os.link`` across it returns ``EXDEV``, so the only reliable
    signal is the syscall's own errno. Any other ``OSError`` propagates.

    A hardlink needs no per-file flush — it shares the source inode, whose data
    is already durable. The COPY fallback writes a fresh inode, so ``durable``
    fsyncs it: without that, a caller that fsyncs only directories afterward
    (the version-store carry) could lose the copied bytes to a power cut once
    the original is deleted by the swap.
    """
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno in _HARDLINK_FALLBACK_ERRNOS:
            import shutil

            shutil.copy2(src, dst)
            if durable:
                _full_fsync_file(dst)
        else:
            raise


def _require_strict_root(root: Path) -> None:
    """Depth-zero guard for the strict walkers: the root itself must be a real
    directory, not a symlink.

    The recursive walks below only ``lstat`` a root's CHILDREN, so a symlinked
    root would be followed and its target walked — silently escaping the tree
    the caller named, exactly the traversal the strict posture exists to
    prevent. Every public strict walker calls this first.
    """
    mode = os.lstat(root).st_mode
    if stat.S_ISLNK(mode):
        raise StrictTreeError(root, "refusing to walk a symlinked root")
    if not stat.S_ISDIR(mode):
        raise StrictTreeError(root, "strict walk root is not a directory")


def validate_tree_strict(root: Path) -> None:
    """Read-only preflight: raise :class:`StrictTreeError` if *root* — or any
    entry beneath it — is a symlink or a non-regular special file.

    Directories and regular files pass; a symlink (even to a directory), FIFO,
    socket or device node is refused, naming the first offender. Run BEFORE an
    exclusive carry that has already mutated the store (e.g. taken a version
    snapshot), so an offending nested entry is discovered before — not during —
    the copy. The strict copiers below re-check as they walk; this pass exists
    solely so the first discovery is read-only. Propagates ``OSError`` (fail
    closed on an unreadable subtree).
    """
    _require_strict_root(root)
    _validate_tree_strict(root)


def _validate_tree_strict(root: Path) -> None:
    for entry in sorted(root.iterdir()):
        mode = os.lstat(entry).st_mode
        if stat.S_ISLNK(mode):
            raise StrictTreeError(entry, "refusing to carry a symlink")
        if stat.S_ISDIR(mode):
            _validate_tree_strict(entry)
        elif not stat.S_ISREG(mode):
            raise StrictTreeError(entry, "refusing to carry a non-regular file")


def copy_tree_strict(src: Path, dst: Path, *, mode: int = 0o644, durable: bool = False) -> None:
    """Deep-copy *src* → *dst* into NEW inodes, REFUSING any symlink or special file.

    Unlike :func:`copy_tree_atomic`, which SKIPS symlinks with a warning — the
    right posture for best-effort fan-out mirroring where the source is
    retained — this REFUSES them (:class:`StrictTreeError`). For a
    carry-then-delete transaction a silently skipped ``overrides/`` entry would
    be a silent DELETION of the user's edit once the source is removed. New
    inodes (not hardlinks) because the copy is a live, editable tree.

    ``durable`` upgrades each file write to ``full_fsync`` and fsyncs every
    directory this call created, deepest first — so the tree is on stable
    storage before a caller renames it into place. Raises
    :class:`StrictTreeError` (offending entry, or a symlinked root) or ``OSError``.
    """
    _require_strict_root(src)
    dst.mkdir(parents=True, exist_ok=True)
    created: list[Path] = [dst]
    _copy_tree_strict(src, dst, mode=mode, durable=durable, created=created)
    if durable:
        for directory in sorted(created, key=lambda p: len(p.parts), reverse=True):
            fsync_dir(directory)


def _copy_tree_strict(
    src: Path, dst: Path, *, mode: int, durable: bool, created: list[Path]
) -> None:
    for entry in sorted(src.iterdir()):
        st = os.lstat(entry)
        target = dst / entry.name
        if stat.S_ISLNK(st.st_mode):
            raise StrictTreeError(entry, "refusing to carry a symlink")
        if stat.S_ISDIR(st.st_mode):
            target.mkdir(exist_ok=True)
            created.append(target)
            _copy_tree_strict(entry, target, mode=mode, durable=durable, created=created)
        elif stat.S_ISREG(st.st_mode):
            atomic_write_bytes(target, entry.read_bytes(), mode=mode, full_fsync=durable)
        else:
            raise StrictTreeError(entry, "refusing to carry a non-regular file")


def hardlink_tree_strict(src: Path, dst: Path, *, durable: bool = False) -> None:
    """Recreate *src*'s directory structure under *dst*, HARDLINKING each file.

    History is immutable, so linking avoids copying a version store that grows
    without bound. Directories cannot be hardlinked (``EPERM``), so they are
    recreated and their files linked via :func:`link_or_copy_file` (which falls
    back to a copy on a cross-device / link-unsupported errno). Refuses symlinks
    and special files (:class:`StrictTreeError`), like :func:`copy_tree_strict`.

    ``durable`` fsyncs every directory this call created, deepest first, and —
    for any file that took the :func:`link_or_copy_file` COPY fallback — the
    copied inode itself. A true hardlink needs no per-file fsync (it shares an
    already durable source inode); only the new directory ENTRIES must be
    flushed.
    """
    _require_strict_root(src)
    dst.mkdir(parents=True, exist_ok=True)
    created: list[Path] = [dst]
    _hardlink_tree_strict(src, dst, created=created, durable=durable)
    if durable:
        for directory in sorted(created, key=lambda p: len(p.parts), reverse=True):
            fsync_dir(directory)


def _hardlink_tree_strict(src: Path, dst: Path, *, created: list[Path], durable: bool) -> None:
    for entry in sorted(src.iterdir()):
        st = os.lstat(entry)
        target = dst / entry.name
        if stat.S_ISLNK(st.st_mode):
            raise StrictTreeError(entry, "refusing to carry a symlink")
        if stat.S_ISDIR(st.st_mode):
            target.mkdir(exist_ok=True)
            created.append(target)
            _hardlink_tree_strict(entry, target, created=created, durable=durable)
        elif stat.S_ISREG(st.st_mode):
            link_or_copy_file(entry, target, durable=durable)
        else:
            raise StrictTreeError(entry, "refusing to carry a non-regular file")


def rename_no_replace(staging: Path, dst: Path) -> None:
    """Atomically rename ``staging`` to an absent ``dst`` or fail closed.

    Directory promotion needs an OS no-replace primitive: plain POSIX
    :func:`os.rename` may replace an empty destination directory, leaving a
    shell/editor writer's ``mkdir`` → ``SKILL.md`` sequence vulnerable. Linux
    and macOS expose the required flag only through native APIs, so call those
    lazily via :mod:`ctypes`; Windows :func:`os.rename` is already exclusive.

    A missing native symbol or unsupported platform/filesystem is loud
    ``ENOTSUP``. Never degrade to ``exists()`` + :func:`os.replace`, which
    would recreate the race this helper closes (#1839).

    Lives here rather than in ``skills.py`` because the version store's
    write-once snapshot promote needs the identical primitive; a second copy is
    exactly how one call site would silently lose exclusivity.
    """
    if staging.parent != dst.parent:
        raise OSError(
            errno.EXDEV,
            "atomic no-replace promote requires a shared parent directory",
            str(dst),
        )

    if sys.platform == "win32":
        # Python's Windows contract refuses every existing destination.
        os.rename(staging, dst)
        return

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    staging_bytes = os.fsencode(staging)
    dst_bytes = os.fsencode(dst)
    unsupported_errno = getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)

    if sys.platform.startswith("linux"):
        try:
            rename = getattr(libc, "renameat2")
        except AttributeError:
            raise OSError(
                unsupported_errno,
                "atomic no-replace directory rename is unavailable",
                str(dst),
            ) from None
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        # Linux <fcntl.h>: AT_FDCWD=-100; <stdio.h>: RENAME_NOREPLACE=1.
        args: tuple[object, ...] = (-100, staging_bytes, -100, dst_bytes, 1)
    elif sys.platform == "darwin":
        try:
            rename = getattr(libc, "renamex_np")
        except AttributeError:
            raise OSError(
                unsupported_errno,
                "atomic no-replace directory rename is unavailable",
                str(dst),
            ) from None
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        # Darwin <sys/stdio.h>: RENAME_EXCL=0x00000004.
        args = (staging_bytes, dst_bytes, 0x00000004)
    else:
        raise OSError(
            unsupported_errno,
            "atomic no-replace directory rename is unavailable",
            str(dst),
        )

    ctypes.set_errno(0)
    if rename(*args) != 0:
        native_errno = ctypes.get_errno() or errno.EIO
        raise OSError(native_errno, os.strerror(native_errno), str(dst))


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
