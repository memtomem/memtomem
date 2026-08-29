"""Tri-state liveness probe for a recorded process id.

Diagnostics need to distinguish "that process is gone" from "I could not tell",
which a boolean cannot express: an access-denied probe means the process
*exists* and is merely not ours to inspect, while a failed query means we
learned nothing at all. Collapsing either into ``False`` is what turns a live
server into a reported-dead one, so every uncertain answer is ``"unknown"`` and
callers are expected to render it as such rather than pick a side.

This deliberately does not reuse ``cli/web.py:_pid_alive``. That helper opens
the Windows handle with ``SYNCHRONIZE`` alone and then calls
``GetExitCodeProcess``, which is documented to require
``PROCESS_QUERY_INFORMATION`` or ``PROCESS_QUERY_LIMITED_INFORMATION`` — so the
query can fail on a perfectly live process and be read as dead. It also treats a
NULL handle as dead without inspecting ``GetLastError``, folding access-denied
into the same answer. Both are why this module exists rather than a third copy;
migrating ``cli/web.py`` and ``cli/upgrade_cmd.py`` onto it is follow-up work.
"""

from __future__ import annotations

import os
from typing import Literal

__all__ = ["ProbeState", "probe_pid"]

ProbeState = Literal["alive", "dead", "unknown"]

# Win32 constants. PROCESS_QUERY_LIMITED_INFORMATION (Vista+) is the minimum
# right GetExitCodeProcess accepts and, unlike PROCESS_QUERY_INFORMATION, it is
# granted for processes owned by other users without SeDebugPrivilege — which is
# the common case when auditing a machine's accumulated servers.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87
_ERROR_ACCESS_DENIED = 5

# Windows passes the pid as a DWORD, and ctypes *narrows* rather than raising:
# 2**32 + 4242 would arrive as 4242 and probe an unrelated process. Since the
# value comes from a filename whose regex accepts unbounded digits, the bound is
# enforced here rather than relied upon downstream.
_MAX_PID = 0xFFFFFFFF


def probe_pid(pid: int) -> ProbeState:
    """Report whether *pid* is alive, without signalling it.

    ``pid`` comes from a registry filename written by another process, so it is
    untrusted input rather than a value this process computed: it may be zero,
    negative, or far larger than the platform's pid type. Those are rejected
    before any syscall, because each fails differently and none fails safely —
    on POSIX ``os.kill(0, 0)`` signals *the caller's whole process group* and
    would report a stale zero as alive, while on Windows ctypes narrows the pid
    to a ``DWORD`` without complaint, so ``2**32 + N`` would silently probe
    process ``N``.

    On Windows a pid additionally does not identify a process across time: the
    id is released for reuse once the process exits and its last handle closes,
    so an ``"alive"`` here can be an unrelated process that inherited the
    number. Callers presenting this to a user must say what was observed
    ("recorded parent"), not assert a relationship.

    Never raises: any unexpected platform error degrades to ``"unknown"``.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid > _MAX_PID:
        return "unknown"
    if os.name == "nt":
        return _probe_windows(pid)
    return _probe_posix(pid)


def _probe_posix(pid: int) -> ProbeState:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        # Exists, owned by someone else — the strongest evidence of liveness we
        # get, and the reason a bare except would be wrong here.
        return "alive"
    except (OverflowError, ValueError, OSError):
        return "unknown"
    return "alive"


def _probe_windows(pid: int) -> ProbeState:  # pragma: no cover - exercised via patched ctypes
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if code == _ERROR_INVALID_PARAMETER:
                return "dead"
            if code == _ERROR_ACCESS_DENIED:
                return "alive"
            return "unknown"
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return "unknown"
            # STILL_ACTIVE is STATUS_PENDING (259). A process that exits with
            # 259 is indistinguishable from a running one; accepted here because
            # this feeds a report, never a termination decision.
            return "alive" if exit_code.value == _STILL_ACTIVE else "dead"
        finally:
            close_handle(handle)
    except (OSError, ValueError, OverflowError, AttributeError):
        return "unknown"
