"""Tests for :mod:`memtomem._process_probe`.

The probe is tri-state because a boolean forces a wrong answer in two real
cases: a process we are not permitted to inspect *exists*, and a failed query
tells us nothing. Both must be distinguishable from "gone", since the caller
renders them to a user as fact.

The Windows branch is exercised on every platform by patching the ctypes shim,
so the discrimination logic is covered on POSIX CI rather than only on the
Windows shard.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from memtomem import _process_probe
from memtomem._process_probe import probe_pid


class TestInputContract:
    """Pids arrive from a filename another process wrote — untrusted input."""

    def test_zero_is_unknown_and_never_probed(self, monkeypatch):
        # POSIX ``os.kill(0, 0)`` signals the caller's whole process group and
        # would answer "alive" for a pid that identifies nothing.
        monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("pid 0 must not reach os.kill"))
        assert probe_pid(0) == "unknown"

    def test_negative_is_unknown_and_never_probed(self, monkeypatch):
        # Negative pids address process groups too.
        monkeypatch.setattr(
            os, "kill", lambda *a: pytest.fail("negative pid must not reach os.kill")
        )
        assert probe_pid(-1) == "unknown"

    def test_oversized_pid_is_unknown_not_a_crash(self):
        # The sentinel filename regex accepts unbounded digits, so a corrupt or
        # hand-edited name can carry a value no pid_t can hold.
        assert probe_pid(2**80) == "unknown"

    def test_pid_above_the_dword_range_is_rejected_before_any_probe(self, monkeypatch):
        """``2**32 + N`` must not become a probe of process ``N``.

        ctypes narrows a Python int to ``DWORD`` silently, so without an
        explicit bound a crafted registry filename could make the doctor report
        on an unrelated process. Asserted on both platforms because the value
        must never reach either backend.
        """
        monkeypatch.setattr(
            os, "kill", lambda *a: pytest.fail("oversized pid must not reach os.kill")
        )
        assert probe_pid(2**32 + 4242) == "unknown"

    @pytest.mark.skipif(sys.platform == "win32", reason="fake shim targets the non-Windows runner")
    def test_oversized_pid_never_reaches_openprocess(self, monkeypatch):
        calls = _install_fake_windows(monkeypatch, handle=7, exit_code=_process_probe._STILL_ACTIVE)
        assert probe_pid(2**32 + 4242) == "unknown"
        assert calls["opened"] == 0, "the bound must be enforced before the Win32 call"
        # Control: a normal pid does reach OpenProcess, so the assertion above
        # is about the bound and not about a fake that is never wired up.
        assert probe_pid(4242) == "alive"
        assert calls["opened"] == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX branch")
class TestPosix:
    def test_self_is_alive(self):
        assert probe_pid(os.getpid()) == "alive"

    def test_permission_error_means_alive(self, monkeypatch):
        def _denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(os, "kill", _denied)
        # Someone else's process: we cannot signal it, which proves it exists.
        assert probe_pid(4242) == "alive"

    def test_process_lookup_error_means_dead(self, monkeypatch):
        def _gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", _gone)
        assert probe_pid(4242) == "dead"

    def test_other_oserror_is_unknown(self, monkeypatch):
        def _boom(pid, sig):
            raise OSError("transient")

        monkeypatch.setattr(os, "kill", _boom)
        assert probe_pid(4242) == "unknown"


def _install_fake_windows(monkeypatch, *, handle, last_error=0, exit_code=None, query_ok=True):
    """Patch the ctypes surface the Windows branch imports lazily."""
    import ctypes

    calls = {"closed": 0, "opened": 0}

    def _open_process(access, inherit, pid):
        assert access == _process_probe._PROCESS_QUERY_LIMITED_INFORMATION, (
            "GetExitCodeProcess requires QUERY(_LIMITED)_INFORMATION, not SYNCHRONIZE"
        )
        calls["opened"] += 1
        return handle

    def _get_exit_code(h, out):
        if not query_ok:
            return 0
        out._obj.value = exit_code
        return 1

    def _close(h):
        calls["closed"] += 1
        return 1

    fake = SimpleNamespace(
        OpenProcess=SimpleNamespace(argtypes=None, restype=None),
        GetExitCodeProcess=SimpleNamespace(argtypes=None, restype=None),
        CloseHandle=SimpleNamespace(argtypes=None, restype=None),
    )
    fake.OpenProcess = _open_process
    fake.GetExitCodeProcess = _get_exit_code
    fake.CloseHandle = _close

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error, raising=False)
    monkeypatch.setattr(os, "name", "nt")
    return calls


@pytest.mark.skipif(sys.platform == "win32", reason="fake shim targets the non-Windows runner")
class TestWindowsBranchViaFakeCtypes:
    """The Windows discrimination logic, exercised on POSIX CI."""

    def test_null_handle_with_invalid_parameter_is_dead(self, monkeypatch):
        _install_fake_windows(
            monkeypatch, handle=0, last_error=_process_probe._ERROR_INVALID_PARAMETER
        )
        assert probe_pid(4242) == "dead"

    def test_null_handle_with_access_denied_is_alive(self, monkeypatch):
        # The process exists; we are simply not allowed to open it. Reading
        # this as "dead" is the bug that motivated a typed probe.
        _install_fake_windows(monkeypatch, handle=0, last_error=_process_probe._ERROR_ACCESS_DENIED)
        assert probe_pid(4242) == "alive"

    def test_null_handle_with_other_error_is_unknown(self, monkeypatch):
        _install_fake_windows(monkeypatch, handle=0, last_error=1234)
        assert probe_pid(4242) == "unknown"

    def test_still_active_is_alive(self, monkeypatch):
        _install_fake_windows(monkeypatch, handle=7, exit_code=_process_probe._STILL_ACTIVE)
        assert probe_pid(4242) == "alive"

    def test_terminated_exit_code_is_dead(self, monkeypatch):
        _install_fake_windows(monkeypatch, handle=7, exit_code=0)
        assert probe_pid(4242) == "dead"

    def test_failed_query_is_unknown_not_dead(self, monkeypatch):
        _install_fake_windows(monkeypatch, handle=7, query_ok=False)
        assert probe_pid(4242) == "unknown"
