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

import os
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

import portalocker

from memtomem._runtime_paths import (
    candidate_runtime_dirs,
    legacy_server_pid_path,
    server_pid_path,
    store_pid_digest,
    web_pid_path,
)


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
        present = pid_file.exists()
    except OSError as exc:
        # #1949: on py3.12 ``Path.exists()`` propagates errors outside its
        # ignore-set (e.g. ``EACCES`` for a pid file linked through an
        # unsearchable directory). Fail *closed* — same as the ``open()``
        # failure below: "cannot inspect the lock file" is not "no writer."
        # ``probe_error`` records that ``alive`` is an assumption so callers
        # refuse honestly instead of claiming a held flock. A dangling pid
        # link stays ``alive=False`` (ENOENT is in the ignore-set).
        return ServerState(
            alive=True,
            pid=None,
            pid_file=pid_file,
            probe_error=f"{type(exc).__name__}: {exc}",
        )
    if not present:
        return ServerState(alive=False, pid=None, pid_file=None)

    pid: int | None
    port: int | None
    started: str | None
    try:
        pid, port, started = _parse_pid_payload(pid_file.read_text())
    except OSError:
        pid, port, started = None, None, None

    # ``"rb+"`` (read-write) not ``"rb"``: portalocker's default Windows
    # backend (``MsvcrtLocker``) calls ``msvcrt.locking``, which the C
    # runtime requires to be opened for writing — read-only handles fail
    # with ``EACCES`` and look indistinguishable from a real holder.
    # POSIX ``flock`` doesn't care about access mode, but the file is
    # already user-owned, so always opening R/W keeps both backends happy.
    try:
        fp = open(pid_file, "rb+")
    except OSError as exc:
        # Cannot open the lock file to probe it — fail closed as before,
        # but record why so uninstall can refuse honestly rather than
        # assert an observed flock (#1949). ``pid``/``port``/``started``
        # (if the earlier read_text succeeded) are still forwarded.
        return ServerState(
            alive=True,
            pid=pid,
            pid_file=pid_file,
            port=port,
            started=started,
            probe_error=f"{type(exc).__name__}: {exc}",
        )

    try:
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.AlreadyLocked, BlockingIOError):
            # Genuine contention: another handle holds the lock. Every
            # portalocker 3.x backend maps a *lock-failed* Win32/POSIX error
            # to ``AlreadyLocked`` (a ``LockException`` subclass) — POSIX
            # EACCES/EAGAIN, Windows ``LOCK_FAILED`` — so this is observed
            # evidence, not an assumption. ``probe_error`` stays None.
            # ``BlockingIOError`` is kept as a defensive raw-``flock`` signal.
            return ServerState(
                alive=True,
                pid=pid,
                pid_file=pid_file,
                port=port,
                started=started,
            )
        except (portalocker.LockException, OSError) as exc:
            # A *non-contention* lock failure (I/O error, ENOLCK, NFS
            # EOFError, a Windows error outside the lock-failed set) — the
            # probe could not decide. Fail closed as before, but record why
            # so uninstall refuses honestly instead of asserting a held
            # flock it never observed (#1949). Portalocker wraps these as a
            # bare ``LockException``, distinct from ``AlreadyLocked`` above.
            return ServerState(
                alive=True,
                pid=pid,
                pid_file=pid_file,
                port=port,
                started=started,
                probe_error=f"{type(exc).__name__}: {exc}",
            )
        portalocker.unlock(fp)
        return ServerState(alive=False, pid=pid, pid_file=pid_file, port=port, started=started)
    finally:
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
        # portalocker's POSIX backend uses ``flock`` (open-file-description
        # scoped), so opening a second fd and closing it cannot release the
        # holder's lock. This classification would not be safe with classic
        # process-scoped ``fcntl`` record locks.
        fp = open(target, "rb+")
    except OSError as exc:
        return replace(state, probe_error=f"legacy lock classification failed: {exc}")

    try:
        try:
            portalocker.lock(fp, portalocker.LOCK_SH | portalocker.LOCK_NB)
        except (portalocker.AlreadyLocked, BlockingIOError):
            return replace(state, legacy_lock_mode="exclusive")
        except (portalocker.LockException, OSError) as exc:
            return replace(state, probe_error=f"legacy lock classification failed: {exc}")
        portalocker.unlock(fp)
        # If the holder exited between the exclusive and shared probes this
        # can conservatively look like a shared alias. It is never signaled,
        # and the next complete inventory pass resolves the race.
        return replace(state, legacy_lock_mode="shared")
    finally:
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
    try:
        return [rt / name for rt in candidate_runtime_dirs()], ""
    except OSError as exc:
        return None, f"runtime dir candidates unresolved: {exc}"


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
    try:
        dirs = candidate_runtime_dirs()
    except OSError as exc:
        return None, f"runtime dir unresolved: {exc}"
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
            return None, f"{rt}: {exc}"
        found.extend(matches)
    return sorted(found), ", ".join(str(rt) for rt in dirs)


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
    return probe_pid_file(web_pid_path())
