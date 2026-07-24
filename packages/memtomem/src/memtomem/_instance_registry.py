"""Per-instance registration of live ``memtomem-server`` processes (#1935).

The per-user ``server.pid`` flock says "some server is running" but not
*which store it has open* — and only the losing (secondary) server ever
learns that a pair exists. This registry closes both gaps: every MCP
server that actually opens storage additionally flock-holds one sentinel
file here, and any process (another server answering ``mem_status``, or
the ``mm status`` CLI) can enumerate the live set and detect two servers
writing one store.

Layout (all under :func:`memtomem._runtime_paths.runtime_dir`):

``instances/<pid>-<ppid>-<digest16>-<procid8>-<nonce8>.lock``
    One empty file per live registration. All metadata lives in the
    *filename* — Windows cannot reliably read the body of a live locked
    file (``msvcrt`` range locks), so bodies are never read. ``digest16``
    is a SHA-256 prefix of the DB file's ``st_dev:st_ino`` (filesystem
    identity, not path text — case-insensitive filesystems and symlinks
    collapse; no path material is recoverable from it). ``procid8`` is a
    random per-process identity — the grouping key for "how many server
    *processes*" (pid values can collide across pid namespaces, procid
    cannot). ``nonce8`` makes each registration unique within a process.
    Liveness is the flock alone: held → live; acquirable → stale (the
    kernel released it when the holder died). mtime is the registration
    timestamp, used only for the stale-GC grace period.

``instances.registry.lock``
    Mutation sidecar, deliberately *outside* the scanned directory so it
    can never be mistaken for a corrupt sentinel. Serializes every
    registry mutation (create+flock, probe/GC, unlink) via the two-layer
    convention from ``indexing/debounce.py`` — intra-process
    ``threading.Lock`` first, then the cross-process portalocker lock —
    both bounded by one shared timeout. It is retained infrastructure:
    never parsed, probed, GC'd, staged, or deleted (unlinking a lock
    file has the classic waiter race — a blocked waiter acquires the
    orphaned inode while newcomers lock a fresh one). The runtime dir is
    volatile (``$XDG_RUNTIME_DIR`` / per-user tmp), so it self-cleans.

Failure polarity is per-surface: the status path **fails open** (an
incomplete enumeration produces no warning — a degraded advisory, never
a hang or a guess), while :func:`probe_all_for_uninstall` **fails
closed** (any live sentinel or any uncertainty refuses deletion).

Fork contract: forking a process that holds a registration is
**unsupported** — the server never forks (no ``os.fork`` /
``multiprocessing`` in this package). A forked child inherits the
sentinel descriptor, so the worst case is the warning *over-reporting*
until the child exits (safe direction: never a false deletion). The one
real mutation hazard — a child's inherited cleanup (atexit / context
close) unlinking the parent's sentinel — is closed by the pid guard in
:meth:`RegisteredInstance.cleanup`, which no-ops before touching any
lock or state when ``os.getpid()`` differs from the registering pid.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import logging
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal

import portalocker

from memtomem._runtime_paths import ensure_runtime_dir, runtime_dir

logger = logging.getLogger(__name__)

# One shared budget for every bounded lock acquisition and for the
# enumeration pass as a whole (acquisition + traversal).
_LOCK_TIMEOUT_S = 2.0
# An unlocked entry younger than this is left alone: its registrar may be
# between create and flock-acquire (the publication window). Fresh files
# always carry a fresh mtime — registration never reuses an existing file.
_STALE_GRACE_S = 60.0

_ENTRY_RE = re.compile(r"^(\d+)-(\d+)-([0-9a-f]{16})-([0-9a-f]{8})-([0-9a-f]{8})\.lock$")

# Exception tuple matching ``cli/_liveness.py:probe_pid_file`` (#817):
# POSIX raises ``BlockingIOError``; portalocker's Windows backend wraps
# Win32 errors as ``LockException``.
_LOCK_CONTENDED = (portalocker.LockException, BlockingIOError, OSError)


def instances_dir() -> Path:
    """Return the sentinel directory path without creating it."""
    return runtime_dir() / "instances"


def registry_sidecar_path() -> Path:
    """Return the mutation-sidecar path (outside :func:`instances_dir`)."""
    return runtime_dir() / "instances.registry.lock"


def store_digest_for(db_path: Path | str) -> str | None:
    """Return the 16-hex store-identity digest for ``db_path``, or ``None``.

    Identity is the DB file's ``(st_dev, st_ino)`` — filesystem identity,
    so different spellings of one file (case, symlinks) collapse and no
    path text leaks into filenames. ``None`` when the path is missing or
    not a regular file (``:memory:``, URI temp targets, and pre-creation
    states never register and never match).
    """
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return hashlib.sha256(f"{st.st_dev}:{st.st_ino}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class InstanceInfo:
    """One live registration, parsed from its sentinel filename."""

    pid: int
    ppid: int
    digest: str
    procid: str
    path: Path


@dataclass(frozen=True)
class EnumerationResult:
    """Live same-store instances plus whether the pass finished.

    ``complete=False`` (lock timeout, deadline expiry, unreadable dir)
    means the advisory surface must stay silent — the list is a lower
    bound, not evidence of absence.
    """

    instances: tuple[InstanceInfo, ...]
    complete: bool


class _MutationLockTimeout(Exception):
    """Bounded registry-lock acquisition expired."""


# ── module state ─────────────────────────────────────────────────────────
# ``_state_guard`` covers the procid, the active dict, and atexit
# installation — pure in-memory work, never held across file I/O.
_state_guard = threading.Lock()
_active: dict[Path, "RegisteredInstance"] = {}
_procid: str | None = None
_atexit_installed = False

# Intra-process half of the mutation lock (see module docstring — Windows
# ``LockFileEx`` does not reliably block a second handle from the same
# process, so portalocker alone cannot serialize threads).
_mutation_thread_lock = threading.Lock()


def _process_id_locked() -> str:
    """Return this process's random identity, generating it on first use.

    Caller holds ``_state_guard``.
    """
    global _procid
    if _procid is None:
        _procid = secrets.token_hex(4)
    return _procid


@contextlib.contextmanager
def _mutation_lock(deadline: float):
    """Two-layer bounded registry mutation lock.

    Raises :class:`_MutationLockTimeout` when either layer cannot be
    acquired before ``deadline`` (``time.monotonic`` timestamp). The
    portalocker layer polls non-blocking acquires — ``portalocker.lock``
    has no timeout parameter of its own.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _mutation_thread_lock.acquire(timeout=remaining):
        raise _MutationLockTimeout
    fp: IO[bytes] | None = None
    try:
        ensure_runtime_dir()
        # ``a+b`` — portalocker's Windows backend needs a writable handle
        # (``msvcrt.locking`` rejects read-only ones), and ``w`` would
        # truncate; see ``cli/_liveness.py``. Don't simplify.
        fp = open(registry_sidecar_path(), "a+b")
        while True:
            try:
                portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except _LOCK_CONTENDED:
                if time.monotonic() >= deadline:
                    raise _MutationLockTimeout from None
                time.sleep(0.05)
        yield
    finally:
        if fp is not None:
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
            with contextlib.suppress(Exception):
                fp.close()
        _mutation_thread_lock.release()


@dataclass
class RegisteredInstance:
    """A held registration: the sentinel path, its flock handle, and owner pid."""

    path: Path
    pid: int
    _fp: IO[bytes] = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    def cleanup(self) -> None:
        """Release this registration. Idempotent; never raises.

        The pid guard runs before *any* lock acquisition or state
        mutation: in a forked child every inherited registration fails
        it, so the child can never unlink the parent's sentinel (fork
        contract in the module docstring). On sidecar timeout the unlink
        is skipped — closing the handle still releases the flock, so the
        sentinel probes stale and ages out through normal GC.
        """
        if os.getpid() != self.pid:
            return
        with _state_guard:
            if self._closed:
                return
            self._closed = True
            if _active.get(self.path) is self:
                del _active[self.path]
        try:
            with _mutation_lock(time.monotonic() + _LOCK_TIMEOUT_S):
                _remove_locked_sentinel(self.path, self._fp)
        except _MutationLockTimeout:
            with contextlib.suppress(Exception):
                self._fp.close()
        except Exception:
            logger.debug("instance-registry cleanup failed for %s", self.path, exc_info=True)
            with contextlib.suppress(Exception):
                self._fp.close()


def _remove_locked_sentinel(path: Path, fp: IO[bytes]) -> None:
    """Unlink a sentinel whose flock ``fp`` currently holds.

    Platform-aware order (mirrors ``server/__init__.py:main``'s pid-file
    cleanup): POSIX unlinks while still holding the flock so exactly the
    owned inode dies, then closes; NTFS refuses to delete an open handle,
    so Windows closes (releasing the lock) and then unlinks best-effort.
    """
    if os.name == "nt":
        try:
            fp.close()
        finally:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        fp.close()


def _atexit_cleanup() -> None:
    # Single module-level handler (installed once) rather than one bound
    # callback per registration: a forked child inherits atexit entries,
    # and per-registration callbacks would carry live handles past the
    # active-dict reset. This handler re-reads the dict at exit time, and
    # each cleanup() re-checks the pid guard anyway.
    for inst in list(_active.values()):
        inst.cleanup()


def register_instance(db_path: Path | str) -> RegisteredInstance | None:
    """Register this process as having the store at ``db_path`` open.

    Called by the MCP server once storage initialization has succeeded
    (the DB file exists; the config is final). Returns ``None`` — never
    raises — on any failure: non-file store, lock timeout, permission
    errors. Registration failure must never affect server startup; the
    cost is a degraded advisory signal, not a broken server.
    """
    try:
        digest = store_digest_for(db_path)
        if digest is None:
            return None
        pid = os.getpid()
        with _state_guard:
            procid = _process_id_locked()
        name = f"{pid}-{os.getppid()}-{digest}-{procid}-{secrets.token_hex(4)}.lock"
        with _mutation_lock(time.monotonic() + _LOCK_TIMEOUT_S):
            directory = instances_dir()
            directory.mkdir(mode=0o700, exist_ok=True)
            # ``exist_ok=True`` accepts a symlink-to-directory, and the
            # sentinel open below would then land in the link's target —
            # refuse instead (same trust rule as the probes; the 0700
            # runtime dir plus the held mutation lock make the
            # lstat→open window practically inert).
            if _dir_state(directory) != "dir":
                return None
            path = directory / name
            # The nonce makes this filename fresh — never reuse/unlink an
            # existing entry here (a same-pid leftover may belong to a
            # different pid namespace and be live; probe+grace GC owns it).
            fp = open(path, "a+b")
            try:
                portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except _LOCK_CONTENDED:
                fp.close()
                return None
            inst = RegisteredInstance(path=path, pid=pid, _fp=fp)
            # Publish into the active dict while STILL holding the
            # mutation lock: the on-disk sentinel must never be visible
            # to a same-process enumeration before the in-memory record
            # exists, or the self-probe skip fails and Windows (where a
            # second same-process handle can acquire the flock) would
            # misread the fresh registration as stale. ``_state_guard``
            # nests inside the mutation lock here; no path nests them in
            # the opposite order, so there is no inversion.
            with _state_guard:
                _active[path] = inst
                global _atexit_installed
                if not _atexit_installed:
                    atexit.register(_atexit_cleanup)
                    _atexit_installed = True
        return inst
    except Exception:
        logger.debug("instance registration failed", exc_info=True)
        return None


def _parse_entry(path: Path) -> InstanceInfo | None:
    m = _ENTRY_RE.match(path.name)
    if m is None:
        return None
    return InstanceInfo(
        pid=int(m.group(1)),
        ppid=int(m.group(2)),
        digest=m.group(3),
        procid=m.group(4),
        path=path,
    )


def _probe_entry(path: Path) -> Literal["live", "stale", "gone", "unknown"]:
    """Flock-probe one sentinel. The lock, not the recorded pid, is
    authoritative (pid reuse — see ``cli/_liveness.py``). On ``stale``
    the caller decides about GC; the probe itself releases immediately.

    Contention and uncertainty are distinct here: only the known
    contention shapes (POSIX ``BlockingIOError``, portalocker's Windows
    ``LockException``) mean ``live``; any other ``OSError`` is an I/O
    failure and reports ``unknown`` — claiming ``live`` on it would let
    a transient error fabricate a concurrent-writer warning (the status
    surface is fail-open) or a false uninstall refusal.
    """
    try:
        fp = open(path, "rb+")
    except FileNotFoundError:
        return "gone"
    except OSError:
        return "unknown"
    try:
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, BlockingIOError):
            return "live"
        except OSError:
            return "unknown"
        portalocker.unlock(fp)
        return "stale"
    finally:
        fp.close()


def _dir_state(path: Path) -> Literal["dir", "missing", "untrusted"]:
    """No-follow tri-state for the sentinel directory.

    Only ``FileNotFoundError`` means *missing* (an empty registry). A
    symlink, a non-directory, or any other stat failure is *untrusted*:
    a symlinked ``instances/`` would redirect probing (and, worse, the
    uninstall staging that trusts these probes) into unrelated files —
    and a *dangling* symlink must not collapse into "missing" via a
    follow-the-link ``exists()`` check, or the fail-closed uninstall
    probe would answer NONE against a registry it cannot actually see.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "untrusted"
    return "dir" if stat.S_ISDIR(st.st_mode) else "untrusted"


def _gc_stale_entry(path: Path) -> None:
    """Best-effort removal of an entry already probed stale and aged.

    Re-acquires the entry's flock so the POSIX unlink happens while
    holding it (same inode-safety rule as cleanup); if the flock has
    been taken since the probe, the entry came alive — leave it.
    """
    try:
        fp = open(path, "rb+")
    except OSError:
        return
    try:
        portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except _LOCK_CONTENDED:
        fp.close()
        return
    _remove_locked_sentinel(path, fp)


def _aged(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > _STALE_GRACE_S
    except OSError:
        return False


def enumerate_live_instances(store_digest: str) -> EnumerationResult:
    """Enumerate live registrations for one store (advisory / status path).

    This process's own active registrations are included directly
    without probing — a second same-process handle can *acquire* the
    lock on Windows (see ``indexing/debounce.py``), so a self-probe
    would misclassify self as stale. Every other entry, same-pid ones
    included, is probed. Stale entries older than the grace period are
    opportunistically removed; unparseable names follow the same
    unlocked+grace rule. Fails open: any uncertainty yields
    ``complete=False`` and the caller must not warn.
    """
    # Both the directory AND the own-registration snapshot are taken only
    # UNDER the mutation lock, deadline started before acquisition: an
    # unlocked directory snapshot could miss a registrar that publishes
    # right after it, and an unlocked ``_active`` snapshot could miss a
    # same-process registration that publishes while we wait for the lock
    # — on Windows the later scan would then probe our own fresh sentinel
    # and misread it as stale (second same-process handles can acquire).
    # Ordering is mutation lock → ``_state_guard``, same as registration.
    results: list[InstanceInfo] = []
    complete = True
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    try:
        with _mutation_lock(deadline):
            with _state_guard:
                own = dict(_active)
            for path in own:
                info = _parse_entry(path)
                if info is not None and info.digest == store_digest:
                    results.append(info)
            directory = instances_dir()
            dir_state = _dir_state(directory)
            if dir_state == "missing":
                return EnumerationResult(_sorted(results), True)
            if dir_state == "untrusted":
                # symlink (dangling included) / non-dir — fail open
                return EnumerationResult(_sorted(results), False)
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                return EnumerationResult(_sorted(results), False)
            for entry in entries:
                if entry in own:
                    continue
                if time.monotonic() >= deadline:
                    complete = False
                    break
                info = _parse_entry(entry)
                state = _probe_entry(entry)
                if state == "live":
                    if info is not None and info.digest == store_digest:
                        results.append(info)
                    # live-but-unparseable: nothing to count, never GC'd
                elif state == "stale":
                    if _aged(entry):
                        _gc_stale_entry(entry)
                elif state == "unknown":
                    complete = False
                # "gone": deleted concurrently — nothing to do
    except _MutationLockTimeout:
        return EnumerationResult(_sorted(results), False)
    except Exception:
        logger.debug("instance enumeration failed", exc_info=True)
        return EnumerationResult(_sorted(results), False)
    return EnumerationResult(_sorted(results), complete)


def _sorted(results: list[InstanceInfo]) -> tuple[InstanceInfo, ...]:
    return tuple(sorted(results, key=lambda i: (i.pid, i.procid)))


def probe_all_for_uninstall() -> Literal["NONE", "LIVE", "UNKNOWN"]:
    """All-store, fail-closed probe for ``mm uninstall``.

    ``LIVE`` — at least one held sentinel (any store; deleting the
    registry under a live server is never acceptable, whatever store it
    has open). ``UNKNOWN`` — the pass could not complete (lock timeout,
    unreadable entry/dir, deadline): uninstall must refuse, a timeout
    never means "empty". ``NONE`` — a fully completed pass found zero
    live sentinels. Unlike the status path this performs no GC — an
    uninstall should not mutate the registry it is about to judge.
    """
    # Same rule as enumeration: the ``_active`` check and the directory
    # listing both happen only under the mutation lock, deadline started
    # before acquisition — an unlocked snapshot (or an unlocked
    # missing-dir fast path) can race a registrar mid-critical-section
    # and judge NONE against a stale view.
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    try:
        with _mutation_lock(deadline):
            with _state_guard:
                if _active:
                    return "LIVE"
            directory = instances_dir()
            dir_state = _dir_state(directory)
            if dir_state == "missing":
                return "NONE"
            if dir_state == "untrusted":
                # symlink (dangling included) / non-dir — never trust,
                # never traverse, never call it empty
                return "UNKNOWN"
            try:
                entries = list(directory.iterdir())
            except OSError:
                return "UNKNOWN"
            for entry in entries:
                if time.monotonic() >= deadline:
                    return "UNKNOWN"
                state = _probe_entry(entry)
                if state == "live":
                    return "LIVE"
                if state == "unknown":
                    return "UNKNOWN"
            return "NONE"
    except _MutationLockTimeout:
        return "UNKNOWN"
    except Exception:
        logger.debug("uninstall registry probe failed", exc_info=True)
        return "UNKNOWN"
