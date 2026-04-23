"""Test ``_install_sigterm_handler`` (issue #387).

Python's default SIGTERM behavior bypasses ``atexit``, so the
``.server.pid`` unlink registered in ``main()`` never fires when the
server is killed via SIGTERM (the signal ``pkill`` and supervisord send
by default).

``sys.exit(0)`` + ``atexit`` doesn't work either: ``mcp.run()`` runs an
asyncio event loop, which swallows ``SystemExit`` raised from a classic
``signal.signal`` handler. So the handler unlinks the pid file directly
and calls ``os._exit(0)`` to bypass the event loop.

The unit tests prove the handler shape; the integration test proves the
whole chain works against a live ``memtomem-server`` subprocess.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memtomem.server import _install_sigterm_handler


def test_install_sigterm_handler_registers_for_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))

    _install_sigterm_handler(tmp_path / ".server.pid")

    assert signal.SIGTERM in captured, "_install_sigterm_handler must bind SIGTERM"


def test_sigterm_handler_unlinks_pid_file_and_hard_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler must unlink the pid file and call ``os._exit(0)``.

    ``sys.exit`` would raise ``SystemExit``, which asyncio swallows — the
    integration test ``test_sigterm_unlinks_pid_file_end_to_end`` is the
    live repro. So the handler has to (a) unlink explicitly and (b) hard
    exit via ``os._exit`` to bypass the event loop entirely.
    """
    pid_file = tmp_path / ".server.pid"
    pid_file.write_text("12345")

    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))
    exit_calls: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exit_calls.append(code))

    _install_sigterm_handler(pid_file)
    handler = captured[signal.SIGTERM]
    handler(signal.SIGTERM, None)  # type: ignore[operator]

    assert not pid_file.exists(), "handler must unlink the pid file"
    assert exit_calls == [0], "handler must call os._exit(0), not sys.exit or return"


# ── integration ──────────────────────────────────────────────────────


def _spawn_server(env: dict[str, str]) -> subprocess.Popen:
    """Start ``memtomem-server`` as a subprocess that keeps its stdin
    open — without that, the MCP stdio loop sees EOF immediately and
    exits via the normal path, defeating any SIGTERM / lifecycle check."""
    return subprocess.Popen(
        [sys.executable, "-m", "memtomem.server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_pid_file(proc: subprocess.Popen, pid_file: Path, *, timeout: float = 10.0) -> None:
    """Poll until ``pid_file`` materialises or fail with the server's stderr."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pid_file.exists():
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(
                f"Server died before writing pid file (rc={proc.returncode}). stderr:\n{stderr}"
            )
        time.sleep(0.1)
    if not pid_file.exists():
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        pytest.fail(f"pid file did not appear within {timeout}s. stderr:\n{stderr}")


def _cleanup_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    # Python's Popen leaves these open if we don't close explicitly when
    # the test path bails early; closing here is idempotent.
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM semantics differ on Windows; server is POSIX-only"
)
def test_sigterm_unlinks_pid_file_end_to_end(tmp_path: Path) -> None:
    """Spawn ``memtomem-server`` as a subprocess, send SIGTERM, verify cleanup.

    Without this end-to-end coverage the unit tests above would still
    pass even if ``main()`` never installed the handler at all — the
    point of #387 is the observable behavior on a live process, not the
    handler shape in isolation.

    Also pins the #412 headline claim: with a fresh ``HOME`` (no
    pre-existing ``~/.memtomem/``), the server handshake must not
    create the state directory. The pid / flock write now lives on
    ``$XDG_RUNTIME_DIR/memtomem/server.pid``, so the persistent data
    root stays untouched until a tool call writes to it.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)  # _runtime_paths validator requires owner-only
    pid_file = xdg / "memtomem" / "server.pid"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, pid_file)

        # Headline claim for #412: the handshake must leave HOME alone.
        assert not (home / ".memtomem").exists(), (
            "~/.memtomem/ must not be created by MCP handshake (#412 goal); "
            "the server only writes to $XDG_RUNTIME_DIR/memtomem/"
        )

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("Server did not exit within 10s of SIGTERM — handler not installed?")

        assert not pid_file.exists(), (
            f"pid file should be unlinked after SIGTERM but is still present: "
            f"{pid_file.read_text() if pid_file.exists() else '<missing>'}"
        )
    finally:
        _cleanup_proc(proc)


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_server_uses_tempdir_fallback_when_xdg_unset(tmp_path: Path) -> None:
    """With ``$XDG_RUNTIME_DIR`` unset the server must land on the
    ``{tempfile.gettempdir()}/memtomem-{uid}/`` fallback, not silently
    refuse to start or write somewhere unexpected.

    Covers the code path that the default sigterm test skips (XDG set).
    Uses an isolated ``TMPDIR`` under ``tmp_path`` so we don't litter
    the real ``/var/folders/.../T/`` during the run.
    """
    home = tmp_path / "home"
    home.mkdir()
    tmp_tmp = tmp_path / "tmp"
    tmp_tmp.mkdir()
    expected_dir = tmp_tmp / f"memtomem-{os.geteuid()}"
    expected_pid = expected_dir / "server.pid"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp_tmp)
    env.pop("XDG_RUNTIME_DIR", None)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, expected_pid)
        assert stat_mode(expected_dir) == 0o700, (
            "tempdir fallback must create the subdir at owner-only mode"
        )
        assert not (home / ".memtomem").exists()
    finally:
        _cleanup_proc(proc)
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_server_refuses_when_legacy_lock_held(tmp_path: Path) -> None:
    """B1 regression (PR #413 review): a pre-#412 server holding
    ``~/.memtomem/.server.pid`` must block a post-#412 server from
    starting, or two writers would race on the same DB.

    We simulate the old server by holding ``fcntl.flock`` on the legacy
    path from the test process, then spawn the new server and assert it
    exits non-zero with a message pointing at the legacy pid file.
    """
    import fcntl as _fcntl

    home = tmp_path / "home"
    home.mkdir()
    (home / ".memtomem").mkdir()
    legacy_pid = home / ".memtomem" / ".server.pid"
    legacy_pid.touch()

    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    holder = open(legacy_pid, "a+b")  # noqa: SIM115 — held for test scope
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        proc = _spawn_server(env)
        try:
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "Server did not exit within 15s despite legacy flock held — "
                    "_try_hold_legacy_flock probably missing"
                )
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            assert rc != 0, (
                f"server must exit non-zero when legacy flock is held; rc={rc} stderr={stderr!r}"
            )
            assert str(legacy_pid) in stderr, (
                f"error message must point at the legacy pid file; stderr={stderr!r}"
            )
        finally:
            _cleanup_proc(proc)
    finally:
        try:
            _fcntl.flock(holder, _fcntl.LOCK_UN)
        except OSError:
            pass
        holder.close()


def stat_mode(path: Path) -> int:
    import stat as _stat

    return _stat.S_IMODE(path.stat().st_mode)
