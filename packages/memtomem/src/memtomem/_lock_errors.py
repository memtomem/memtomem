"""Classify a ``portalocker`` lock-call failure: contention vs. I/O failure.

Every non-blocking lock poll in this codebase has the same fork. A held lock
is *contention* — the caller waits, retries, and eventually reports "busy,
try again". Anything else (``EIO``, ``ENOLCK``, ``EBADF``, an NFS
``EOFError``, a non-lock-violation Win32 error) is a **lock-call I/O
failure**: retrying cannot clear it, and reporting it as contention sends
the operator hunting for a holder that does not exist (#1870).

The split lived in :mod:`memtomem._instance_registry` (#1957) until
``context/_atomic.py``'s ``_file_lock`` needed the same judgement (#2229):
its poll loop treated *every* failure as contention, burning the whole
acquisition budget on a permanent error and then blaming "another process".
This module is a leaf — it imports nothing from ``memtomem`` — so both the
registry and the atomic-write layer can share one classifier without a
cycle.

Both helpers take the exception, never a return code: portalocker's
``LockException`` carries no ``errno`` (only the constant ``LOCK_FAILED``),
so the underlying error is reachable only through ``__cause__``.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import NoReturn

import portalocker

# The *catch* set for a non-blocking ``portalocker.lock`` — every shape it
# can produce, contention or not. POSIX raises ``BlockingIOError``;
# portalocker's Windows backend wraps Win32 errors as ``LockException``; a
# raw ``OSError`` can leak out of some backends. One documented hole:
# portalocker 3.x re-raises a non-``ERROR_LOCK_VIOLATION``
# ``pywintypes.error`` raw, and that type derives from ``Exception``, not
# ``OSError``. Callers that must survive *that* use
# :data:`LOCK_CALL_ERRORS_WIDE` below; the ones whose own ``except`` arms
# absorb a raw error keep this narrower catch, matching
# ``cli/_liveness.py:probe_pid_file`` (#817).
LOCK_CALL_ERRORS: tuple[type[BaseException], ...] = (
    portalocker.LockException,
    BlockingIOError,
    OSError,
)

# The same set widened for the hole above. On portalocker 3.x the Win32
# backend maps ``ERROR_LOCK_VIOLATION`` to ``AlreadyLocked`` but re-raises
# every *other* ``pywintypes.error`` **raw**, and that type derives from
# ``Exception``, not ``OSError``, so the tuple above would let a
# non-contention Win32 lock failure escape unclassified — past the CLIs'
# ``except OSError`` repair branch (#1957) and past
# ``wiki/commit.py``'s classifier (#2229). 4.0.0 closed that upstream (the
# ``else`` branch now wraps in ``LockException``), but the floor stays
# ``portalocker>=3.0``, so a 3.x install still needs this catch — keep it
# until the floor moves. Any caller that *promises* a classified failure
# (the lifecycle barrier, ``context._atomic``) catches this one.
# The ``import`` is Windows-only and best-effort — absent pywin32,
# portalocker could not have raised it, so the POSIX tuple is complete.
try:
    import pywintypes as _pywintypes

    LOCK_CALL_ERRORS_WIDE: tuple[type[BaseException], ...] = (
        *LOCK_CALL_ERRORS,
        _pywintypes.error,
    )
except ImportError:
    LOCK_CALL_ERRORS_WIDE = LOCK_CALL_ERRORS

# Non-blocking-lock errnos that mean "held by someone else": POSIX
# ``fcntl.flock`` documents both ``EACCES`` and ``EAGAIN`` for a held lock,
# and the POSIX backend of every source-verified release (3.0 through 4.1)
# maps exactly this pair to ``AlreadyLocked``. Scoped to POSIX on purpose —
# the Windows msvcrt backend treats a wider errno set as contention, and the
# floor is a floor, so a release past 4.1 is unverified by construction.
# These errnos are therefore *not* the primary gate — they are the defensive
# one, and the ``>=3.0`` range is what the pin allows, not what it proves.
CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN})

# Windows ``ERROR_LOCK_VIOLATION`` — the one ``pywintypes.error`` code the
# Win32 backend maps to contention. ``pywintypes.error`` carries
# ``.winerror`` and *no* ``.errno`` (#1957 comment), so the errno gate
# cannot see it. Numeric on purpose: importing pywin32 here would add a
# Windows-only dependency for a single integer.
WINERROR_LOCK_VIOLATION = 33


def is_lock_contention(exc: BaseException) -> bool:
    """True when a non-blocking ``portalocker.lock`` failure means "held by
    someone else" rather than "the lock call itself failed" (#1957).

    Callers poll on ``True`` and let a ``False`` escape: the lifecycle
    barrier and ``context._atomic._file_lock`` normalize it to ``OSError``
    via :func:`raise_lock_io_failure`, the instance-registry mutation lock
    re-raises it straight into ``register_instance``'s never-raise handler.
    It is *not* reusable in a liveness probe as-is: that surface maps its
    own I/O uncertainty to ``"unknown"``, so a bare ``False`` there would
    need that translation.

    Across every source-verified release (portalocker 3.0/3.1/3.2 and
    4.0/4.1 — the floor is ``>=3.0``, so anything past 4.1 is unverified by
    construction) genuine contention is *always* the ``AlreadyLocked``
    subclass — POSIX ``EACCES``/``EAGAIN`` and Windows
    ``ERROR_LOCK_VIOLATION`` alike — so the ``isinstance`` check below
    catches it regardless of how the original error is chained. The
    errno/winerror probes are defensive: the cause probes cover a future
    version that might raise a bare ``LockException`` for a held lock (the
    #1944 type-drift note), and the ``winerror``-on-``exc`` probe covers a
    *raw* ``pywintypes.error`` (which the Win32 backend only ever re-raises
    for non-lock-violation codes, so in practice it is always
    non-contention — but a leaked raw code 33 must still read as contention,
    not as a path to repair). A lock-call I/O failure (``EIO``, ``ENOLCK``,
    ``EBADF``, an NFS ``EOFError``, a non-33 ``pywintypes.error``) matches
    none of these.
    """
    if isinstance(exc, (portalocker.AlreadyLocked, BlockingIOError)):
        return True
    # A raw ``EACCES``/``EAGAIN`` ``OSError`` leaking straight out of some
    # portalocker version is fcntl-documented contention; classify it as
    # such rather than as a path to repair (a deliberate superset of the
    # cause-based check below).
    if isinstance(exc, OSError) and exc.errno in CONTENTION_ERRNOS:
        return True
    # ``pywintypes.error`` exposes ``.winerror`` but no ``.errno`` (#1957
    # comment): probe it on the raw exception and on the chained cause.
    if getattr(exc, "winerror", None) == WINERROR_LOCK_VIOLATION:
        return True
    cause = exc.__cause__
    if isinstance(cause, OSError) and cause.errno in CONTENTION_ERRNOS:
        return True
    return getattr(cause, "winerror", None) == WINERROR_LOCK_VIOLATION


def raise_lock_io_failure(exc: BaseException, path: Path, *, label: str) -> NoReturn:
    """Normalize a non-contention lock-call failure to ``OSError`` (#1957).

    ``OSError`` is what callers already handle from this call site: the
    ``mkdir`` and ``open`` that *establish* a lock raise it, so a backend
    failure arriving as ``OSError`` adds no new type to any caller's
    surface. It lands in the branches written for exactly this class of
    problem — the destructive CLIs' repair-the-path remediation (#1951,
    #1959), ``wiki/commit.py``'s ``WikiLockUnavailableError`` (#2227) —
    instead of being flattened into stop-the-holder advice, which sends the
    user hunting for a process that does not exist (#1870). A ``portalocker``
    ``LockException`` is **not** an ``OSError``, so letting it propagate
    unwrapped would slip past every one of those branches (#2229).

    A chained ``OSError`` cause donates its errno/strerror/filename to a
    *fresh* ``OSError`` rather than being re-raised itself: ``raise cause
    from exc`` would make the pair each other's ``__cause__``/``__context__``
    — a reference cycle the caller's log does not need.

    ``path`` (the resolved lock file) backfills the filename: a lock syscall
    operates on a descriptor, so ``OSError.filename`` is usually ``None``
    and the ``EOFError`` fallback is always pathless — leaving the CLI to
    advise "repair the reported path" without naming one. The lock path is
    the only actionable path there is. ``label`` names the lock in the
    last-resort message ("lifecycle barrier", "file lock"), which is the
    only text a caller without an errno has to go on.
    """
    if isinstance(exc, OSError):
        # Mutate in place rather than re-wrap: keeps the precise subtype
        # (``FileNotFoundError`` etc.) while naming the path in ``str(exc)``.
        if exc.filename is None:
            exc.filename = str(path)
        raise exc
    cause = exc.__cause__
    if isinstance(cause, OSError) and cause.errno is not None:
        err = OSError(cause.errno, cause.strerror or str(cause))
        err.filename = cause.filename or str(path)
        raise err from exc
    raise OSError(f"{label} lock failed at {path}: {exc}") from exc
