"""Tests for ``memtomem._pid_lock.lock_pid_file`` (#2611).

The owner side of a pid-file lock: the MCP server and the Web UI take it
once and keep it for their lifetime. These pin the two gaps the helper
closes, and the boundary it does not cross:

- a holder that releases within the retry budget (a liveness probe) must not
  read as a live owner;
- a lock that lands on a file whose name was deleted or replaced between the
  open and the lock must be re-taken on the file the path now names;
- a holder that outlasts the budget still reads as live (the mitigation is
  bounded, not a probe/owner protocol).

Clock and sleep are faked through the module's ``time`` so no assertion
depends on wall-clock timing. Rival holders use a second open handle in this
process: ``flock`` contends per open file description on POSIX and
``msvcrt.locking`` per handle on Windows (``test_windows_lock_semantics``),
so a same-process rival contends exactly as another process would.
"""

from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import portalocker
import pytest

from memtomem import _pid_lock

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="Windows cannot delete a file with an open handle"
)
_CTX = mp.get_context("spawn")


class _FakeClock:
    """``monotonic``/``sleep`` pair: each sleep advances the clock exactly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep: list = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        for hook in self.on_sleep:
            hook()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(_pid_lock.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(_pid_lock.time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every handle the helper opens, so a test can check it was closed."""
    handles: list = []
    real_open = open

    def spy(*args, **kwargs):
        fp = real_open(*args, **kwargs)
        handles.append(fp)
        return fp

    monkeypatch.setattr(_pid_lock, "open", spy, raising=False)
    return handles


class _Rival:
    """A second handle holding ``LOCK_EX`` on the pid file."""

    def __init__(self, path: Path) -> None:
        self.fp = open(path, "rb+")
        portalocker.lock(self.fp, portalocker.LOCK_EX | portalocker.LOCK_NB)

    def release(self) -> None:
        if not self.fp.closed:
            portalocker.unlock(self.fp)
            self.fp.close()


def _close(fp) -> None:
    portalocker.unlock(fp)
    fp.close()


def test_uncontended_lock_returns_handle_and_keeps_content(tmp_path: Path, clock) -> None:
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    fp = _pid_lock.lock_pid_file(pid_file, label="server pid")

    assert fp is not None
    try:
        fp.seek(0)
        assert fp.read() == "12345\n", "the helper must not truncate; the caller does"
        assert clock.sleeps == []
    finally:
        _close(fp)


def test_missing_file_is_created(tmp_path: Path, clock) -> None:
    pid_file = tmp_path / "web.pid"

    fp = _pid_lock.lock_pid_file(pid_file, label="web pid")

    assert fp is not None
    try:
        assert pid_file.exists()
    finally:
        _close(fp)


def test_probe_holding_the_lock_across_the_first_attempt(tmp_path: Path, clock) -> None:
    """The issue's case: a liveness probe holds the lock at the owner's first
    attempt and releases it a moment later. The owner must get the lock."""
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("", encoding="utf-8")
    probe = _Rival(pid_file)
    clock.on_sleep.append(probe.release)
    try:
        fp = _pid_lock.lock_pid_file(pid_file, label="server pid")
    finally:
        probe.release()

    assert fp is not None, "a probe's brief hold must not read as a live owner"
    try:
        assert len(clock.sleeps) == 1
    finally:
        _close(fp)


def test_transient_contention_is_retried(
    tmp_path: Path, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "server.pid"
    real_lock = portalocker.lock
    calls = {"n": 0}

    def lock_contended_twice(fp, flags):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise portalocker.AlreadyLocked("held by a probe")
        return real_lock(fp, flags)

    monkeypatch.setattr(_pid_lock.portalocker, "lock", lock_contended_twice)

    fp = _pid_lock.lock_pid_file(pid_file, label="server pid")

    assert fp is not None
    try:
        assert len(clock.sleeps) == 2
        assert all(s == _pid_lock._PID_LOCK_POLL_S for s in clock.sleeps)
    finally:
        _close(fp)


def test_holder_longer_than_the_budget_reads_as_live(tmp_path: Path, clock, opened) -> None:
    """The mitigation is bounded: a holder that keeps the lock for the whole
    budget (a live owner, or a probe suspended that long) reads as live."""
    pid_file = tmp_path / "server.pid"
    pid_file.write_bytes(b"777\n")  # bytes: write_text would write CRLF on Windows
    owner = _Rival(pid_file)
    try:
        result = _pid_lock.lock_pid_file(pid_file, label="server pid")

        assert result is None
        expected = round(_pid_lock._PID_LOCK_RETRY_S / _pid_lock._PID_LOCK_POLL_S)
        # +1 absorbs float accumulation of the fake clock at the deadline.
        assert expected <= len(clock.sleeps) <= expected + 1
        assert all(fp.closed for fp in opened)
        owner.fp.seek(0)
        assert owner.fp.read() == b"777\n", "a contended attempt must not touch the file"
    finally:
        owner.release()


def test_explicit_zero_timeout_does_not_wait(tmp_path: Path, clock) -> None:
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("", encoding="utf-8")
    owner = _Rival(pid_file)
    try:
        assert _pid_lock.lock_pid_file(pid_file, label="web pid", timeout_s=0) is None
        assert clock.sleeps == []
    finally:
        owner.release()


def _hold_until_released(pid_file_str: str, release, q) -> None:
    fp = open(pid_file_str, "a+")
    portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
    q.put("locked")
    release.wait(30)
    portalocker.unlock(fp)
    fp.close()


def test_another_process_holding_the_lock_reads_as_live(tmp_path: Path) -> None:
    """Cross-process, real clock: a holder in another process is a live owner
    until it lets go, and the lock is free once it does."""
    pid_file = tmp_path / "server.pid"
    release = _CTX.Event()
    q = _CTX.Queue()
    child = _CTX.Process(target=_hold_until_released, args=(str(pid_file), release, q))
    child.start()
    try:
        assert q.get(timeout=30) == "locked"
        assert _pid_lock.lock_pid_file(pid_file, label="server pid", timeout_s=0.2) is None
    finally:
        release.set()
        child.join(30)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 0

    fp = _pid_lock.lock_pid_file(pid_file, label="server pid", timeout_s=0.2)
    assert fp is not None
    _close(fp)


def test_lock_call_io_failure_raises_oserror(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_lock(fp, flags):
        raise portalocker.LockException("backend failure")

    monkeypatch.setattr(_pid_lock.portalocker, "lock", broken_lock)

    with pytest.raises(OSError, match="web pid lock failed"):
        _pid_lock.lock_pid_file(tmp_path / "web.pid", label="web pid")
    assert clock.sleeps == [], "an I/O failure is not contention; nothing to wait for"
    assert opened and all(fp.closed for fp in opened)


class _RawWin32Error(Exception):
    """Shape of a raw ``pywintypes.error``: not an ``OSError``, no ``errno``."""

    winerror = 5  # ERROR_ACCESS_DENIED, not a lock violation


def test_raw_non_oserror_lock_failure_is_normalized(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """portalocker 3.x re-raises a non-lock-violation ``pywintypes.error`` raw
    (``_lock_errors.LOCK_CALL_ERRORS_WIDE``). It must still come out as
    ``OSError`` with the handle closed, not escape unclassified."""
    monkeypatch.setattr(
        _pid_lock, "LOCK_CALL_ERRORS_WIDE", (*_pid_lock.LOCK_CALL_ERRORS_WIDE, _RawWin32Error)
    )

    def raw_failure(fp, flags):
        raise _RawWin32Error("access denied")

    monkeypatch.setattr(_pid_lock.portalocker, "lock", raw_failure)

    with pytest.raises(OSError, match="server pid lock failed"):
        _pid_lock.lock_pid_file(tmp_path / "server.pid", label="server pid")
    assert opened and all(fp.closed for fp in opened)


@_POSIX_ONLY
def test_post_lock_stat_failure_releases_the_lock(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "server.pid"
    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if Path(path) == pid_file:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(_pid_lock.os, "stat", failing_stat)

    with pytest.raises(PermissionError):
        _pid_lock.lock_pid_file(pid_file, label="server pid")
    monkeypatch.setattr(_pid_lock.os, "stat", real_stat)

    assert opened and all(fp.closed for fp in opened)
    rival = _Rival(pid_file)  # would raise AlreadyLocked had the lock leaked
    rival.release()


class _Interrupt(BaseException):
    """Stands in for an asynchronous ``KeyboardInterrupt``."""


@_POSIX_ONLY
def test_interrupt_while_releasing_still_closes_the_handle(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-lock failure releases the lock and closes the handle; an
    interruption during that unlock must not skip the close."""
    pid_file = tmp_path / "server.pid"
    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if Path(path) == pid_file:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    def interrupted_unlock(fp):
        raise _Interrupt

    monkeypatch.setattr(_pid_lock.os, "stat", failing_stat)
    monkeypatch.setattr(_pid_lock.portalocker, "unlock", interrupted_unlock)

    with pytest.raises(_Interrupt):
        _pid_lock.lock_pid_file(pid_file, label="server pid")

    assert opened and all(fp.closed for fp in opened)


@_POSIX_ONLY
@pytest.mark.parametrize("dangling", [False, True], ids=["to-a-file", "dangling"])
def test_symlinked_pid_file_is_refused(tmp_path: Path, clock, opened, dangling: bool) -> None:
    """The caller truncates the handle it gets back; following a symlink
    would truncate (or, dangling, create) the link's target."""
    target = tmp_path / "elsewhere.txt"
    if not dangling:
        target.write_text("keep me\n", encoding="utf-8")
    pid_file = tmp_path / "server.pid"
    pid_file.symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        _pid_lock.lock_pid_file(pid_file, label="server pid")

    if dangling:
        assert not target.exists()
    else:
        assert target.read_text(encoding="utf-8") == "keep me\n"
    assert pid_file.is_symlink()


@_POSIX_ONLY
def test_path_turned_into_a_symlink_after_the_open_is_refused(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the open and the lock the name is swapped for a symlink to the
    very file that was opened. ``stat`` would call that the same file; the
    identity check must not, and the re-open then refuses the link."""
    pid_file = tmp_path / "web.pid"
    moved = tmp_path / "moved.pid"
    real_lock = portalocker.lock
    calls = {"n": 0}

    def lock_after_symlink_swap(fp, flags):
        calls["n"] += 1
        if calls["n"] == 1:
            os.replace(pid_file, moved)
            pid_file.symlink_to(moved)
        return real_lock(fp, flags)

    monkeypatch.setattr(_pid_lock.portalocker, "lock", lock_after_symlink_swap)

    with pytest.raises(OSError, match="symbolic link"):
        _pid_lock.lock_pid_file(pid_file, label="web pid")
    assert all(fp.closed for fp in opened)


def _swap_path(pid_file: Path, how: str) -> None:
    if how == "deleted":
        os.unlink(pid_file)
    else:
        fresh = pid_file.with_name("fresh.tmp")
        fresh.write_text("", encoding="utf-8")
        os.replace(fresh, pid_file)


@_POSIX_ONLY
@pytest.mark.parametrize("how", ["deleted", "replaced"])
def test_file_swapped_between_open_and_lock_is_reopened(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A remover deletes the pid file under its own lock (#2595) after the
    owner opened it but before the owner locked it. The owner's lock then
    lands on a file no path names; it must re-take the lock on the file the
    path names now."""
    pid_file = tmp_path / "web.pid"
    real_lock = portalocker.lock
    calls = {"n": 0}

    def lock_after_swap(fp, flags):
        calls["n"] += 1
        if calls["n"] == 1:
            _swap_path(pid_file, how)
        return real_lock(fp, flags)

    monkeypatch.setattr(_pid_lock.portalocker, "lock", lock_after_swap)

    fp = _pid_lock.lock_pid_file(pid_file, label="web pid")

    assert fp is not None
    try:
        assert pid_file.exists(), "the owner must end up holding a named file"
        held = os.fstat(fp.fileno())
        named = os.stat(pid_file)
        assert (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
        assert len(opened) == 2
        assert opened[0].closed
    finally:
        _close(fp)


@_POSIX_ONLY
def test_endless_replacement_raises_oserror(
    tmp_path: Path, clock, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "server.pid"
    real_lock = portalocker.lock

    def always_deleted(fp, flags):
        os.unlink(pid_file)
        return real_lock(fp, flags)

    monkeypatch.setattr(_pid_lock.portalocker, "lock", always_deleted)

    with pytest.raises(OSError, match="replaced") as info:
        _pid_lock.lock_pid_file(pid_file, label="server pid")

    assert str(pid_file) in str(info.value)
    assert len(opened) == _pid_lock._PID_LOCK_REOPENS + 1
    assert all(fp.closed for fp in opened)


@pytest.mark.skipif(os.name != "nt", reason="records Windows stat identity for a follow-up")
def test_fstat_and_stat_agree_on_identity_on_windows(tmp_path: Path) -> None:
    """The identity check is skipped on Windows. This records whether
    ``os.fstat`` and ``os.stat`` agree there, which decides whether it could
    be enabled."""
    pid_file = tmp_path / "server.pid"
    with open(pid_file, "a+") as fp:
        held = os.fstat(fp.fileno())
        named = os.stat(pid_file)
    assert (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
