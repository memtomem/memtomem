"""Process liveness probes shared by ``mm uninstall`` and ``mm upgrade``.

Both commands need to know whether a ``memtomem-server`` (or ``mm web``)
process is currently holding its pid lock file. The probe uses
``portalocker.lock(LOCK_EX | LOCK_NB)``
— if we can acquire it, no live writer is holding the file (it's a stale
leftover or fresh and unowned). If we cannot, a writer is alive, regardless
of whether the recorded PID is still valid or has been recycled.

Cross-platform via ``portalocker`` (POSIX ``fcntl.flock`` / Windows
``LockFileEx``); both surface the same non-blocking-acquire contract, so
the probe is real on every supported OS.

The legacy ``~/.memtomem/.server.pid`` path is the exception to the pure
exclusive probe: on POSIX it is classified shared-vs-exclusive first, and
only an exclusive holder (a genuine pre-0.1.25 server) or an
unclassifiable probe gates destructive work (#2003) — a shared holder is
the compatibility alias that 0.1.26-through-pre-#2003 servers took (current
servers take none), and such a server is gated by its own
``server[-<digest>].pid`` instead.

Runtime pid files are probed under *both* :func:`runtime_dir` branches, not
just the one this environment resolves: a server whose context differed
(``$XDG_RUNTIME_DIR`` set for it, unset for us, or the reverse) is otherwise
invisible and would be reported dead while holding the WAL.
"""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import portalocker

from memtomem._runtime_paths import (
    candidate_runtime_dirs,
    legacy_server_pid_path,
    runtime_dir,
    scrub_text,
    server_pid_path,
    store_pid_digest,
    validate_runtime_dir,
    web_pid_path,
)


_PID_PAYLOAD_MAX_BYTES = 4096
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class _UnsafeProbePathError(Exception):
    """A pid/metadata path could not be proven to be one stable regular file."""


def _exception_detail(exc: BaseException) -> str:
    if isinstance(exc, _UnsafeProbePathError):
        return scrub_text(str(exc))
    return f"{type(exc).__name__}: {scrub_text(str(exc))}"


def _verify_opened_regular(
    fd: int,
    path: Path,
    path_stat: os.stat_result,
    parent_stat: os.stat_result,
) -> None:
    """Require *fd*, *path*, and its parent to retain one no-follow identity."""
    try:
        descriptor_stat = os.fstat(fd)
        current_path_stat = os.stat(path, follow_symlinks=False)
        current_parent_stat = os.stat(path.parent, follow_symlinks=False)
        parent_is_junction = path.parent.is_junction()
    except OSError as exc:
        raise _UnsafeProbePathError(
            f"pid path changed during probe ({type(exc).__name__}: {exc})"
        ) from exc

    if not stat.S_ISREG(descriptor_stat.st_mode):
        raise _UnsafeProbePathError(f"pid path {path} is not a regular file")
    if not stat.S_ISREG(current_path_stat.st_mode):
        raise _UnsafeProbePathError(f"pid path {path} is not a regular file")
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ) or (current_path_stat.st_dev, current_path_stat.st_ino) != (
        path_stat.st_dev,
        path_stat.st_ino,
    ):
        raise _UnsafeProbePathError(f"pid path {path} changed identity during probe")
    if not stat.S_ISDIR(current_parent_stat.st_mode) or parent_is_junction:
        raise _UnsafeProbePathError(f"pid parent {path.parent} is redirected or not a directory")
    if (current_parent_stat.st_dev, current_parent_stat.st_ino) != (
        parent_stat.st_dev,
        parent_stat.st_ino,
    ):
        raise _UnsafeProbePathError(f"pid parent {path.parent} changed identity during probe")


def _open_verified_regular(
    path: Path, *, writable: bool
) -> tuple[int, os.stat_result, os.stat_result] | None:
    """Open one stable regular file without following its final component.

    ``None`` means the parent or file was absent at the initial snapshot. Once
    an entry has been observed, every open/stat/type/identity failure is
    raised as :class:`_UnsafeProbePathError` so liveness callers fail closed.
    ``O_NONBLOCK`` prevents a FIFO swapped in after the initial stat from
    blocking the open; the descriptor ``fstat`` rejects it before any read or
    lock operation.
    """
    try:
        parent_stat = os.stat(path.parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _UnsafeProbePathError(
            f"cannot inspect pid parent {path.parent} ({type(exc).__name__}: {exc})"
        ) from exc
    try:
        parent_is_junction = path.parent.is_junction()
    except OSError as exc:
        raise _UnsafeProbePathError(
            f"cannot inspect pid parent {path.parent} ({type(exc).__name__}: {exc})"
        ) from exc
    if stat.S_ISLNK(parent_stat.st_mode):
        # A dangling parent link cannot contain a live pid file. Preserve the
        # uninstall inventory boundary for a stale ``~/.memtomem`` link while
        # still refusing every parent link whose target exists.
        try:
            os.stat(path.parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _UnsafeProbePathError(
                f"cannot inspect pid parent {path.parent} ({type(exc).__name__}: {exc})"
            ) from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_is_junction:
        raise _UnsafeProbePathError(f"pid parent {path.parent} is redirected or not a directory")

    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _UnsafeProbePathError(
            f"cannot inspect pid path {path} ({type(exc).__name__}: {exc})"
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise _UnsafeProbePathError(f"pid path {path} is not a regular file")

    flags = (os.O_RDWR if writable else os.O_RDONLY) | _NOFOLLOW | _NONBLOCK | _BINARY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _UnsafeProbePathError(
            f"cannot open pid path {path} ({type(exc).__name__}: {exc})"
        ) from exc
    try:
        _verify_opened_regular(fd, path, path_stat, parent_stat)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    return fd, path_stat, parent_stat


def _read_bounded_fd(fd: int, limit: int = _PID_PAYLOAD_MAX_BYTES) -> bytes | None:
    """Read through EOF or ``limit + 1``; ``None`` means oversized."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        block = os.read(fd, limit + 1 - size)
        if not block:
            return b"".join(chunks)
        chunks.append(block)
        size += len(block)
    return None


def _read_bounded_regular_file(path: Path, limit: int = _PID_PAYLOAD_MAX_BYTES) -> bytes | None:
    """Best-effort bounded read of a stable, no-follow regular file."""
    try:
        opened = _open_verified_regular(path, writable=False)
    except (OSError, _UnsafeProbePathError):
        return None
    if opened is None:
        return None
    fd, path_stat, parent_stat = opened
    try:
        try:
            payload = _read_bounded_fd(fd, limit)
            if payload is None:
                return None
            _verify_opened_regular(fd, path, path_stat, parent_stat)
            return payload
        except (OSError, _UnsafeProbePathError):
            return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@dataclass(frozen=True)
class ServerState:
    alive: bool
    pid: int | None
    pid_file: Path | None
    port: int | None = None
    started: str | None = None
    # #1949: set only when ``alive=True`` is a fail-closed *assumption* —
    # the probe could not inspect the lock file at all (``exists()`` or
    # ``open()`` raised) — never when a held flock was actually observed.
    # Callers that print "flock is held by an active writer" must check
    # this first: that claim is evidence-based, and a failed probe has no
    # evidence. ``None`` means the ``alive`` verdict is real.
    probe_error: str | None = None
    # The legacy path is special on POSIX: modern servers take a shared
    # compatibility lock while pre-0.1.25 servers take it exclusively.
    # Upgrade uses this distinction to avoid signaling a stale pid from a
    # shared alias while still stopping a genuinely separate old server.
    legacy_lock_mode: Literal["shared", "exclusive"] | None = None


def _parse_pid_payload(text: str) -> tuple[int | None, int | None, str | None]:
    """Parse pid-file payloads.

    Legacy server pid files are a single ``pid`` line. Current servers and
    ``mm web`` write ``pid`` / optional ``port`` / ``started`` on separate
    lines. The first line stays the pid so older call sites that only care
    about the process id remain compatible.
    """
    lines = [line.strip() for line in text.splitlines()]
    try:
        pid = int(lines[0]) if lines and lines[0] else None
    except ValueError:
        pid = None
    try:
        port = int(lines[1]) if len(lines) > 1 and lines[1] else None
    except ValueError:
        port = None
    started = lines[2] if len(lines) > 2 and lines[2] else None
    return pid, port, started


def probe_pid_file(pid_file: Path) -> ServerState:
    """Probe a single pid file via ``portalocker``.

    ``server/__init__.py:main`` opens this file and holds an exclusive
    lock for the entire server lifetime. If we can acquire
    ``LOCK_EX | LOCK_NB`` on it, no live writer is holding it. If we
    cannot, a writer is alive — regardless of whether the recorded PID
    is still valid (kernel may have recycled it; see #387).

    Real probe on every OS; portalocker dispatches to ``fcntl.flock`` on
    POSIX and ``LockFileEx`` on Windows. Replaces the prior conservative
    "pid file exists → assume alive" Windows fallback (see #448, #625).
    """
    try:
        opened = _open_verified_regular(pid_file, writable=True)
    except (OSError, _UnsafeProbePathError) as exc:
        return ServerState(
            alive=True,
            pid=None,
            pid_file=pid_file,
            probe_error=_exception_detail(exc),
        )
    if opened is None:
        return ServerState(alive=False, pid=None, pid_file=None)

    fd, path_stat, parent_stat = opened
    pid: int | None = None
    port: int | None = None
    started: str | None = None
    try:
        try:
            payload = _read_bounded_fd(fd)
        except OSError:
            payload = None
        if payload is not None:
            try:
                pid, port, started = _parse_pid_payload(payload.decode("utf-8"))
            except UnicodeDecodeError:
                pass

        # portalocker's Windows backend locks from the current file offset and
        # requires a writable handle. Reset after the bounded metadata read,
        # then wrap this same verified descriptor as ``rb+``.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            fp = os.fdopen(fd, "rb+", buffering=0)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.close(fd)
            return ServerState(
                alive=True,
                pid=None,
                pid_file=pid_file,
                probe_error=_exception_detail(exc),
            )
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise

    lock_owned = False
    try:
        contended = False
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
            lock_owned = True
        except (portalocker.AlreadyLocked, BlockingIOError):
            # Genuine contention is positive liveness evidence. Metadata may
            # still be absent on Windows (mandatory range lock) or because a
            # bounded/UTF-8 payload check rejected it.
            contended = True
        except (portalocker.LockException, OSError) as exc:
            return ServerState(
                alive=True,
                pid=pid,
                pid_file=pid_file,
                port=port,
                started=started,
                probe_error=_exception_detail(exc),
            )

        try:
            # The PID is signalable only if the path still names the exact
            # regular inode whose bytes and lock state were inspected.
            _verify_opened_regular(fp.fileno(), pid_file, path_stat, parent_stat)
        except _UnsafeProbePathError as exc:
            return ServerState(
                alive=True,
                pid=None,
                pid_file=pid_file,
                probe_error=_exception_detail(exc),
            )

        if contended:
            return ServerState(
                alive=True,
                pid=pid,
                pid_file=pid_file,
                port=port,
                started=started,
            )
        portalocker.unlock(fp)
        lock_owned = False
        return ServerState(alive=False, pid=pid, pid_file=pid_file, port=port, started=started)
    finally:
        if lock_owned:
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
        fp.close()


def probe_legacy_pid_file(pid_file: Path | None = None) -> ServerState:
    """Probe and classify the POSIX legacy compatibility lock.

    A normal exclusive probe establishes whether the path is live. When it
    is, a second non-blocking shared probe distinguishes modern shared alias
    holders from a pre-0.1.25 exclusive holder. The shared handle is released
    immediately. Windows never creates this compatibility lock and callers
    should keep using :func:`probe_pid_file` there.
    """
    target = legacy_server_pid_path() if pid_file is None else pid_file
    state = probe_pid_file(target)
    if not state.alive or state.probe_error is not None:
        return state

    try:
        opened = _open_verified_regular(target, writable=True)
    except (OSError, _UnsafeProbePathError) as exc:
        return replace(
            state,
            pid=None,
            port=None,
            started=None,
            probe_error=f"legacy lock classification failed: {_exception_detail(exc)}",
        )
    if opened is None:
        return replace(
            state,
            pid=None,
            port=None,
            started=None,
            probe_error="legacy lock classification failed: pid path disappeared",
        )

    fd, path_stat, parent_stat = opened
    try:
        fp = os.fdopen(fd, "rb+", buffering=0)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.close(fd)
        return replace(
            state,
            pid=None,
            port=None,
            started=None,
            probe_error=f"legacy lock classification failed: {_exception_detail(exc)}",
        )

    lock_owned = False
    try:
        contended = False
        try:
            # portalocker's POSIX backend uses ``flock``
            # (open-file-description scoped), so this second descriptor cannot
            # release the holder's lock when it closes.
            portalocker.lock(fp, portalocker.LOCK_SH | portalocker.LOCK_NB)
            lock_owned = True
        except (portalocker.AlreadyLocked, BlockingIOError):
            contended = True
        except (portalocker.LockException, OSError) as exc:
            return replace(
                state,
                probe_error=f"legacy lock classification failed: {_exception_detail(exc)}",
            )
        try:
            _verify_opened_regular(fp.fileno(), target, path_stat, parent_stat)
        except _UnsafeProbePathError as exc:
            return replace(
                state,
                pid=None,
                port=None,
                started=None,
                probe_error=f"legacy lock classification failed: {_exception_detail(exc)}",
            )
        if contended:
            return replace(state, legacy_lock_mode="exclusive")
        portalocker.unlock(fp)
        lock_owned = False
        # If the holder exited between the exclusive and shared probes this
        # can conservatively look like a shared alias. It is never signaled,
        # and the next complete inventory pass resolves the race.
        return replace(state, legacy_lock_mode="shared")
    finally:
        if lock_owned:
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
        fp.close()


def _probe_legacy_gate() -> ServerState:
    """Probe the legacy path as a liveness *gate* (#2003).

    A shared holder is the compatibility alias taken by 0.1.26-through-
    pre-#2003 servers (current servers take none), not an independent
    server: such a server is gated by its own ``server[-<digest>].pid``,
    which :func:`check_server_liveness` probes under both runtime-dir
    branches first, so the alias must not additionally block work on an
    unrelated store sharing the same HOME. Exclusive holders (pre-0.1.25)
    and classification failures (``probe_error``) still gate fail-closed.
    Windows never had the compatibility lock, so a plain exclusive probe is
    kept there — a held lock still fails closed.
    """
    if os.name == "nt":
        return probe_pid_file(legacy_server_pid_path())
    state = probe_legacy_pid_file()
    if state.alive and state.legacy_lock_mode == "shared" and state.probe_error is None:
        return ServerState(alive=False, pid=None, pid_file=None)
    return state


def _validated_runtime_dirs() -> tuple[list[Path] | None, str]:
    """Resolve existing runtime candidates under the writer policy.

    A missing directory is an empty candidate. The caller's resolved
    :func:`runtime_dir` fails closed when unsafe: it is the location a server
    in this environment could have used before its policy changed underneath
    it. Other candidates are speculative contexts. If one fails the writer
    policy, no server could currently resolve to it, so skip it rather than
    let an untrusted sibling candidate deny every liveness inventory (#2039).
    """
    try:
        current_runtime = runtime_dir()
        candidates = candidate_runtime_dirs()
    except OSError as exc:
        return None, f"runtime dir candidates unresolved: {_exception_detail(exc)}"

    validated: list[Path] = []
    for candidate in candidates:
        try:
            if validate_runtime_dir(candidate):
                validated.append(candidate)
        except OSError as exc:
            if candidate == current_runtime:
                return None, (
                    f"runtime dir candidate {scrub_text(str(candidate))}: {_exception_detail(exc)}"
                )
    return validated, ", ".join(scrub_text(str(path)) for path in validated)


def _runtime_pid_candidates(name: str) -> tuple[list[Path] | None, str]:
    """Return ``name`` under every runtime dir a live server could have picked.

    A server started in a different context may have taken the other
    :func:`runtime_dir` branch — ``$XDG_RUNTIME_DIR`` set for the server
    (systemd user session) and unset for the CLI, or the reverse — so
    probing only the caller's own branch reports "dead" for a server that
    is alive (#2003 review).

    Returns ``(paths, detail)`` with ``paths`` ``None`` when the candidate
    set could not be resolved. Callers must fail closed on ``None`` rather
    than fall back to the caller's own path: a probe that could not even
    enumerate where a server might be has no evidence that none exists,
    and silently narrowing the set would let a destructive command through
    on an incomplete pass — the same contract :func:`_glob_server_pid_files`
    keeps (#1949).
    """
    dirs, detail = _validated_runtime_dirs()
    if dirs is None:
        return None, detail
    return [rt / name for rt in dirs], detail


def _glob_server_pid_files() -> tuple[list[Path] | None, str]:
    """Enumerate per-store ``server-*.pid`` files across both runtime dirs.

    Returns ``(files, detail)`` — ``files`` is ``None`` when enumeration
    failed, with ``detail`` describing where/why. Callers must treat
    ``None`` as "could not enumerate" and fail closed (#1949) rather than
    conclude no per-store server exists, and a failure in *any* candidate
    directory fails the whole pass: a directory we cannot search is not a
    directory we can call empty. The detail is captured here so the caller
    never re-resolves the runtime dir — if resolution itself was the
    failure, a second call would raise out of the fail-closed path.

    The scan is explicit rather than using :meth:`Path.glob`: ``Path.glob``
    suppresses traversal errors and would turn an unreadable directory into
    an empty result. A missing candidate directory is legitimately empty;
    every other scan failure keeps the pass fail-closed.
    """
    dirs, detail = _validated_runtime_dirs()
    if dirs is None:
        return None, detail
    found: list[Path] = []
    for rt in dirs:
        try:
            with os.scandir(rt) as entries:
                matches = [
                    rt / entry.name for entry in entries if fnmatch(entry.name, "server-*.pid")
                ]
        except FileNotFoundError:
            continue
        except OSError as exc:
            return None, f"{scrub_text(str(rt))}: {_exception_detail(exc)}"
        found.extend(matches)
    return sorted(found), detail


def enumerate_server_liveness() -> list[ServerState]:
    """Return every live or unverifiable server pid-lock state.

    Candidates are deterministic: sorted per-store ``server-*.pid`` files,
    then the transitional bare ``server.pid`` and the legacy
    ``~/.memtomem/.server.pid`` path. The returned states describe lock files,
    not necessarily unique processes: modern POSIX servers can also share the
    legacy compatibility lock. An empty list means a complete pass found no
    live holder.

    An enumeration failure is represented by a fail-closed ``ServerState``
    with ``probe_error`` set, preserving the liveness probe contract from
    #1949. Callers must refuse rather than treat that state as stoppable.
    """
    states: list[ServerState] = []
    globbed, detail = _glob_server_pid_files()
    if globbed is None:
        states.append(
            ServerState(
                alive=True,
                pid=None,
                pid_file=None,
                probe_error=f"could not enumerate server-*.pid ({detail})",
            )
        )
        candidates: list[Path] = []
    else:
        bare, bare_detail = _runtime_pid_candidates("server.pid")
        if bare is None:
            states.append(
                ServerState(
                    alive=True,
                    pid=None,
                    pid_file=None,
                    probe_error=f"could not resolve server.pid candidates ({bare_detail})",
                )
            )
            bare = []
        candidates = [*globbed, *bare]

    for pid_file in candidates:
        state = probe_pid_file(pid_file)
        if state.alive:
            states.append(state)

    legacy_state = (
        probe_pid_file(legacy_server_pid_path()) if os.name == "nt" else probe_legacy_pid_file()
    )
    if legacy_state.alive:
        states.append(legacy_state)
    return states


def check_server_liveness(db_path: Path | None = None) -> ServerState:
    """Probe the server pid files at per-store, transitional and legacy locations.

    With *db_path*, only pid files that can gate work on **that store** are
    probed (#1990): the store-scoped ``server-<digest>.pid`` first (so the
    reported pid is this store's server), then the transitional bare
    ``server.pid`` (a server started by an older version can't be
    attributed to a store — refuse fail-closed), then the legacy
    ``~/.memtomem/.server.pid``. A live server on a *different* store no
    longer reports alive here.

    The legacy path gates only on an *exclusive* holder (a genuine
    pre-0.1.25 server) or an unclassifiable probe (#2003): a shared holder
    is a modern server's compatibility alias, and that server is already
    gated by its own runtime pid file above.

    Without *db_path* — or when no digest can be derived for it
    (``:memory:``, normalization failure) — sorted ``server-*.pid``
    candidates are considered before the transitional and legacy names, so
    store-agnostic compatibility callers see per-store servers too. An
    enumeration failure fails closed with ``probe_error`` set (#1949).

    First live holder wins; if nothing is held the state is dead.
    """
    digest = store_pid_digest(db_path) if db_path is not None else None
    if digest is not None:
        scoped, scoped_detail = _runtime_pid_candidates(server_pid_path(db_path).name)
        bare, bare_detail = _runtime_pid_candidates("server.pid")
        if scoped is None or bare is None:
            return ServerState(
                alive=True,
                pid=None,
                pid_file=None,
                probe_error=f"could not resolve pid candidates ({scoped_detail or bare_detail})",
            )
        for pid_file in (*scoped, *bare):
            state = probe_pid_file(pid_file)
            if state.alive:
                return state
        state = _probe_legacy_gate()
        if state.alive:
            return state
        return ServerState(alive=False, pid=None, pid_file=None)

    globbed, detail = _glob_server_pid_files()
    if globbed is None:
        return ServerState(
            alive=True,
            pid=None,
            pid_file=None,
            probe_error=f"could not enumerate server-*.pid ({detail})",
        )
    bare, bare_detail = _runtime_pid_candidates("server.pid")
    if bare is None:
        return ServerState(
            alive=True,
            pid=None,
            pid_file=None,
            probe_error=f"could not resolve server.pid candidates ({bare_detail})",
        )
    for pid_file in (*globbed, *bare):
        state = probe_pid_file(pid_file)
        if state.alive:
            return state
    state = _probe_legacy_gate()
    if state.alive:
        return state
    return ServerState(alive=False, pid=None, pid_file=None)


def check_web_liveness() -> ServerState:
    """Probe ``mm web``'s pid file (``web.pid``).

    Same portalocker contract as the server probe: ``web._web_pid_lock``
    holds ``LOCK_EX`` on the file for the UI process lifetime, and
    ``_parse_pid_payload`` already understands its pid/port/started
    payload. Kept separate from :func:`check_server_liveness` so callers
    that only care about the MCP server (and their tests) are unaffected
    (#1569).
    """
    pid_file = web_pid_path()
    try:
        runtime_present = validate_runtime_dir(pid_file.parent)
    except OSError as exc:
        return ServerState(
            alive=True,
            pid=None,
            pid_file=pid_file,
            probe_error=f"runtime directory validation failed: {_exception_detail(exc)}",
        )
    if not runtime_present:
        return ServerState(alive=False, pid=None, pid_file=None)
    return probe_pid_file(pid_file)
