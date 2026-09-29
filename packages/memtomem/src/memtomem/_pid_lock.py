"""Take the exclusive lock a pid-file owner holds for its lifetime (#2611).

The MCP server (``server/__init__.py:main``) and the Web UI
(``cli/web.py:_web_pid_lock``) each own a pid file: they open it, take
``LOCK_EX | LOCK_NB``, write their pid, and keep the lock until they exit.
A non-blocking attempt that fails once used to be final, and two things
made that wrong:

- **Contention is not always an owner.** Every liveness probe
  (``cli/_liveness.py:probe_pid_file``: ``mm status``, ``mm web status``,
  ``mm upgrade``, ``mm reset``, ``mm uninstall``, ``mm web stop``'s release
  poll) and the stale-file remover (``unlink_stale_pid_file``, #2595) take
  the same lock for a moment. A server that started in that moment read it
  as a second server and ran without a pid lock for its whole life. This
  module retries contention for a short budget first. It is a bounded
  mitigation, not a way to tell a probe from an owner: a probe that is
  suspended (``SIGSTOP``, a debugger) for longer than the budget still
  reads as a live owner.
- **The locked file can have lost its name.** The stale-file remover
  deletes the path while it holds the lock. If it does so between the
  owner's ``open`` and ``lock``, the owner's lock then succeeds on a file no
  path names, and every probe that opens the path reports "not running".
  After locking, this module checks that the path still names the locked
  file and re-opens when it does not.

The open mode is ``"a+"``, never ``"w"``: truncating before the lock would
zero a live owner's pid file, and the Windows ``msvcrt.locking`` backend
needs a writable handle. The caller truncates and writes only after this
returns a handle.

Leaf module: it imports only :mod:`memtomem._lock_errors`, so the server's
startup path does not import the CLI package.
"""

from __future__ import annotations

import contextlib
import errno
import os
import time
from pathlib import Path
from typing import TextIO

import portalocker

from memtomem._lock_errors import (
    LOCK_CALL_ERRORS_WIDE,
    is_lock_contention,
    raise_lock_io_failure,
)

# Budget for waiting out a transient holder. A probe holds the lock for an
# open, a bounded read and an fstat; ``mm web stop`` polls every 0.1 s. Ten
# polls cover a burst of those without making a genuinely contended start
# (the slow path already: the server warns, the Web UI refuses) much slower.
_PID_LOCK_RETRY_S = 0.5
# Same cadence as ``_instance_registry._acquire_barrier_file``.
_PID_LOCK_POLL_S = 0.05
# A remover deletes a pid file at most once per stale file; a path that keeps
# changing under us is a path problem, not a holder.
_PID_LOCK_REOPENS = 5


def lock_pid_file(pid_file: Path, *, label: str, timeout_s: float | None = None) -> TextIO | None:
    """Open *pid_file* and take its exclusive lock, or return ``None`` if held.

    Returns the locked handle, opened ``"a+"`` with its content untouched.
    The caller writes the payload and owns closing it. Returns ``None`` when
    another process still holds the lock after ``timeout_s`` (default
    :data:`_PID_LOCK_RETRY_S`) of retries.

    A symlinked pid path is refused (``O_NOFOLLOW``), as the liveness probes
    refuse it: following it would let the caller truncate the link's target.

    On POSIX the returned handle is the file the path names at return time:
    if the path was deleted or replaced between the open and the lock, the
    handle is closed and the path re-opened. Windows skips that check: a file
    with an open handle cannot be deleted there, so the lock cannot land on a
    nameless file.

    Raises ``OSError`` when the open fails, when the lock call fails for a
    reason other than contention (normalized by
    :func:`~memtomem._lock_errors.raise_lock_io_failure`, with *label* in the
    message), when the path is a symlink, or when the path is replaced more than
    :data:`_PID_LOCK_REOPENS` times. Never reports those as a live holder.
    """
    budget = _PID_LOCK_RETRY_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + budget
    reopens = 0
    while True:
        try:
            # Returned to the caller, who owns it.
            fp = open(pid_file, "a+", opener=_open_no_follow)  # noqa: SIM115
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise OSError(
                    errno.ELOOP,
                    f"{label} pid file is a symbolic link; refusing to follow it",
                    str(pid_file),
                ) from exc
            raise
        locked = False
        try:
            while True:
                try:
                    portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    locked = True
                    break
                except LOCK_CALL_ERRORS_WIDE as exc:
                    if not is_lock_contention(exc):
                        raise_lock_io_failure(exc, pid_file, label=label)
                    if time.monotonic() >= deadline:
                        fp.close()
                        return None
                    time.sleep(_PID_LOCK_POLL_S)
            if _path_names_locked_file(fp, pid_file):
                return fp
        except BaseException:
            _release(fp, locked=locked)
            raise
        _release(fp, locked=locked)
        reopens += 1
        if reopens > _PID_LOCK_REOPENS:
            raise OSError(
                f"{label} pid file {pid_file} was replaced {reopens} times while locking it"
            )


# ``O_NOFOLLOW`` does not exist on Windows; there the flag is 0.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_no_follow(path: str, flags: int) -> int:
    """``open`` opener: the flags ``"a+"`` computes, plus ``O_NOFOLLOW``."""
    return os.open(path, flags | _O_NOFOLLOW, 0o666)


def _path_names_locked_file(fp: TextIO, pid_file: Path) -> bool:
    """Whether *pid_file* still names the file *fp* has locked (POSIX).

    ``lstat``, not ``stat``: the handle was opened without following links,
    so a name that has since become a symlink does not name the locked file.
    """
    if os.name == "nt":
        return True
    held = os.fstat(fp.fileno())
    try:
        current = os.stat(pid_file, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _release(fp: TextIO, *, locked: bool) -> None:
    try:
        if locked:
            with contextlib.suppress(Exception):
                portalocker.unlock(fp)
    finally:
        with contextlib.suppress(OSError):
            fp.close()
