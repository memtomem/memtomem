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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import portalocker

from memtomem._runtime_paths import legacy_server_pid_path, server_pid_path, web_pid_path


@dataclass(frozen=True)
class ServerState:
    alive: bool
    pid: int | None
    pid_file: Path | None
    port: int | None = None
    started: str | None = None


def _parse_pid_payload(text: str) -> tuple[int | None, int | None, str | None]:
    """Parse pid-file payloads.

    Legacy server pid files are a single ``pid`` line. ``mm web`` writes
    ``pid`` / ``port`` / ``started`` on separate lines. The first line stays
    the pid so older call sites that only care about the process id remain
    compatible.
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
    if not pid_file.exists():
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
    except OSError:
        return ServerState(alive=True, pid=pid, pid_file=pid_file, port=port, started=started)

    try:
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, BlockingIOError, OSError):
            # POSIX raises BlockingIOError; portalocker's Windows backend
            # wraps Win32 errors as LockException. Treat any of them as
            # "another holder, server is alive."
            return ServerState(
                alive=True,
                pid=pid,
                pid_file=pid_file,
                port=port,
                started=started,
            )
        portalocker.unlock(fp)
        return ServerState(alive=False, pid=pid, pid_file=pid_file, port=port, started=started)
    finally:
        fp.close()


def check_server_liveness() -> ServerState:
    """Probe the server pid file at both new (#412) and legacy locations.

    First live holder wins; if neither is held the state is dead.
    """
    for pid_file in (server_pid_path(), legacy_server_pid_path()):
        state = probe_pid_file(pid_file)
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
