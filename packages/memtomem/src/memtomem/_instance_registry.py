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

``presence/<pid>-<ppid>-<pathdigest16>-<procid8>-<nonce8>.lock``
    One empty file per live *server process* (#2230), written at startup
    before any store is opened, so handshake-only MCP sessions — the
    population that accumulates — are visible at all. Same filename
    grammar and same liveness rule as ``instances/`` above, with one
    substitution: ``pathdigest16`` is :func:`memtomem._runtime_paths.
    store_pid_digest`, the SHA-256 prefix of the *normalized store path
    text* already used to name ``server-<digest>.pid``, because the store
    file need not exist yet and ``st_dev:st_ino`` is therefore undefined.
    The two digests are not comparable and must never be joined on.
    A marker and the sentinel the same process later publishes under
    ``instances/`` are related through the process they share — readers
    identify it by ``(pid, procid8)``, since ``procid8`` is 32 random bits
    and a pid is reused and collides across pid namespaces, so neither
    settles identity on its own. Markers live in their
    own directory precisely so that no existing reader — above all
    ``mm uninstall``'s fail-closed probe, which treats an unparseable
    entry under ``instances/`` as untrusted — sees a new kind of file in a
    directory whose contents it already ascribes meaning to.

``instances.registry.lock``
    Mutation sidecar, deliberately *outside* the scanned directory so it
    can never be mistaken for a corrupt sentinel. Serializes every
    registry mutation (create+flock, probe/GC, unlink) via the two-layer
    convention from ``indexing/debounce.py`` — intra-process
    ``threading.Lock`` first, then the cross-process portalocker lock —
    both bounded by one shared timeout. It is retained infrastructure:
    never parsed, probed, GC'd, staged, or deleted (unlinking a lock
    file has the classic waiter race — a blocked waiter acquires the
    orphaned inode while newcomers lock a fresh one). The stable runtime dir
    lives in the per-user OS temp tree, so it is volatile.

``lifecycle.lock``
    The lifecycle barrier (#1936), also outside the scanned directory and
    also retained infrastructure — never parsed, probed, GC'd, staged, or
    deleted. A reader/writer flock closing the window between "a server
    opens the store" and "that server publishes its sentinel above": the
    server takes it **shared** before storage opens and holds it for the
    process lifetime, while the destructive CLIs take the stable barrier and
    every safe derivable pre-#2037 barrier **exclusive** across their final
    liveness re-probe and their write phase:
    ``mm uninstall`` over the whole staging of state, ``mm reset`` (#1945)
    over each of its two write boundaries (``initialize`` and the wipe).
    Both sides fail closed — a barrier that cannot be acquired means a
    destructive operation may be in flight, and neither startup nor
    deletion may proceed on a guess. The two sides use different Windows
    APIs (shared goes through ``LockFileEx``, exclusive through
    ``msvcrt.locking`` since portalocker 3.2.0), so that they contend with
    each other at all is measured in both directions rather than assumed
    (#2102, ``tests/test_windows_lock_semantics.py``) — as is the converse
    the shared side needs, that two shared holders in one process still
    coexist. Lock ordering is always **barrier →
    mutation sidecar**; no path acquires them the other way round.

    Scope, stated honestly: the barrier only closes the race between peers
    that implement it. A pre-#1936 server ignores this file entirely. A
    pre-#2037 server under a derivable legacy root is fenced during transition,
    but one already running under a non-standard XDG root unknown to this
    caller cannot be inferred retroactively. No mechanism can teach an
    already-shipped binary; restarting it on the current version moves it to
    the stable anchor.

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
import errno
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
from typing import IO, Literal, NoReturn

import portalocker

from memtomem._lock_errors import (
    LOCK_CALL_ERRORS,
    LOCK_CALL_ERRORS_WIDE,
    is_lock_contention as _is_lock_contention,
    raise_lock_io_failure,
)
from memtomem._runtime_paths import (
    candidate_runtime_dirs,
    ensure_runtime_dir,
    ensure_runtime_dir_at,
    runtime_dir,
    store_pid_digest,
    validate_runtime_dir,
)

logger = logging.getLogger(__name__)

# One shared budget for every bounded lock acquisition and for the
# enumeration pass as a whole (acquisition + traversal).
_LOCK_TIMEOUT_S = 2.0
# Total sidecar-lock attempts for register_instance (#1939). Retried only
# on _MutationLockTimeout — contention with an enumeration pass that holds
# the sidecar for its whole 2 s budget would otherwise leave this server
# permanently invisible to the concurrent-writer signal. Every other
# failure is permanent (non-file store, untrusted dir) or logged-and-
# dropped and is not retried; worst case is _REGISTRATION_ATTEMPTS ×
# _LOCK_TIMEOUT_S off-loop (to_thread) on the first initializing call.
_REGISTRATION_ATTEMPTS = 3
# An unlocked entry younger than this is left alone: its registrar may be
# between create and flock-acquire (the publication window). Fresh files
# always carry a fresh mtime — registration never reuses an existing file.
_STALE_GRACE_S = 60.0

_ENTRY_RE = re.compile(r"^(\d+)-(\d+)-([0-9a-f]{16})-([0-9a-f]{8})-([0-9a-f]{8})\.lock$")

# Digest slot for a presence marker (#2230) whose store cannot be named:
# ``:memory:``, a URI store, or a config that would not resolve. Chosen to
# satisfy ``_ENTRY_RE`` so an unnamed store still registers rather than
# leaving the process uncounted. It is *not* a store identity — every
# unnamed store collapses onto it — so only ``procid`` may be grouped on.
# A SHA-256 prefix of sixteen zeros is not reachable in practice, so this
# cannot be confused with a real digest.
_UNKNOWN_STORE_DIGEST = "0" * 16

# Presence registration runs inline in ``memtomem-server``'s startup, ahead of
# ``mcp.run()``, so its worst case is startup latency an MCP client waits on.
# ``register_instance``'s budget is wrong here twice over: it is three attempts
# of ``_LOCK_TIMEOUT_S`` (up to six seconds before the handshake), and its
# reason for retrying does not apply — a sentinel lost to the sidecar race
# would leave a *writer* permanently invisible to the concurrent-writer
# warning, while a lost marker costs one process in a diagnostic and is
# rewritten by the next server to start. One short attempt, then give up.
_PRESENCE_LOCK_TIMEOUT_S = 0.5

# Separate budget from ``_LOCK_TIMEOUT_S`` so the barrier's wait can be
# tuned (and shortened in tests) without touching the mutation lock's.
_BARRIER_TIMEOUT_S = 2.0
# Exception tuple matching ``cli/_liveness.py:probe_pid_file`` (#817), shared
# with ``context/_atomic.py`` since #2229 — see
# :data:`memtomem._lock_errors.LOCK_CALL_ERRORS` for every shape a
# non-blocking ``portalocker.lock`` can produce and the one documented hole
# (a raw 3.x ``pywintypes.error``, which only ``_BARRIER_LOCK_ERRORS`` below
# widens for).
# ``_acquire_barrier`` (#1957) and ``_mutation_lock`` (#1939) narrow it
# further with ``_is_lock_contention``: both poll a held lock but must let
# a lock-call I/O failure escape, because each has a caller that would
# otherwise mistreat it — the barrier would flatten it into stop-the-holder
# advice, and ``register_instance``'s retry would triple a permanent
# failure's delay. ``register_instance``'s own sentinel flock and
# ``_gc_stale_entry`` still treat the whole tuple as one bucket: neither
# retries, and their fail-open/fail-closed contracts absorb a lock-call I/O
# error exactly as they do contention, so splitting there would only add
# noise.
_LOCK_CONTENDED = LOCK_CALL_ERRORS

# Windows ``PermissionError.winerror`` codes that mean *transient*
# contention, not durable denial: ERROR_SHARING_VIOLATION (32) and
# ERROR_LOCK_VIOLATION (33). A sentinel briefly held by antivirus or
# another handle takes this path and must stay ``unknown`` (retry can
# clear it), unlike a mode-000 / root-owned entry (#1938).
_WIN_TRANSIENT_SHARING = frozenset({32, 33})

# The barrier poll loop's catch set: the shared tuple widened for a raw
# portalocker 3.x ``pywintypes.error`` (see
# :data:`memtomem._lock_errors.LOCK_CALL_ERRORS_WIDE`). Barrier-only among
# the registry's sites — the others keep the narrower ``_LOCK_CONTENDED``,
# whose callers absorb a raw error through their own ``except``.
_BARRIER_LOCK_ERRORS: tuple[type[BaseException], ...] = LOCK_CALL_ERRORS_WIDE


def instances_dir(root: Path | None = None) -> Path:
    """Return the sentinel directory path without creating it."""
    return (runtime_dir() if root is None else root) / "instances"


def presence_dir(root: Path | None = None) -> Path:
    """Return the startup-marker directory path without creating it (#2230)."""
    return (runtime_dir() if root is None else root) / "presence"


def registry_sidecar_path(root: Path | None = None) -> Path:
    """Return the mutation-sidecar path (outside :func:`instances_dir`)."""
    return (runtime_dir() if root is None else root) / "instances.registry.lock"


def lifecycle_barrier_path(root: Path | None = None) -> Path:
    """Return the lifecycle-barrier path (outside :func:`instances_dir`)."""
    return (runtime_dir() if root is None else root) / "lifecycle.lock"


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
    """Fail-closed verdict for ``mm uninstall`` (#1935, #1942, #1938).

    ``UNKNOWN`` and ``UNTRUSTED`` both refuse, but they prescribe
    opposite remediations: ``UNKNOWN`` is *transient* (lock timeout, a
    racing registrar, a mid-write entry) and retrying can succeed;
    ``UNTRUSTED`` is *persistent* (a probe path is redirected, or a
    sentinel entry is not a probeable private regular file — a stray
    subdirectory, link, or permission-denied path) and retrying cannot
    change the answer until ``untrusted_path`` is removed or repaired.
    Collapsing the two sends the user into a retry loop against a
    condition that never resolves itself.

    ``untrusted_path`` is set exactly when ``state == "UNTRUSTED"`` and
    names the offending path — the sentinel directory, the runtime dir
    that anchors it, or a single entry inside it. ``untrusted_kind``
    (also set then; ``None`` defaults to ``"redirected"`` at the surface)
    selects the remediation vocabulary, *not* the path kind:
    ``"redirected"`` for a path that is a symlink / junction /
    non-directory (the surface says "redirected path"), and
    ``"unprobeable"`` for a real path the probe cannot read through — a
    stray subdirectory entry, a permission-denied entry, or an
    ``instances/`` that cannot be listed (the surface says "cannot be
    probed"). The unlistable directory is a *real* private directory, so
    it carries ``"unprobeable"`` despite ``untrusted_path`` being a
    directory — hence keying on the message, not the path shape.

    ``detail`` is optional even then: only the runtime-dir producer sets
    it, carrying the exact ``ensure_runtime_dir`` refusal (cause,
    expected value, and removal hint) that the generic redirected-path
    sentence cannot express — wrong owner or unsafe mode name a uid/mode
    the CLI would otherwise hide (#1948). Every ``ensure_runtime_dir``
    refusal carries it, including a symlinked or junctioned *runtime*
    dir; only the redirected ``instances/`` directory (caught before the
    lock, not via ``_RuntimeDirRefused``) and the entry-level unprobeable
    causes leave it ``None``, since their cause is already in the wording.

    ``__post_init__`` enforces the ``untrusted_path`` <-> ``UNTRUSTED``
    invariant (and ``untrusted_kind`` / ``detail`` only alongside it) at
    construction, so a future producer cannot silently emit a path
    without the refusing state or vice versa.
    """

    state: Literal["NONE", "LIVE", "UNKNOWN", "UNTRUSTED"]
    untrusted_path: Path | None = None
    untrusted_kind: Literal["redirected", "unprobeable"] | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        untrusted = self.state == "UNTRUSTED"
        if (self.untrusted_path is not None) != untrusted:
            raise ValueError(
                "untrusted_path is set exactly when state == 'UNTRUSTED' "
                f"(state={self.state!r}, untrusted_path={self.untrusted_path!r})"
            )
        if self.untrusted_kind is not None and not untrusted:
            raise ValueError(
                "untrusted_kind is only meaningful when state == 'UNTRUSTED' "
                f"(state={self.state!r}, untrusted_kind={self.untrusted_kind!r})"
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

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        super().__init__(detail)


# ── module state ─────────────────────────────────────────────────────────
# ``_state_guard`` covers the procid, the active dict, and atexit
# installation — pure in-memory work, never held across file I/O.
_state_guard = threading.Lock()
_active: dict[Path, "RegisteredInstance"] = {}
# Startup presence markers (#2230), deliberately a *separate* dict from
# ``_active``. Both live one level under the runtime root, so a shared dict
# would leak markers into ``_active_for_root`` — and through it into the
# store-scoped enumeration and, worse, into ``mm uninstall``'s fail-closed
# in-process LIVE short-circuit. A handshake-only server is not evidence
# that the store is open, so the two populations never merge.
_active_presence: dict[Path, "RegisteredInstance"] = {}
# Identity set — several shared barrier holders share one path, so a
# path-keyed dict (as ``_active`` uses) could not hold them all.
_active_barriers: set["HeldBarrier"] = set()
_procid: str | None = None
_atexit_installed = False

# Intra-process half of the mutation lock (see module docstring). This is
# load-bearing and unconditional: it serializes this process's mutation spans
# whatever the file backend does with same-process acquisitions (measured to
# contend on Windows for portalocker 3.1.1/3.2.0/4.1.0, #2102 — but this layer
# does not depend on that), leaving the file lock responsible for cross-process
# serialization only. It also keeps
# the file lock's bounded non-blocking poll budget reserved for genuine
# cross-process contention instead of this process's own threads.
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
def _mutation_lock(deadline: float, *, root: Path | None = None):
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
            target = ensure_runtime_dir() if root is None else ensure_runtime_dir_at(root)
        except PermissionError as exc:
            refused = runtime_dir() if root is None else root
            raise _RuntimeDirRefused(refused, str(exc)) from exc
        # ``a+b`` — portalocker's Windows backend needs a writable handle
        # (``msvcrt.locking`` rejects read-only ones), and ``w`` would
        # truncate; see ``cli/_liveness.py``. Don't simplify.
        fp = open(registry_sidecar_path(target), "a+b")
        while True:
            try:
                portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except _LOCK_CONTENDED as exc:
                # Split contention from a lock-call I/O failure the same way
                # ``_acquire_barrier`` does (#1957), because #1939 gave this
                # site a *retrying* caller: ``register_instance`` re-runs on
                # ``_MutationLockTimeout``, so folding a durable ``EIO``/
                # ``ENOLCK`` (a bare ``LockException``) into the timeout
                # would triple a permanent failure's startup delay. Only
                # genuine contention (:func:`_is_lock_contention`) is polled
                # to the deadline; anything else propagates unwrapped to the
                # never-raise handler and stays one-shot. Unlike the barrier
                # there is no ``_raise_lock_io_failure`` normalization — the
                # mutation lock's callers already absorb a raw error through
                # their own ``except`` (cleanup closes the handle,
                # enumeration is incomplete, the uninstall probe is UNKNOWN).
                if not _is_lock_contention(exc):
                    raise
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
    """The lifecycle barrier could not be taken before the deadline.

    Normally contention — another process holds the flock — and the
    destructive CLIs advise stopping the holder. Infrastructure failures
    *before* the lock attempt (a refused runtime dir, an unopenable
    barrier path) do not reach here: they escape :func:`_acquire_barrier`
    unwrapped as the original :class:`OSError`, and the CLIs route them to
    a repair-the-path remediation instead (#1945, #1951). Both fail closed;
    the last flock error is chained for the log.

    A lock-*call* I/O failure inside the poll loop is not contention
    either (#1957): portalocker wraps e.g. ``EIO``/``ENOLCK`` — and an NFS
    ``EOFError`` — in a bare ``LockException`` (not an ``OSError``), and
    :func:`_is_lock_contention` splits those out so they escape as
    ``OSError`` immediately, without waiting out the deadline. Only a
    genuinely held lock (``AlreadyLocked`` / ``BlockingIOError`` / an
    ``EACCES``-``EAGAIN`` cause / a Windows lock-violation cause) reaches
    this timeout.
    """


@dataclass(eq=False)
class HeldBarrier:
    """One logical lifecycle hold backed by one or more ordered flocks.

    ``eq=False`` keeps the default identity hash: several shared holders
    coexist in one process (two ``AppContext``s), they all share one path,
    and :data:`_active_barriers` is an identity set. A value-comparing
    dataclass would be unhashable and ``set.add`` would raise *after* the
    flock was taken — leaking a hold nothing can release.
    """

    path: Path
    pid: int
    _fp: IO[bytes] = field(repr=False)
    _additional: tuple[tuple[Path, IO[bytes]], ...] = field(default=(), repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def paths(self) -> tuple[Path, ...]:
        """Every path owned by this logical hold, in acquisition order."""
        return (self.path, *(path for path, _fp in self._additional))

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
        handles = ((self.path, self._fp), *self._additional)
        for _path, fp in reversed(handles):
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
            with contextlib.suppress(Exception):
                fp.close()


def _raise_lock_io_failure(exc: BaseException, path: Path) -> NoReturn:
    """Normalize a barrier lock-call failure to ``OSError`` (#1957).

    Thin binding of the shared :func:`memtomem._lock_errors.raise_lock_io_failure`
    that names *this* lock in the last-resort message. The destructive CLIs
    route an ``OSError`` from barrier acquisition to their repair-the-path
    remediation (#1951, #1959); any other type would be flattened into
    :class:`BarrierTimeout`'s stop-the-holder advice, sending the user
    hunting for a process that does not exist (#1870).
    """
    raise_lock_io_failure(exc, path, label="lifecycle barrier")


def _acquire_barrier_file(path: Path, flags: int, deadline: float, budget: float) -> IO[bytes]:
    """Acquire one already-resolved barrier path under a shared deadline."""
    fp = open(path, "a+b")
    locked = False
    try:
        while True:
            try:
                portalocker.lock(fp, flags | portalocker.LOCK_NB)
                locked = True
                return fp
            except _BARRIER_LOCK_ERRORS as exc:
                if not _is_lock_contention(exc):
                    _raise_lock_io_failure(exc, path)
                if time.monotonic() >= deadline:
                    raise BarrierTimeout(f"lifecycle barrier busy after {budget:.1f}s") from exc
                time.sleep(0.05)
    except BaseException:
        if locked:
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
        with contextlib.suppress(Exception):
            fp.close()
        raise


def _acquire_barrier(
    flags: int, timeout_s: float | None, *, roots: tuple[Path, ...] | None = None
) -> HeldBarrier:
    """Acquire one logical lifecycle barrier, bounded by one deadline.

    One fresh descriptor per acquisition — flock conflicts are per open
    file description, so shared holders never block each other while an
    exclusive request still conflicts with every one of them. There is
    deliberately no intra-process ``threading.Lock`` layer (unlike
    :func:`_mutation_lock`): shared holders in one process *must* be
    allowed to coexist.
    """
    budget = _BARRIER_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + budget
    resolved_roots: tuple[Path, ...]
    if roots is None:
        canonical = ensure_runtime_dir()
        resolved_roots = (canonical,)
    else:
        resolved_roots = roots
    acquired: list[tuple[Path, IO[bytes]]] = []
    try:
        for root in resolved_roots:
            path = lifecycle_barrier_path(root)
            acquired.append((path, _acquire_barrier_file(path, flags, deadline, budget)))
        first_path, first_fp = acquired[0]
        barrier = HeldBarrier(
            path=first_path,
            pid=os.getpid(),
            _fp=first_fp,
            _additional=tuple(acquired[1:]),
        )
        with _state_guard:
            _active_barriers.add(barrier)
    except BaseException:
        for _path, fp in reversed(acquired):
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
            with contextlib.suppress(Exception):
                fp.close()
        raise
    return barrier


def _destructive_barrier_roots() -> tuple[Path, ...]:
    """Ensure canonical then derivable legacy roots in deadlock-safe order.

    Every destructive process takes the canonical lock first, so only one can
    proceed to the historical set. Creating a missing safe historical leaf
    prevents an old server from appearing there after the final probe.
    """
    canonical = ensure_runtime_dir()
    candidates = candidate_runtime_dirs()
    # Preserve the established module-level ``runtime_dir`` test seam: a
    # patched canonical with an unpatched candidate resolver must never reach
    # the developer's real coordination roots.
    if not candidates or candidates[0] != canonical:
        candidates = [canonical]
    others = sorted({path for path in candidates if path != canonical}, key=str)
    ensured = [canonical]
    for candidate in others:
        # Do not skip an unsafe existing historical leaf. It may have become
        # unsafe after an old server opened and locked its barrier; treating
        # it as speculative would let a destructive command proceed without
        # proving that holder absent. Refuse fail-closed and preserve the
        # exact legacy path in RuntimeDirValidationError for CLI remediation.
        ensured.append(ensure_runtime_dir_at(candidate))
    return tuple(ensured)


def acquire_server_lifecycle_barrier(timeout_s: float | None = None) -> HeldBarrier:
    """Take the barrier **shared**, before the server opens storage.

    Held for the process lifetime and released only once storage close is
    confirmed, so a server whose registration failed — or whose close
    failed — still blocks uninstall instead of going invisible. Raises
    :class:`BarrierTimeout` on contention, or an :class:`OSError` on an
    unusable runtime dir / barrier path *or* a non-contention lock-call
    I/O failure (#1957); the caller must not proceed to open the store on
    failure.

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
    :class:`BarrierTimeout` on contention — a held flock is never stale
    (the kernel releases it when its holder dies), so that refusal is not
    ``--force``-overridable — or an :class:`OSError` on an unusable runtime
    dir / barrier path *or* a non-contention lock-call I/O failure (#1957),
    which the CLIs route to a repair-the-path remediation (#1951). (Name
    kept for API stability; not uninstall-specific.)
    """
    return _acquire_barrier(
        portalocker.LOCK_EX,
        timeout_s,
        roots=_destructive_barrier_roots(),
    )


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
            # Both populations are unpublished here rather than only
            # ``_active``: a startup presence marker (#2230) is the same held
            # (path, flock) pair and releases identically, and keying the
            # sweep on identity keeps a path that somehow appears in both
            # from unpublishing the wrong record.
            for pool in (_active, _active_presence):
                if pool.get(self.path) is self:
                    del pool[self.path]
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
    for inst in [*_active.values(), *_active_presence.values()]:
        inst.cleanup()


def register_instance(db_path: Path | str) -> RegisteredInstance | None:
    """Register this process as having the store at ``db_path`` open.

    Called by the MCP server once storage initialization has succeeded
    (the DB file exists; the config is final). Returns ``None`` — never
    raises — on any failure: non-file store, lock timeout, permission
    errors. Registration failure must never affect server startup; the
    cost is a degraded advisory signal, not a broken server.

    A sidecar-lock timeout is retried up to :data:`_REGISTRATION_ATTEMPTS`
    times (#1939): the mutation sidecar is held for a full ``mem_status``
    enumeration pass, so a single lost race would otherwise leave this
    server permanently invisible to the concurrent-writer signal. The
    timeout is raised *before* :func:`_mutation_lock` yields, so no partial
    on-disk state exists to reconcile between attempts. Every other
    outcome is one-shot: a non-file store never enters the loop, and a
    permanent in-loop refusal (untrusted directory, sentinel flock
    contention) or an unexpected exception exits immediately.
    """
    try:
        digest = store_digest_for(db_path)
        if digest is None:
            return None
        pid = os.getpid()
        with _state_guard:
            procid = _process_id_locked()
        for attempt in range(_REGISTRATION_ATTEMPTS):
            # Fresh nonce per attempt — a timed-out attempt created no
            # file, but regenerating keeps the "registration never reuses
            # an existing filename" invariant unconditional.
            name = f"{pid}-{os.getppid()}-{digest}-{procid}-{secrets.token_hex(4)}.lock"
            try:
                with _mutation_lock(time.monotonic() + _LOCK_TIMEOUT_S):
                    directory = instances_dir()
                    directory.mkdir(mode=0o700, exist_ok=True)
                    # ``exist_ok=True`` accepts a symlink-to-directory, and
                    # the sentinel open below would then land in the link's
                    # target — refuse instead (same trust rule as the
                    # probes; the 0700 runtime dir plus the held mutation
                    # lock make the lstat→open window practically inert).
                    if _dir_state(directory) != "dir":
                        return None
                    path = directory / name
                    # The nonce makes this filename fresh — never
                    # reuse/unlink an existing entry here (a same-pid
                    # leftover may belong to a different pid namespace and
                    # be live; probe+grace GC owns it).
                    fp = open(path, "a+b")
                    try:
                        portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    except _LOCK_CONTENDED:
                        fp.close()
                        return None
                    inst = RegisteredInstance(path=path, pid=pid, _fp=fp)
                    # Publish into the active dict while STILL holding the
                    # mutation lock, so the on-disk sentinel is never visible
                    # to a same-process enumeration before the in-memory
                    # record exists. What that buys is uniformity, not a
                    # different answer: an enumeration racing in earlier
                    # would fall through to probing our own fresh entry and
                    # (measured, #2102) find it ``live`` anyway. Publishing
                    # under the lock keeps every self-directed read on the
                    # ``_active`` path and spares it that open+lock.
                    # ``_state_guard`` nests inside the mutation lock here;
                    # no path nests them in the opposite order, so there is
                    # no inversion.
                    with _state_guard:
                        _active[path] = inst
                        global _atexit_installed
                        if not _atexit_installed:
                            atexit.register(_atexit_cleanup)
                            _atexit_installed = True
                    return inst
            except _MutationLockTimeout:
                # Contention only — retry with a fresh budget. No file was
                # created (the timeout fires before the lock yields).
                logger.debug(
                    "instance registration lost the sidecar race (attempt %d/%d)",
                    attempt + 1,
                    _REGISTRATION_ATTEMPTS,
                )
        return None
    except Exception:
        logger.debug("instance registration failed", exc_info=True)
        return None


def register_server_presence(db_path: Path | str | None) -> RegisteredInstance | None:
    """Record that this *process* is a live server, before any store opens.

    The startup counterpart to :func:`register_instance` (#2230). That one
    can only run once storage is initialized, because its digest is the
    store file's ``st_dev:st_ino`` — so a handshake-only MCP session, which
    opens no store at all, was invisible to every registry consumer. This
    marker is written from ``memtomem-server``'s ``main`` beside the pid
    lock, where the only store fact available is the configured *path*.

    ``db_path`` is therefore digested with
    :func:`memtomem._runtime_paths.store_pid_digest` (path text, the same
    key that names ``server-<digest>.pid``), and a ``None`` digest —
    ``:memory:``, a URI store, an unresolvable config, or no path at all —
    falls back to :data:`_UNKNOWN_STORE_DIGEST` rather than refusing to
    register: the marker's job is to count *processes*, and a server whose
    store cannot be named is still a running server. Consumers group
    markers by ``procid`` and must not read the digest as a store identity
    (all unnamed stores collapse onto one value), nor compare it with a
    sentinel's inode digest — the two are different functions of different
    inputs and only coincide by accident.

    Returns ``None`` — never raises — on any failure, and one registration
    is one file: startup must not be blocked, delayed past its bounded lock
    budget, or aborted because a coordination directory is unusable.
    """
    try:
        digest = store_pid_digest(db_path) if db_path is not None else None
        if digest is None:
            digest = _UNKNOWN_STORE_DIGEST
        pid = os.getpid()
        with _state_guard:
            procid = _process_id_locked()
        name = f"{pid}-{os.getppid()}-{digest}-{procid}-{secrets.token_hex(4)}.lock"
        # One budget for the whole lock-held span, not just for taking it:
        # whatever acquisition leaves is what the sweep may spend, and
        # publishing this process's own marker always happens regardless.
        deadline = time.monotonic() + _PRESENCE_LOCK_TIMEOUT_S
        try:
            with _mutation_lock(deadline):
                directory = presence_dir()
                directory.mkdir(mode=0o700, exist_ok=True)
                # ``exist_ok=True`` accepts a symlink-to-directory and the open
                # below would then land in the link's target — refuse instead,
                # the same trust rule the sentinel path applies.
                if _dir_state(directory) != "dir":
                    return None
                # Markers outlive their process only when it died without
                # running its cleanup (SIGKILL, power loss). Nothing else ever
                # walks this directory with the mutation lock held, so
                # registration is the one place that can collect them; the
                # snapshot readers stay read-only by contract.
                _gc_stale_presence(directory, deadline)
                path = directory / name
                # The nonce makes this filename fresh — never reuse or unlink
                # an existing entry here; probe+grace GC above owns that.
                fp = open(path, "a+b")
                try:
                    portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
                except _LOCK_CONTENDED:
                    fp.close()
                    return None
                inst = RegisteredInstance(path=path, pid=pid, _fp=fp)
                # Published under the mutation lock, as sentinels are, so the
                # file is never visible to a same-process read before the
                # in-memory record exists.
                with _state_guard:
                    _active_presence[path] = inst
                    global _atexit_installed
                    if not _atexit_installed:
                        atexit.register(_atexit_cleanup)
                        _atexit_installed = True
                return inst
        except _MutationLockTimeout:
            logger.debug("presence registration lost the sidecar race", exc_info=True)
        return None
    except Exception:
        logger.debug("presence registration failed", exc_info=True)
        return None


def sweep_stale_presence() -> None:
    """Collect abandoned presence markers under every known runtime root.

    For the destructive CLIs (#2230). A marker is *not* inventoried, staged
    or deleted the way a sentinel is: a handshake-only server that is still
    running holds its marker's flock, and moving that file would both
    silently unregister a live process and, on Windows, fail outright on the
    open handle. So this removes exactly what its owner can no longer remove
    — unlocked, past the publication grace window — and leaves everything
    else, which is also why it is safe to run while servers are starting.

    Never raises: a coordination directory that cannot be swept is residue
    in a volatile temp tree, not a reason to abort an uninstall.
    """
    try:
        canonical = runtime_dir()
        roots, _refusal = _candidate_registry_roots()
    except OSError:
        return
    for root in roots or ():
        try:
            # Anchor *and* leaf are checked, the same pair
            # ``mm uninstall``'s own registry listing checks: a junctioned
            # runtime dir leaves an ordinary ``presence/`` inside the target,
            # which passes every test made on the leaf alone. Deliberately
            # ``_dir_state`` rather than ``validate_runtime_dir`` — the
            # historical roots were already trust-filtered by
            # ``_candidate_registry_roots``, which admits the canonical root
            # unvalidated, and what this pass needs from a root is that it
            # does not redirect, not that it is owner-only.
            if _dir_state(root) != "dir":
                continue
            directory = presence_dir() if root == canonical else presence_dir(root)
            if _dir_state(directory) != "dir":
                continue
            lock_root = None if root == canonical else root
            # One budget per root for acquiring the lock and sweeping under
            # it, so a large residue directory cannot hold the shared lock
            # for an unbounded time while a destructive command waits.
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            with _mutation_lock(deadline, root=lock_root):
                _gc_stale_presence(directory, deadline)
        except (_MutationLockTimeout, OSError):
            continue
        except Exception:
            logger.debug("presence sweep failed for %s", root, exc_info=True)
            continue


def _gc_stale_presence(directory: Path, deadline: float) -> None:
    """Collect unlocked, aged presence markers, bounded by ``deadline``.

    Runs with the shared mutation lock held, which is why the deadline is not
    optional: that lock also serializes sentinel registration, the
    concurrent-writer enumeration and the uninstall probe, and this sweep runs
    on a startup path that a client is waiting on. An accumulated directory —
    exactly what a host with this problem has — would otherwise let an
    unbounded probe loop hold the lock for as long as it takes. Collecting
    part of the residue and stopping is correct: the next registration
    resumes, and the entries left behind are already inert.

    Streamed with ``scandir`` rather than a sorted listing for the same
    reason: order is irrelevant to collection, and materializing thousands of
    paths before the first check is work done inside the lock for nothing.
    """
    with _state_guard:
        own = set(_active_presence)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    return
                if _ENTRY_RE.match(entry.name) is None:
                    continue
                path = Path(entry.path)
                if path in own:
                    continue
                if _probe_entry(path) == "stale" and _aged(path):
                    _gc_stale_entry(path)
    except OSError:
        return


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


def _nofollow_opener(path: str, flags: int) -> int:
    """``open`` opener that refuses a symlink final component (#1938).

    ``O_NOFOLLOW`` is a no-op fallback (``0``) where the platform lacks
    it (Windows); its junction/symlink redirects are caught by
    :func:`_dir_state` and the leading no-follow ``stat`` instead.
    """
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))


def _denial_verdict(exc: PermissionError) -> Literal["unknown", "untrusted"]:
    """Classify a ``PermissionError`` accessing a sentinel (#1938).

    Durable denial — a mode-000 / root-owned entry, or a listable but
    *unsearchable* (``0o400``) ``instances/`` that ``iterdir`` enumerates
    yet blocks the per-entry ``stat`` — is persistent: ``untrusted``, so
    the caller names the path instead of prescribing "retry". A Windows
    sharing / lock violation (``winerror`` 32/33) is transient contention
    and stays ``unknown``.
    """
    if getattr(exc, "winerror", None) in _WIN_TRANSIENT_SHARING:
        return "unknown"
    return "untrusted"


def _probe_entry(path: Path) -> Literal["live", "stale", "gone", "unknown", "untrusted"]:
    """Flock-probe one sentinel. The lock, not the recorded pid, is
    authoritative (pid reuse — see ``cli/_liveness.py``). On ``stale``
    the caller decides about GC; the probe itself releases immediately.

    Contention and uncertainty are distinct here: only the known
    contention shapes (POSIX ``BlockingIOError``, portalocker's Windows
    ``LockException``) mean ``live``; any other ``OSError`` at lock time
    is an I/O failure and reports ``unknown`` — claiming ``live`` on it
    would let a transient error fabricate a concurrent-writer warning
    (the status surface is fail-open) or a false uninstall refusal.

    A no-follow ``stat`` gates the open, mirroring :func:`_dir_state`
    one level down (#1938): a sentinel must be a regular file. Anything
    else — a stray subdirectory, a symlink (a healthy one would follow
    silently and flock an *unrelated* file, fabricating live/stale), a
    fifo (whose ``open`` could even block), a junction — is
    ``untrusted``: a *persistent* cause the caller names and asks the
    user to remove, not "retry". The stat→open pair is a TOCTOU window
    (an entry could be swapped for a symlink between them), closed on two
    fronts: the open adds ``O_NOFOLLOW`` (``ELOOP`` → ``untrusted`` on
    POSIX; a no-op where the platform lacks it, e.g. Windows), and after
    opening, the descriptor must be a regular file whose ``st_dev``/
    ``st_ino`` match a fresh no-follow ``stat`` of the path. That catches
    a redirect slipping past ``O_NOFOLLOW``: a symlink swapped in — even
    one pointing back at the original inode — has its own distinct inode
    under the no-follow ``stat``, so the identity check fails; a
    fifo/device swap fails ``S_ISREG`` on the descriptor.
    ``NotADirectoryError`` is unreachable: the parent was already
    validated as a real directory by :func:`_dir_state` under the same
    mutation lock.

    Only *durable* denial is ``untrusted`` (see :func:`_denial_verdict`).
    A ``PermissionError`` accessing this exact entry — at the pre-open
    ``stat`` (an unsearchable ``0o400`` ``instances/`` that still lists
    the name) or at the open (mode-000 / root-owned entry) — is
    persistent and precisely attributable, so ``untrusted``. That does
    not violate #1942's rule that an arbitrary ``PermissionError``
    (sidecar open, an entry's unlock/close) stays ``unknown`` — that rule
    guards against blaming a path that may be fine, whereas these are
    raised *for the entry itself*. A Windows sharing / lock violation, by
    contrast, *is* transient (antivirus or another handle holding the
    file for a moment) and stays ``unknown`` so the caller does not
    prescribe remove/repair for a condition retrying can clear (#1938).
    Post-open failures (lock-time ``OSError``, unlock/close) also stay
    ``unknown``; the caller's loop absorbs any escaping exception so a
    later entry cannot demote an ``untrusted`` already seen.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return "gone"
    except PermissionError as exc:
        # An unsearchable ``instances/`` (``0o400``) lists the entry but
        # denies this per-entry stat — durable, so ``untrusted`` (#1938).
        return _denial_verdict(exc)
    except OSError:
        return "unknown"
    if not stat.S_ISREG(st.st_mode):
        return "untrusted"
    try:
        # ``opener`` injects ``O_NOFOLLOW`` while keeping ``open``'s file
        # object (so ``fp.name`` stays the path, not a bare fd).
        fp = open(path, "rb+", opener=_nofollow_opener)
    except FileNotFoundError:
        return "gone"
    except PermissionError as exc:
        # Durable denial (mode-000 / root-owned) is persistent; a Windows
        # sharing/lock violation is transient.
        return _denial_verdict(exc)
    except OSError as exc:
        # ``ELOOP`` — a symlink raced in after the stat and ``O_NOFOLLOW``
        # refused it; persistent. Any other ``OSError`` is a transient
        # I/O failure.
        if exc.errno == errno.ELOOP:
            return "untrusted"
        return "unknown"
    try:
        try:
            fst = os.fstat(fp.fileno())
            lst = os.stat(path, follow_symlinks=False)
        except OSError:
            return "unknown"
        # The open descriptor must be a regular file *and* the same inode
        # the current no-follow path resolves to. A redirect that slips
        # past ``O_NOFOLLOW`` (a no-op on Windows) is caught here rather
        # than flock-probed: a symlink swapped in — even one pointing back
        # at the original inode — has its *own* distinct inode under the
        # no-follow ``stat``, so ``fst != lst``; a fifo/device swap fails
        # ``S_ISREG(fst)`` after the open returns (#1938).
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (
            lst.st_dev,
            lst.st_ino,
        ):
            return "untrusted"
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


def _candidate_registry_roots() -> tuple[list[Path] | None, tuple[Path, OSError] | None]:
    """Resolve canonical plus existing historical registry roots.

    ``None`` means the candidate set itself could not be resolved.  A returned
    ``(path, error)`` records an existing candidate that failed the writer
    trust policy; advisory callers become incomplete, destructive callers
    report it as UNTRUSTED.
    """
    try:
        canonical = runtime_dir()
        candidates = candidate_runtime_dirs()
    except OSError:
        logger.debug("instance-registry roots could not be resolved", exc_info=True)
        return None, None

    if not candidates or candidates[0] != canonical:
        candidates = [canonical]

    roots: list[Path] = []
    refusal: tuple[Path, OSError] | None = None
    for candidate in candidates:
        if candidate == canonical:
            roots.append(candidate)
            continue
        try:
            if validate_runtime_dir(candidate):
                roots.append(candidate)
        except OSError as exc:
            if refusal is None:
                refusal = (candidate, exc)
    return roots, refusal


def _active_for_root(root: Path) -> dict[Path, "RegisteredInstance"]:
    """Return the in-process registrations published beneath ``root``."""
    return {path: inst for path, inst in _active.items() if path.parent.parent == root}


def _active_presence_for_root(root: Path) -> dict[Path, "RegisteredInstance"]:
    """Return the in-process presence markers published beneath ``root`` (#2230).

    Separate from :func:`_active_for_root` because the dicts are separate:
    both populations sit one level under the root, so a shared dict would
    make ``presence/`` entries answer questions asked about ``instances/``.
    """
    return {path: inst for path, inst in _active_presence.items() if path.parent.parent == root}


def _enumerate_live_instances_at(
    root: Path, store_digest: str, deadline: float
) -> EnumerationResult:
    """Enumerate one canonical or historical registry under one deadline."""
    results: list[InstanceInfo] = []
    complete = True
    try:
        canonical = runtime_dir()
        lock_root = None if root == canonical else root
        with _mutation_lock(deadline, root=lock_root):
            with _state_guard:
                own = _active_for_root(root)
            for path in own:
                info = _parse_entry(path)
                if info is not None and info.digest == store_digest:
                    results.append(info)
            directory = instances_dir() if lock_root is None else instances_dir(root)
            dir_state = _dir_state(directory)
            if dir_state == "missing":
                return EnumerationResult(_sorted(results), True)
            if dir_state == "untrusted":
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
                elif state == "stale":
                    if _aged(entry):
                        _gc_stale_entry(entry)
                elif state in ("unknown", "untrusted"):
                    complete = False
    except (_MutationLockTimeout, _RuntimeDirRefused):
        return EnumerationResult(_sorted(results), False)
    except Exception:
        logger.debug("instance enumeration failed for %s", root, exc_info=True)
        return EnumerationResult(_sorted(results), False)
    return EnumerationResult(_sorted(results), complete)


def enumerate_live_instances(store_digest: str) -> EnumerationResult:
    """Enumerate live registrations for one store across all known roots.

    This process's own active registrations are included directly from
    ``_active`` without probing — the in-memory record is authoritative
    for locks this process already holds. The skip is an optimization, not
    a correctness requirement: a self-directed probe was measured to
    *contend* on Windows (#2102 — a second same-process handle asking for
    ``LOCK_EX | LOCK_NB`` is refused with ``AlreadyLocked``; measured on
    portalocker 3.1.1, 3.2.0 and 4.1.0, which is not every release the
    ``>=3.0`` floor admits), as POSIX ``flock``
    on a second open file description does, so probing ourselves would
    answer ``live`` — the correct answer, at the cost of an open and a
    lock call. Skipping also keeps enumeration independent of backend
    drift; the measurement is pinned by
    ``tests/test_windows_lock_semantics.py``. Every other entry, same-pid
    ones included, is probed. Stale entries older than the grace period are
    opportunistically removed; unparseable names follow the same
    unlocked+grace rule. Fails open: any uncertainty yields
    ``complete=False`` and the caller must not warn.

    Canonical and pre-#2037 environment-derived registries are aggregated.
    One shared deadline bounds candidate resolution, lock acquisition, and
    traversal. Any incomplete root keeps the advisory surface silent.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    roots, refusal = _candidate_registry_roots()
    if roots is None:
        return EnumerationResult((), False)
    results: list[InstanceInfo] = []
    complete = refusal is None
    for root in roots:
        outcome = _enumerate_live_instances_at(root, store_digest, deadline)
        results.extend(outcome.instances)
        complete = complete and outcome.complete
    return EnumerationResult(_sorted(results), complete)


def _sorted(results: list[InstanceInfo]) -> tuple[InstanceInfo, ...]:
    return tuple(sorted(results, key=lambda i: (i.pid, i.procid)))


@dataclass(frozen=True)
class _RootScan:
    """One root's read-only scan, carrying why it degraded rather than a bool."""

    instances: tuple[InstanceInfo, ...]
    complete: bool
    stale_seen: int
    unlocked_fresh_seen: int
    unparseable_seen: int
    error: OSError | None


@dataclass(frozen=True)
class RegistrySnapshot:
    """Every live registration this host can see, across stores and roots.

    The counterpart to :class:`EnumerationResult`, which answers "who else is
    writing *my* store". A machine can hold dozens of servers spread over
    several stores and every store-scoped view reports nothing, which is the
    observability gap #2226 describes; this is the unfiltered read.

    ``canonical_error`` is kept apart from ``refusal`` on purpose. A historical
    root that fails the trust policy is a degraded scan (some entries may be
    unseen); the *canonical* root failing means this host's coordination
    directory is unusable, which is a finding in its own right and must be
    reportable as a failure rather than dissolving into ``complete=False``.

    ``presence`` is the startup population (#2230) and is kept in its own
    field rather than folded into ``instances``: a marker says only "this
    process is a live server", while a sentinel says "this process has
    *this store* open". Every existing consumer reads ``instances`` and
    keeps its current meaning by construction. The two are joined on
    ``procid`` — never on ``digest``, which is a different function of a
    different input on each side.
    """

    instances: tuple[InstanceInfo, ...]
    complete: bool
    stale_seen: int
    unlocked_fresh_seen: int
    unparseable_seen: int
    roots_consulted: int
    canonical_error: OSError | None
    refusal: tuple[Path, OSError] | None
    presence: tuple[InstanceInfo, ...] = ()
    presence_stale_seen: int = 0
    presence_unlocked_fresh_seen: int = 0
    presence_unparseable_seen: int = 0


def _scan_registry_root(root: Path, deadline: float) -> _RootScan:
    """Read one registry root without changing anything under it.

    Deliberately not :func:`_enumerate_live_instances_at`. That walker garbage-
    collects aged stale sentinels as it goes and takes the mutation sidecar,
    which creates the runtime directory and the sidecar file when they are
    absent. Both are wrong for a diagnostic: a report that deletes evidence
    cannot be run twice to compare, and a tool asked to inspect a machine must
    not leave new coordination state on it. So this takes no sidecar lock —
    reads here are advisory, and each entry's flock probe remains individually
    atomic, so the only cost is that a registration landing mid-walk may be
    missed, which ``complete`` already exists to express.

    Dropping the sidecar lock must not also drop the validation it implied:
    ``_candidate_registry_roots`` admits the canonical root *unvalidated*, so
    this checks every root itself before touching anything beneath it. Without
    that, a symlinked or junctioned runtime dir would be traversed here.
    """
    return _scan_entry_dir(root, deadline, kind="instances")


def _scan_entry_dir(
    root: Path, deadline: float, *, kind: Literal["instances", "presence"]
) -> _RootScan:
    """Read one root's ``instances/`` or ``presence/`` directory, read-only.

    The two populations share a filename grammar, a liveness rule and a
    staleness policy, and differ only in which directory holds them and
    which in-process dict already knows about our own entries — so they
    share the walk. They are never merged into one result: what a marker
    proves ("this process is a server") is weaker than what a sentinel
    proves ("this process has this store open"), and only the sentinel
    population may reach the concurrent-writer signal or the uninstall
    probe. Callers keep them apart, which is why ``kind`` selects a
    directory rather than the walk taking both.

    The root is validated here, per call, rather than once by the caller:
    ``_candidate_registry_roots`` admits the canonical root *unvalidated*,
    and a symlinked or junctioned runtime dir must not be traversed by
    either walk. One extra no-follow ``stat`` per root is the price of
    neither walk depending on the other having run first.
    """
    try:
        if not validate_runtime_dir(root):
            # Absent is not a failure: a host that has never run a server has
            # no registry, and creating one to look at it is exactly what this
            # function must not do.
            return _RootScan((), True, 0, 0, 0, None)
    except OSError as exc:
        return _RootScan((), False, 0, 0, 0, exc)

    results: list[InstanceInfo] = []
    complete = True
    stale = 0
    unlocked_fresh = 0
    unparseable = 0
    try:
        canonical = runtime_dir()
        resolve = instances_dir if kind == "instances" else presence_dir
        pool = _active_for_root if kind == "instances" else _active_presence_for_root
        with _state_guard:
            own = pool(root)
        for path in own:
            info = _parse_entry(path)
            if info is not None:
                results.append(info)
        directory = resolve() if root == canonical else resolve(root)
        dir_state = _dir_state(directory)
        if dir_state == "missing":
            return _RootScan(_sorted(results), True, 0, 0, 0, None)
        if dir_state == "untrusted":
            return _RootScan(_sorted(results), False, 0, 0, 0, None)
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            return _RootScan(_sorted(results), False, 0, 0, 0, exc)
        for entry in entries:
            if entry in own:
                continue
            if time.monotonic() >= deadline:
                complete = False
                break
            info = _parse_entry(entry)
            state = _probe_entry(entry)
            if state == "live":
                if info is not None:
                    results.append(info)
                else:
                    # Held by someone, but we cannot attribute it to a pid or a
                    # store. The count below is then a lower bound, so say so.
                    unparseable += 1
                    complete = False
            elif state == "stale":
                # An unlocked sentinel inside the grace window is a healthy
                # registration caught between create and flock-acquire, not
                # residue — counting it as stale would report a starting server
                # as a problem. ``_aged`` cannot make that call here because it
                # answers False for a stat failure too, which would file an
                # entry we could not read as "still starting up".
                try:
                    age = time.time() - entry.stat().st_mtime
                except FileNotFoundError:
                    # Collected or released between probe and stat: it is simply
                    # not there any more, which is not a hygiene observation.
                    continue
                except OSError:
                    complete = False
                    continue
                if age > _STALE_GRACE_S:
                    stale += 1
                else:
                    unlocked_fresh += 1
            elif state in ("unknown", "untrusted"):
                complete = False
    except OSError as exc:
        logger.debug("registry snapshot failed for %s", root, exc_info=True)
        return _RootScan(_sorted(results), False, stale, unlocked_fresh, unparseable, exc)
    except Exception:
        logger.debug("registry snapshot failed for %s", root, exc_info=True)
        return _RootScan(_sorted(results), False, stale, unlocked_fresh, unparseable, None)
    return _RootScan(_sorted(results), complete, stale, unlocked_fresh, unparseable, None)


def snapshot_all_instances() -> RegistrySnapshot:
    """Read every live registration on this host, across all stores and roots.

    Read-only (see :func:`_scan_registry_root`): nothing is created, nothing is
    garbage-collected. Bounded by one shared deadline like
    :func:`enumerate_live_instances`, and fails open the same way — an
    incomplete pass yields a lower bound, never evidence of absence.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    roots, refusal = _candidate_registry_roots()
    if roots is None:
        return RegistrySnapshot((), False, 0, 0, 0, 0, None, None)

    try:
        canonical: Path | None = runtime_dir()
    except OSError:
        canonical = None

    results: list[InstanceInfo] = []
    presence: list[InstanceInfo] = []
    complete = refusal is None
    stale = unlocked_fresh = unparseable = 0
    p_stale = p_unlocked_fresh = p_unparseable = 0
    canonical_error: OSError | None = None
    for root in roots:
        scan = _scan_registry_root(root, deadline)
        results.extend(scan.instances)
        complete = complete and scan.complete
        stale += scan.stale_seen
        unlocked_fresh += scan.unlocked_fresh_seen
        unparseable += scan.unparseable_seen
        if scan.error is not None and root == canonical and canonical_error is None:
            canonical_error = scan.error
        # Same roots, same deadline: a marker directory that does not exist
        # (a host on a pre-#2230 server, or a historical root) simply scans
        # empty, so multi-root parity needs no separate policy. ``complete``
        # is shared — a degraded marker scan makes the whole snapshot a
        # lower bound, which is what every consumer already reads it as.
        p_scan = _scan_entry_dir(root, deadline, kind="presence")
        presence.extend(p_scan.instances)
        complete = complete and p_scan.complete
        p_stale += p_scan.stale_seen
        p_unlocked_fresh += p_scan.unlocked_fresh_seen
        p_unparseable += p_scan.unparseable_seen
        if p_scan.error is not None and root == canonical and canonical_error is None:
            canonical_error = p_scan.error
    return RegistrySnapshot(
        instances=_sorted(results),
        complete=complete,
        stale_seen=stale,
        unlocked_fresh_seen=unlocked_fresh,
        unparseable_seen=unparseable,
        roots_consulted=len(roots),
        canonical_error=canonical_error,
        refusal=refusal,
        presence=_sorted(presence),
        presence_stale_seen=p_stale,
        presence_unlocked_fresh_seen=p_unlocked_fresh,
        presence_unparseable_seen=p_unparseable,
    )


def _probe_registry_root_for_uninstall(root: Path, deadline: float) -> UninstallProbeResult:
    """Run the fail-closed all-store probe against one registry root."""
    try:
        canonical = runtime_dir()
        lock_root = None if root == canonical else root
        with _mutation_lock(deadline, root=lock_root):
            with _state_guard:
                if _active_for_root(root):
                    return UninstallProbeResult("LIVE")
            directory = instances_dir() if lock_root is None else instances_dir(root)
            dir_state = _dir_state(directory)
            if dir_state == "missing":
                return UninstallProbeResult("NONE")
            if dir_state == "untrusted":
                return UninstallProbeResult(
                    "UNTRUSTED", untrusted_path=directory, untrusted_kind="redirected"
                )
            try:
                entries = list(directory.iterdir())
            except PermissionError:
                return UninstallProbeResult(
                    "UNTRUSTED", untrusted_path=directory, untrusted_kind="unprobeable"
                )
            except OSError:
                return UninstallProbeResult("UNKNOWN")
            untrusted_entry: Path | None = None
            saw_unknown = False
            for entry in entries:
                if time.monotonic() >= deadline:
                    saw_unknown = True
                    break
                try:
                    state = _probe_entry(entry)
                except (OSError, portalocker.LockException):
                    logger.debug("entry probe raised, treating as unknown", exc_info=True)
                    saw_unknown = True
                    continue
                if state == "live":
                    return UninstallProbeResult("LIVE")
                if state == "untrusted":
                    if untrusted_entry is None:
                        untrusted_entry = entry
                elif state == "unknown":
                    saw_unknown = True
            if untrusted_entry is not None:
                return UninstallProbeResult(
                    "UNTRUSTED",
                    untrusted_path=untrusted_entry,
                    untrusted_kind="unprobeable",
                )
            if saw_unknown:
                return UninstallProbeResult("UNKNOWN")
            return UninstallProbeResult("NONE")
    except _MutationLockTimeout:
        return UninstallProbeResult("UNKNOWN")
    except _RuntimeDirRefused as exc:
        logger.debug("uninstall registry probe refused runtime dir", exc_info=True)
        return UninstallProbeResult(
            "UNTRUSTED",
            untrusted_path=exc.path,
            untrusted_kind="redirected",
            detail=str(exc),
        )
    except Exception:
        logger.debug("uninstall registry probe failed", exc_info=True)
        return UninstallProbeResult("UNKNOWN")


def probe_all_for_uninstall() -> UninstallProbeResult:
    """All-store, fail-closed probe for ``mm uninstall``.

    ``LIVE`` — at least one held sentinel (any store; deleting the
    registry under a live server is never acceptable, whatever store it
    has open). ``UNKNOWN`` — the pass could not complete (lock timeout,
    a transient I/O failure, deadline): uninstall must refuse, a timeout
    never means "empty". ``UNTRUSTED`` — a probe path is not a private
    real directory (symlinked/junctioned ``instances/``, or a runtime
    dir ``ensure_runtime_dir`` refuses), *or* the directory cannot be
    listed, *or* a single entry is not a probeable regular file (stray
    subdirectory, link, permission-denied path — #1938): uninstall must
    refuse *and* tell the user which path to remove or repair — "retry"
    is wrong advice for these causes (#1942). ``NONE`` — a fully
    completed pass found zero live sentinels. Unlike the status path
    this performs no GC — an uninstall should not mutate the registry it
    is about to judge.

    Verdict precedence within the entry scan is LIVE > UNTRUSTED >
    UNKNOWN: a live sentinel returns immediately, but a transient
    ``unknown`` on an earlier entry must not mask a persistent
    ``untrusted`` on a later one — that would send the user back into
    the retry loop against a condition that never clears. So the loop
    remembers the first untrusted path and any unknown, and resolves by
    precedence at loop end and at deadline expiry alike.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    roots, refused = _candidate_registry_roots()
    if roots is None:
        return UninstallProbeResult("UNKNOWN")
    first_untrusted: UninstallProbeResult | None = None
    saw_unknown = False
    if refused is not None:
        path, exc = refused
        first_untrusted = UninstallProbeResult(
            "UNTRUSTED",
            untrusted_path=path,
            untrusted_kind="redirected",
            detail=str(exc),
        )
    for root in roots:
        result = _probe_registry_root_for_uninstall(root, deadline)
        if result.state == "LIVE":
            return result
        if result.state == "UNTRUSTED" and first_untrusted is None:
            first_untrusted = result
        elif result.state == "UNKNOWN":
            saw_unknown = True
    if first_untrusted is not None:
        return first_untrusted
    if saw_unknown:
        return UninstallProbeResult("UNKNOWN")
    return UninstallProbeResult("NONE")
