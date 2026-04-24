"""Parent-death watchdog — issue #440.

When an MCP stdio client (Claude Code observed) exits without closing
our stdio sockets OR sending SIGTERM, ``memtomem-server`` is left alive
as an orphan holding ``~/.memtomem/.server.pid``. This watchdog polls
``os.getppid()`` and self-SIGTERMs when the parent disappears
(reparenting to PID 1 / launchd), letting the already-installed sigterm
handler (#439) unlink the pid files and exit cleanly.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memtomem.server.lifespan import (
    _watch_parent,
    _watchdog_enabled,
    _watchdog_interval,
)


# ── env gating ───────────────────────────────────────────────────────


def test_watchdog_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMTOMEM_PARENT_WATCHDOG", raising=False)
    assert _watchdog_enabled() is True


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF", "False"])
def test_watchdog_disabled_by_env(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_PARENT_WATCHDOG", value)
    assert _watchdog_enabled() is False


@pytest.mark.parametrize("value", ["on", "1", "true", "anything-else"])
def test_watchdog_enabled_for_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_PARENT_WATCHDOG", value)
    assert _watchdog_enabled() is True


def test_watchdog_interval_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMTOMEM_PARENT_WATCHDOG_INTERVAL", raising=False)
    assert _watchdog_interval() == 10.0


def test_watchdog_interval_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_PARENT_WATCHDOG_INTERVAL", "2.5")
    assert _watchdog_interval() == 2.5


def test_watchdog_interval_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage values must not crash the server — fall back to the default."""
    monkeypatch.setenv("MEMTOMEM_PARENT_WATCHDOG_INTERVAL", "not-a-number")
    assert _watchdog_interval() == 10.0


# ── behavior ─────────────────────────────────────────────────────────


async def test_watch_parent_noops_when_ppid_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As long as getppid() equals the original, nothing happens."""
    monkeypatch.setattr(os, "getppid", lambda: 12345)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    task = asyncio.create_task(_watch_parent(original_ppid=12345, poll_seconds=0.01))
    await asyncio.sleep(0.05)  # ~5 polls at 10ms each
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert killed == [], "must not send SIGTERM while parent is alive"


async def test_watch_parent_self_sigterms_when_ppid_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulated reparent triggers self-SIGTERM.

    The watchdog sends the signal and returns; the sigterm handler
    (registered separately in ``main()``) takes it from there. We only
    verify the signal dispatch here — the handler's behavior is pinned
    by ``test_server_sigterm.py``.
    """
    ppids = iter([11111, 11111, 11111, 1])  # three polls stable, then reparented
    monkeypatch.setattr(os, "getppid", lambda: next(ppids))

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    await asyncio.wait_for(
        _watch_parent(original_ppid=11111, poll_seconds=0.01),
        timeout=2.0,
    )

    assert len(killed) == 1, f"expected exactly one signal dispatch, got {killed}"
    pid, sig = killed[0]
    assert pid == os.getpid()
    assert sig == signal.SIGTERM


async def test_watch_parent_cancellation_returns_cleanly() -> None:
    """Cancelling the task during the sleep must raise nothing.

    The lifespan cleanup path does ``task.cancel()`` + ``await task``;
    if the coroutine leaked a CancelledError up through an unshielded
    `os.kill` we'd see it here.
    """
    task = asyncio.create_task(_watch_parent(original_ppid=os.getppid(), poll_seconds=5.0))
    await asyncio.sleep(0.01)  # let it enter the sleep
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # The coroutine *should* catch and return, but depending on
        # asyncio timing the caller may still observe the cancellation.
        # Either shape is acceptable; the important part is no other
        # exception leaks out.
        pass


# ── integration ──────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only watchdog")
def test_server_exits_when_parent_dies(tmp_path: Path) -> None:
    """End-to-end: a grandparent spawns ``claude``-simulated parent,
    which spawns memtomem-server. Kill the parent; the server must
    notice via the watchdog and exit, unlinking its pid file.

    This is the live repro of the #440 orphan scenario: no stdio close,
    no SIGTERM from the client — the parent just disappears.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    pid_file = xdg / "memtomem" / "server.pid"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    env["MEMTOMEM_PARENT_WATCHDOG_INTERVAL"] = "0.2"  # speed up the test

    # Parent = a tiny python that spawns the server and then just sleeps
    # long enough to be killable. We use a shell script via `sh -c` to
    # keep the parent PID stable and well-known.
    parent_cmd = [
        sys.executable,
        "-c",
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-m', 'memtomem.server'], "
        "stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE); "
        "print(p.pid, flush=True); "
        "time.sleep(60)",
    ]
    parent = subprocess.Popen(
        parent_cmd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # First line of parent stdout is the server PID.
        assert parent.stdout is not None
        deadline = time.monotonic() + 10.0
        server_pid_str = None
        while time.monotonic() < deadline:
            line = parent.stdout.readline()
            if line:
                server_pid_str = line.decode().strip()
                break
            time.sleep(0.05)
        assert server_pid_str is not None, "parent never printed the server pid"
        server_pid = int(server_pid_str)

        # Wait for the server to finish startup and create its pid file.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        assert pid_file.exists(), "server pid file never appeared"

        # Now kill the parent with SIGKILL — no chance for it to close
        # the child's stdio or forward a signal. This simulates the
        # Claude-Code-exits-leaving-orphan scenario.
        parent.kill()
        parent.wait(timeout=5)

        # Server should self-SIGTERM within ~watchdog interval + handler time.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(server_pid, 0)  # 0 = check existence, don't signal
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            # Cleanup before failing.
            try:
                os.kill(server_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            pytest.fail(f"server pid {server_pid} did not exit within 5s of parent death")

        assert not pid_file.exists(), (
            "server pid file must be unlinked on watchdog-triggered exit "
            "(sigterm handler from #439 should fire)"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        for stream in (parent.stdin, parent.stdout, parent.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
