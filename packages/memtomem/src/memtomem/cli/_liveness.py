"""Server liveness probe shared by ``mm uninstall`` and ``mm upgrade``.

Both commands need to know whether a ``memtomem-server`` process is currently
holding the pid lock file. The probe uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` —
if we can acquire it, no live writer is holding the file (it's a stale
leftover or fresh and unowned). If we cannot, a writer is alive, regardless
of whether the recorded PID is still valid or has been recycled.

On Windows ``fcntl`` is unavailable; the probe falls back to conservative
"pid file exists → assume alive" so callers can decide how to treat the
ambiguity (uninstall: refuse without ``--force``; upgrade: skip kill stage
and warn).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from memtomem._runtime_paths import legacy_server_pid_path, server_pid_path


@dataclass(frozen=True)
class ServerState:
    alive: bool
    pid: int | None
    pid_file: Path | None


def probe_pid_file(pid_file: Path) -> ServerState:
    """Probe a single pid file via ``fcntl.flock``.

    ``server/__init__.py:main`` opens this file and holds an exclusive
    flock for the entire server lifetime. If we can acquire
    ``LOCK_EX | LOCK_NB`` on it, no live writer is holding it. If we
    cannot, a writer is alive — regardless of whether the recorded PID
    is still valid (kernel may have recycled it; see #387).

    On Windows ``fcntl`` is unavailable; falls back to "pid file exists →
    assume alive". See #448.
    """
    if not pid_file.exists():
        return ServerState(alive=False, pid=None, pid_file=None)

    pid: int | None
    try:
        pid_text = pid_file.read_text().strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        pid = None

    if sys.platform == "win32":
        return ServerState(alive=True, pid=pid, pid_file=pid_file)

    import fcntl

    try:
        fp = open(pid_file, "rb")
    except OSError:
        return ServerState(alive=True, pid=pid, pid_file=pid_file)

    try:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ServerState(alive=True, pid=pid, pid_file=pid_file)
        except OSError:
            return ServerState(alive=True, pid=pid, pid_file=pid_file)
        fcntl.flock(fp, fcntl.LOCK_UN)
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
