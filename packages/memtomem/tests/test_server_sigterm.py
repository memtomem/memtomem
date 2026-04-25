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


def test_sigterm_handler_unlinks_all_pid_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Variadic form: during the #412 transition ``main()`` tracks two pid
    files (new XDG path + legacy ``~/.memtomem/.server.pid``). Both must
    be cleaned up on SIGTERM, otherwise the next server start hits the
    stale-legacy-lock branch (#437)."""
    xdg_pid = tmp_path / "server.pid"
    legacy_pid = tmp_path / "legacy.pid"
    xdg_pid.write_text("12345")
    legacy_pid.write_text("12345")

    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))
    monkeypatch.setattr(os, "_exit", lambda code: None)

    _install_sigterm_handler(xdg_pid, legacy_pid)
    captured[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]

    assert not xdg_pid.exists(), "XDG pid file must be unlinked"
    assert not legacy_pid.exists(), "legacy pid file must be unlinked (#437)"


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM semantics differ on Windows; server is POSIX-only"
)
def test_sigterm_unlinks_legacy_pid_file_end_to_end(tmp_path: Path) -> None:
    """Issue #437: when ``~/.memtomem/`` exists but no live server holds
    the legacy flock, a new server acquires it, runs, and must unlink
    the legacy pid file on SIGTERM too.

    Without the fix, the legacy file is left behind after every shutdown.
    The next start opens it, fails ``flock`` intermittently under
    parallel probes (``claude mcp list`` probing multiple MCP servers),
    and prints the misleading "pre-0.1.25 install" message.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".memtomem").mkdir()  # triggers _try_hold_legacy_flock's is_dir() gate
    legacy_pid = home / ".memtomem" / ".server.pid"
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    xdg_pid = xdg / "memtomem" / "server.pid"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, xdg_pid)
        assert legacy_pid.exists(), (
            "server should have created the legacy pid file on acquiring the flock"
        )

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("Server did not exit within 10s of SIGTERM")

        assert not legacy_pid.exists(), (
            "legacy pid file must be unlinked on SIGTERM (#437); still present leaves "
            "a stale artifact that the next server spawn misreads as a pre-0.1.25 holder"
        )
    finally:
        _cleanup_proc(proc)


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_server_warns_but_proceeds_when_legacy_lock_held_exclusively(
    tmp_path: Path,
) -> None:
    """#444: legacy flock contention must NOT be a fatal exit.

    A pre-0.1.25 server (simulated here with ``LOCK_EX``) holds the
    legacy pid file. The new 0.1.26+ server tries ``LOCK_SH`` on the
    same file → fails → falls through to the XDG flock path and
    continues. We assert the server reaches the pid-file-written state
    (= past both flock gates) rather than exiting non-zero, because
    the previous behavior (``sys.exit(1)``) also blocked two *current*
    0.1.26 instances from coexisting, which is the ``#444`` bug.

    Cross-version protection is still preserved by the pre-0.1.25
    server's own ``LOCK_EX`` check — it fails when our ``LOCK_SH`` is
    already held. That direction is pinned by
    ``test_two_post_412_servers_coexist_with_shared_lock`` below
    (inverted: we hold ``LOCK_SH``, ``LOCK_EX`` probe must fail).
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
    xdg_pid = xdg / "memtomem" / "server.pid"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    holder = open(legacy_pid, "a+b")  # noqa: SIM115 — held for test scope
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        proc = _spawn_server(env)
        try:
            _wait_for_pid_file(proc, xdg_pid)
            # Server survived the legacy-flock contention and wrote its
            # XDG pid file — exactly the behavior #444 requires.
            assert proc.poll() is None, (
                "server must stay alive when legacy flock is held exclusively "
                "(#444); fatal exit would block multi-instance usage"
            )
        finally:
            _cleanup_proc(proc)
    finally:
        try:
            _fcntl.flock(holder, _fcntl.LOCK_UN)
        except OSError:
            pass
        holder.close()


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_two_post_412_servers_coexist_with_shared_lock(tmp_path: Path) -> None:
    """#444 primary repro: two 0.1.26 servers must be able to run at
    the same time (different projects / Claude Code sessions).

    Both acquire ``LOCK_SH`` on the legacy pid file; neither blocks the
    other. Previously (``LOCK_EX``) the second would ``sys.exit(1)``
    — the whole motivation for this fix.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".memtomem").mkdir()

    xdg1 = tmp_path / "xdg1"
    xdg1.mkdir()
    os.chmod(xdg1, 0o700)
    pid1 = xdg1 / "memtomem" / "server.pid"

    xdg2 = tmp_path / "xdg2"
    xdg2.mkdir()
    os.chmod(xdg2, 0o700)
    pid2 = xdg2 / "memtomem" / "server.pid"

    env1 = os.environ.copy()
    env1["HOME"] = str(home)
    env1["XDG_RUNTIME_DIR"] = str(xdg1)
    env2 = os.environ.copy()
    env2["HOME"] = str(home)
    env2["XDG_RUNTIME_DIR"] = str(xdg2)

    proc1 = _spawn_server(env1)
    proc2 = None
    try:
        _wait_for_pid_file(proc1, pid1)
        proc2 = _spawn_server(env2)
        _wait_for_pid_file(proc2, pid2)

        assert proc1.poll() is None, "first instance must stay alive"
        assert proc2.poll() is None, (
            "second instance must coexist with the first (#444); it used to "
            "exit(1) on the legacy LOCK_EX guard"
        )
    finally:
        _cleanup_proc(proc1)
        if proc2 is not None:
            _cleanup_proc(proc2)


def test_legacy_lock_sh_allows_multiple_holders(tmp_path: Path) -> None:
    """Unit-level pin for the core fcntl semantics the fix relies on.

    Two `LOCK_SH | LOCK_NB` acquires on the same file from the same
    process must both succeed. If a future Python / kernel quirk ever
    breaks this, the coexistence integration tests above would stop
    proving what they claim; this test catches that regression at the
    primitive level.
    """
    import fcntl as _fcntl

    path = tmp_path / "shared-lock.pid"
    path.touch()

    fp1 = open(path, "a+b")  # noqa: SIM115
    fp2 = open(path, "a+b")  # noqa: SIM115
    try:
        _fcntl.flock(fp1, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        # The second acquire on a different fd of the same file must succeed.
        _fcntl.flock(fp2, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        # And a LOCK_EX from a third handle must fail while both SH are held,
        # which is how cross-version mutex stays intact.
        fp3 = open(path, "a+b")  # noqa: SIM115
        try:
            with pytest.raises((BlockingIOError, OSError)):
                _fcntl.flock(fp3, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        finally:
            fp3.close()
    finally:
        try:
            _fcntl.flock(fp1, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            _fcntl.flock(fp2, _fcntl.LOCK_UN)
        except OSError:
            pass
        fp1.close()
        fp2.close()


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_contended_server_start_preserves_pid_file_content(tmp_path: Path) -> None:
    """Regression: a contended server start must NOT truncate the live
    server's pid file when the flock probe bails.

    Pre-fix, ``main()`` opened the pid file with ``open(..., "w")`` which
    truncates *before* ``fcntl.flock`` is checked. So a second server
    starting while the first held the lock zeroed out the file content
    even though the first server kept running. The user-visible symptom:
    ``mm uninstall`` reports ``Server still running (pid None)`` and
    ``lsof`` loses the recorded process identity, defeating the whole
    point of writing the pid in the first place.

    Repro: pre-create the pid file with known content and hold
    ``LOCK_EX`` on it, spawn the server, and assert the recorded pid
    survived. The fix uses ``open(..., "a+")`` + post-lock truncate so
    contended starts leave the live file alone.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    sub = xdg / "memtomem"
    sub.mkdir()
    os.chmod(sub, 0o700)
    pid_file = sub / "server.pid"
    pid_file.write_text("12345")

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)

    import fcntl as _fcntl

    holder = open(pid_file, "a+b")  # noqa: SIM115 — held for test scope
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        proc = _spawn_server(env)
        try:
            # The open + flock + warning log runs synchronously at
            # startup, well before mcp.run() spins up the asyncio loop.
            # 1.5s is generous coverage for cold-start interpreter
            # overhead while still failing fast on the regression.
            time.sleep(1.5)
            assert proc.poll() is None, (
                "server must stay alive when another holder owns the flock; "
                f"exited rc={proc.returncode}"
            )
            assert pid_file.read_text() == "12345", (
                "contended server start truncated the live pid file — this "
                "is the open(..., 'w') race the fix replaces with "
                "open(..., 'a+') + post-lock truncate. Got: "
                f"{pid_file.read_text()!r}"
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
