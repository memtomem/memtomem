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


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM semantics differ on Windows; server is POSIX-only"
)
def test_sigterm_unlinks_pid_file_end_to_end(tmp_path: Path) -> None:
    """Spawn ``memtomem-server`` as a subprocess, send SIGTERM, verify cleanup.

    Without this end-to-end coverage the unit tests above would still
    pass even if ``main()`` never installed the handler at all — the
    point of #387 is the observable behavior on a live process, not the
    handler shape in isolation.

    Isolation: ``HOME`` + ``XDG_RUNTIME_DIR`` both point under
    ``tmp_path`` so the server writes to
    ``tmp_path/xdg_runtime/memtomem/server.pid`` (see #412).
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    pid_file = xdg / "memtomem" / "server.pid"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    # ``stdin=subprocess.PIPE`` (kept open) makes the stdio MCP loop block on
    # the JSON-RPC read — without this the server sees EOF immediately and
    # exits cleanly via the normal path, defeating the SIGTERM check.
    proc = subprocess.Popen(
        [sys.executable, "-m", "memtomem.server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for the server to register its pid file. 10 s budget covers
        # cold imports (sqlite_vec, embedder factory, etc.) on slow CI hosts.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pid_file.exists():
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                pytest.fail(
                    f"Server died before writing pid file (rc={proc.returncode}). stderr:\n{stderr}"
                )
            time.sleep(0.1)
        if not pid_file.exists():
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(f"pid file did not appear within 10s. stderr:\n{stderr}")

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
