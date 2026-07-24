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
    # #1949: set only when ``alive=True`` is a fail-closed *assumption* —
    # the probe could not inspect the lock file at all (``exists()`` or
    # ``open()`` raised) — never when a held flock was actually observed.
    # Callers that print "flock is held by an active writer" must check
    # this first: that claim is evidence-based, and a failed probe has no
    # evidence. ``None`` means the ``alive`` verdict is real.
    probe_error: str | None = None


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
