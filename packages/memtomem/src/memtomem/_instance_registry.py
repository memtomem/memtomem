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

``lifecycle.lock``
    The lifecycle barrier (#1936), also outside the scanned directory and
    also retained infrastructure — never parsed, probed, GC'd, staged, or
    deleted. A reader/writer flock closing the window between "a server
    opens the store" and "that server publishes its sentinel above": the
    server takes it **shared** before storage opens and holds it for the
    process lifetime, while the destructive CLIs take it **exclusive**
    across their final liveness re-probe and their write phase:
    ``mm uninstall`` over the whole staging of state, ``mm reset`` (#1945)
    over each of its two write boundaries (``initialize`` and the wipe).
    Both sides fail closed — a barrier that cannot be acquired means a
    destructive operation may be in flight, and neither startup nor
    deletion may proceed on a guess. Lock ordering is always **barrier →
    mutation sidecar**; no path acquires them the other way round.

    Scope, stated honestly: the barrier only closes the race between
    peers that *both* implement it. A pre-#1936 server ignores this file
    entirely, and a secondary server deliberately continues after losing
    the ``server.pid`` flock, so an older binary can still open the store
    inside uninstall's window. No mechanism can teach an already-shipped
    binary; for stale peers the residual window is the pre-existing
    #1936 bug, unchanged.

Failure polarity is per-surface: the status path **fails open** (an
incomplete enumeration produces no warning — a degraded advisory, never
a hang or a guess), while :func:`probe_all_for_uninstall` and both
barrier acquisitions **fail closed** (any live sentinel, any contention,
or any uncertainty refuses).

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

# Separate budget from ``_LOCK_TIMEOUT_S`` so the barrier's wait can be
# tuned (and shortened in tests) without touching the mutation lock's.
_BARRIER_TIMEOUT_S = 2.0

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


def lifecycle_barrier_path() -> Path:
    """Return the lifecycle-barrier path (outside :func:`instances_dir`)."""
    return runtime_dir() / "lifecycle.lock"


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


@dataclass(frozen=True)
class UninstallProbeResult:
    """Fail-closed verdict for ``mm uninstall`` (#1935, #1942).

    ``UNKNOWN`` and ``UNTRUSTED`` both refuse, but they prescribe
    opposite remediations: ``UNKNOWN`` is *transient* (lock timeout, a
    racing registrar, an unreadable entry) and retrying can succeed;
    ``UNTRUSTED`` is *persistent* (a probe path is redirected or
    otherwise not a private real directory) and retrying cannot change
    the answer until ``untrusted_path`` is removed or repaired.
    Collapsing the two sends the user into a retry loop against a
    condition that never resolves itself.

    ``untrusted_path`` is set exactly when ``state == "UNTRUSTED"`` and
    names the offending path — the sentinel directory, or the runtime
    dir that anchors it. ``detail`` is optional even then: only the
    runtime-dir producer sets it, carrying the exact ``ensure_runtime_dir``
    refusal (cause, expected value, and removal hint) that the generic
    redirected-path sentence cannot express — wrong owner or unsafe mode
    name a uid/mode the CLI would otherwise hide (#1948). Every
    ``ensure_runtime_dir`` refusal carries it, including a symlinked or
    junctioned *runtime* dir; only the redirected ``instances/`` directory
    (caught before the lock, not via ``_RuntimeDirRefused``) leaves it
    ``None``, since its cause is already in the generic wording.

    ``__post_init__`` enforces the ``untrusted_path`` <-> ``UNTRUSTED``
    invariant (and ``detail`` only alongside it) at construction, so a
    future producer cannot silently emit a path without the refusing
    state or vice versa.
    """

    state: Literal["NONE", "LIVE", "UNKNOWN", "UNTRUSTED"]
    untrusted_path: Path | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        untrusted = self.state == "UNTRUSTED"
        if (self.untrusted_path is not None) != untrusted:
            raise ValueError(
                "untrusted_path is set exactly when state == 'UNTRUSTED' "
                f"(state={self.state!r}, untrusted_path={self.untrusted_path!r})"
            )
        if self.detail is not None and not untrusted:
            raise ValueError(
                "detail is only meaningful when state == 'UNTRUSTED' "
                f"(state={self.state!r}, detail={self.detail!r})"
            )


class _MutationLockTimeout(Exception):
    """Bounded registry-lock acquisition expired."""


class _RuntimeDirRefused(Exception):
    """``ensure_runtime_dir`` refused the runtime dir itself (#1940).

    Raised only from :func:`_mutation_lock`'s translation of that one
    call — a symlinked/junctioned runtime dir, wrong owner, or unsafe
    mode. Kept distinct from every other failure inside the lock so the
    uninstall probe can attribute UNTRUSTED to the runtime dir without
    guessing: an arbitrary ``PermissionError`` (sidecar open, an entry's
    unlock/close) does not prove the runtime dir is at fault and must
    stay UNKNOWN (#1942).
    """


# ── module state ─────────────────────────────────────────────────────────
# ``_state_guard`` covers the procid, the active dict, and atexit
# installation — pure in-memory work, never held across file I/O.
_state_guard = threading.Lock()
_active: dict[Path, "RegisteredInstance"] = {}
# Identity set — several shared barrier holders share one path, so a
# path-keyed dict (as ``_active`` uses) could not hold them all.
_active_barriers: set["HeldBarrier"] = set()
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
        try:
            ensure_runtime_dir()
        except PermissionError as exc:
            raise _RuntimeDirRefused(str(exc)) from exc
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


class BarrierTimeout(Exception):
    """The lifecycle barrier could not be acquired before its deadline.

    Raised on contention *and* on infrastructure failure — both mean the
    caller cannot prove that no destructive operation is in flight, and
    both surfaces fail closed. The distinction is not actionable, but the
    underlying error is chained for the log.
    """


@dataclass(eq=False)
class HeldBarrier:
    """A held lifecycle-barrier flock: its path, handle, and owner pid.

    ``eq=False`` keeps the default identity hash: several shared holders
    coexist in one process (two ``AppContext``s), they all share one path,
    and :data:`_active_barriers` is an identity set. A value-comparing
    dataclass would be unhashable and ``set.add`` would raise *after* the
    flock was taken — leaking a hold nothing can release.
    """

    path: Path
    pid: int
    _fp: IO[bytes] = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    def release(self) -> None:
        """Drop this hold. Idempotent; never raises; never unlinks.

        The pid guard mirrors :meth:`RegisteredInstance.cleanup`: a forked
        child inherits the descriptor, and closing it there would release
        a barrier the parent still relies on. The file itself is retained
        infrastructure — unlinking a lock file lets a blocked waiter
        acquire the orphaned inode while newcomers lock a fresh one.
        """
        if os.getpid() != self.pid:
            return
        with _state_guard:
            if self._closed:
                return
            self._closed = True
            _active_barriers.discard(self)
        with contextlib.suppress(Exception):
            portalocker.unlock(self._fp)
        with contextlib.suppress(Exception):
            self._fp.close()


def _acquire_barrier(flags: int, timeout_s: float | None) -> HeldBarrier:
    """Acquire the lifecycle barrier with ``flags``, bounded by a deadline.

    One fresh descriptor per acquisition — flock conflicts are per open
    file description, so shared holders never block each other while an
    exclusive request still conflicts with every one of them. There is
    deliberately no intra-process ``threading.Lock`` layer (unlike
    :func:`_mutation_lock`): shared holders in one process *must* be
    allowed to coexist.
    """
    budget = _BARRIER_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + budget
    # Outside the poll loop on purpose: a runtime dir that cannot be
    # created or a barrier path that cannot be opened is not contention,
    # and the original error (``PermissionError`` naming the path, with
    # its remediation hint) must reach the caller unwrapped.
    ensure_runtime_dir()
    # Resolved once: the module-level resolvers are monkeypatchable seams,
    # and a second call could raise (or answer differently) *after* the
    # flock is held — leaving the lock owned by a descriptor no one tracks.
    path = lifecycle_barrier_path()
    # ``a+b`` for the same reason as the mutation sidecar — portalocker's
    # Windows backend needs a writable handle and ``w`` would truncate.
    fp = open(path, "a+b")
    # Everything from here to the successful return runs under one
    # ownership block: until the tracked ``HeldBarrier`` exists, nothing
    # but this handler can release the lock, so no failure may escape it.
    locked = False
    try:
        while True:
            try:
                portalocker.lock(fp, flags | portalocker.LOCK_NB)
                locked = True
                break
            except _LOCK_CONTENDED as exc:
                if time.monotonic() >= deadline:
                    raise BarrierTimeout(f"lifecycle barrier busy after {budget:.1f}s") from exc
                time.sleep(0.05)
        barrier = HeldBarrier(path=path, pid=os.getpid(), _fp=fp)
        with _state_guard:
            _active_barriers.add(barrier)
    except BaseException:
        # A hold nobody can release is worse than no hold: drop it and
        # fail closed rather than leaning on descriptor finalization.
        if locked:
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
        with contextlib.suppress(Exception):
            fp.close()
        raise
    return barrier


def acquire_server_lifecycle_barrier(timeout_s: float | None = None) -> HeldBarrier:
    """Take the barrier **shared**, before the server opens storage.

    Held for the process lifetime and released only once storage close is
    confirmed, so a server whose registration failed — or whose close
    failed — still blocks uninstall instead of going invisible. Raises
    :class:`BarrierTimeout` (contention or unusable runtime dir) or the
    original :class:`OSError`; the caller must not proceed to open the
    store on failure.

    ``timeout_s=None`` resolves :data:`_BARRIER_TIMEOUT_S` at call time —
    a default argument would freeze the value at import and silently
    ignore any later tuning.
    """
    return _acquire_barrier(portalocker.LOCK_SH, timeout_s)


def acquire_uninstall_lifecycle_barrier(timeout_s: float | None = None) -> HeldBarrier:
    """Take the barrier **exclusive**, across a destructive phase.

    The exclusive side of the barrier, shared by every destructive CLI
    that must keep servers out of the store while it writes: ``mm
    uninstall`` across its staging, ``mm reset`` (#1945) across each of
    its two write boundaries. Held through the final liveness re-probe
    *and* the write, so a server cannot open the store in between. Raises
    :class:`BarrierTimeout` on contention: a held flock is never stale
    (the kernel releases it when its holder dies), so this refusal is not
    ``--force``-overridable. (Name kept for API stability; not
    uninstall-specific.)
    """
    return _acquire_barrier(portalocker.LOCK_EX, timeout_s)


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

    ``lstat`` alone does not carry that rule on Windows: a junction
    redirects exactly like a symlink but keeps ``S_IFDIR``, so it needs
    its own reparse-tag check to land in *untrusted*.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "untrusted"
    if not stat.S_ISDIR(st.st_mode) or path.is_junction():
        return "untrusted"
    return "dir"


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


def probe_all_for_uninstall() -> UninstallProbeResult:
    """All-store, fail-closed probe for ``mm uninstall``.

    ``LIVE`` — at least one held sentinel (any store; deleting the
    registry under a live server is never acceptable, whatever store it
    has open). ``UNKNOWN`` — the pass could not complete (lock timeout,
    unreadable entry/dir, deadline): uninstall must refuse, a timeout
    never means "empty". ``UNTRUSTED`` — a probe path is not a private
    real directory (symlinked/junctioned ``instances/``, or a runtime
    dir ``ensure_runtime_dir`` refuses): uninstall must refuse *and*
    tell the user which path to remove or repair — "retry" is wrong
    advice for this cause (#1942). ``NONE`` — a fully completed pass
    found zero live sentinels. Unlike the status path this performs no
    GC — an uninstall should not mutate the registry it is about to
    judge.
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
                    return UninstallProbeResult("LIVE")
            directory = instances_dir()
            dir_state = _dir_state(directory)
            if dir_state == "missing":
                return UninstallProbeResult("NONE")
            if dir_state == "untrusted":
                # symlink (dangling included) / non-dir — never trust,
                # never traverse, never call it empty
                return UninstallProbeResult("UNTRUSTED", untrusted_path=directory)
            try:
                entries = list(directory.iterdir())
            except OSError:
                return UninstallProbeResult("UNKNOWN")
            for entry in entries:
                if time.monotonic() >= deadline:
                    return UninstallProbeResult("UNKNOWN")
                state = _probe_entry(entry)
                if state == "live":
                    return UninstallProbeResult("LIVE")
                if state == "unknown":
                    return UninstallProbeResult("UNKNOWN")
            return UninstallProbeResult("NONE")
    except _MutationLockTimeout:
        return UninstallProbeResult("UNKNOWN")
    except _RuntimeDirRefused as exc:
        # ``ensure_runtime_dir`` refused the runtime dir itself —
        # symlink, junction, wrong owner, unsafe mode (#1940).
        # Persistent until the user removes/repairs it, so it must not
        # collapse into UNKNOWN's "retry" advice. Only this translated
        # signal is attributed: any other error inside the lock (sidecar
        # open, an entry's unlock/close) proves nothing about the
        # runtime dir and stays UNKNOWN below. Carry the exception's
        # message (the precise cause + removal hint) as ``detail`` so the
        # CLI can surface owner/mode specifics the generic sentence hides
        # (#1948).
        logger.debug("uninstall registry probe refused runtime dir", exc_info=True)
        return UninstallProbeResult("UNTRUSTED", untrusted_path=runtime_dir(), detail=str(exc))
    except Exception:
        logger.debug("uninstall registry probe failed", exc_info=True)
        return UninstallProbeResult("UNKNOWN")
