"""Server liveness probe shared by ``mm uninstall`` and ``mm upgrade``.

Both commands need to know whether a ``memtomem-server`` process is currently
holding the pid lock file. The probe uses ``portalocker.lock(LOCK_EX | LOCK_NB)``
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

from memtomem._runtime_paths import legacy_server_pid_path, server_pid_path


@dataclass(frozen=True)
class ServerState:
    alive: bool
    pid: int | None
    pid_file: Path | None


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
    try:
        pid_text = pid_file.read_text().strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        pid = None

    try:
        fp = open(pid_file, "rb")
    except OSError:
        return ServerState(alive=True, pid=pid, pid_file=pid_file)

    try:
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, BlockingIOError, OSError):
            # POSIX raises BlockingIOError; portalocker's Windows backend
            # wraps Win32 errors as LockException. Treat any of them as
            # "another holder, server is alive."
            return ServerState(alive=True, pid=pid, pid_file=pid_file)
        portalocker.unlock(fp)
        return ServerState(alive=False, pid=pid, pid_file=pid_file)
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
