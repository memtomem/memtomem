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

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from memtomem.server import (
    _install_sigterm_handler,
    _sigterm_deferred,
    _sigterm_targets,
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows path skips signal.signal registration (#817); see test_install_sigterm_handler_is_noop_on_windows",
)
def test_install_sigterm_handler_registers_for_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))

    _install_sigterm_handler(tmp_path / ".server.pid")

    assert signal.SIGTERM in captured, "_install_sigterm_handler must bind SIGTERM"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="depends on signal.signal capture; Windows path does not register SIGTERM (#817)",
)
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


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only: pins the no-op contract added in #817",
)
def test_install_sigterm_handler_is_noop_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, ``_install_sigterm_handler`` must NOT call ``signal.signal``.

    Python's SIGTERM is a no-op on Windows (the C runtime does not
    deliver it), so registering a handler would silently mislead
    readers into thinking shutdown was wired. The Windows path relies
    on FastMCP's stdin-EOF teardown + ``atexit`` instead. Pin the
    contract here so a future "let's just always register it" tweak
    surfaces as a test failure.
    """
    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))

    _install_sigterm_handler(tmp_path / ".server.pid")

    assert captured == {}, "Windows path must not call signal.signal"


# ── integration ──────────────────────────────────────────────────────


def _pin_store_pid_name(env: dict[str, str], home: Path) -> str:
    """Pin the subprocess's store path and return the expected pid filename.

    #1990 made the pid file name store-scoped (``server-<digest>.pid``).
    The digest is derived from the resolved SQLite path, so the tests must
    (a) pin ``MEMTOMEM_STORAGE__SQLITE_PATH`` explicitly — the parent's
    ambient value or the isolated ``HOME`` default would otherwise decide
    the name — and (b) compute the expectation through the production
    helper so macOS ``/tmp`` → ``/private/tmp`` resolution matches what
    the server computes.
    """
    from memtomem._runtime_paths import store_pid_digest

    db = home / ".memtomem" / "memtomem.db"
    env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(db)
    return f"server-{store_pid_digest(db)}.pid"


def _subprocess_runtime_dir(env: dict[str, str]) -> Path:
    """Return the suite's inherited subprocess-safe runtime anchor.

    Production has one fixed OS anchor (#2037). The suite uses a private,
    pytest-gated equivalent so these real server processes cannot contend
    with a developer's running memtomem server.
    """
    raw = env.get("_MEMTOMEM_TEST_RUNTIME_DIR")
    assert raw, "the autouse runtime isolation fixture must reach subprocess tests"
    return Path(raw)


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
    """Poll until ``pid_file`` materialises or fail with the server's stderr.

    The failure paths drain stderr through :func:`_kill_and_read_stderr`,
    never a bare ``read()``: a still-running server (or a grandchild
    holding an inherited pipe handle on Windows) keeps the write end open,
    so the read would block forever and the whole CI job would hang until
    the workflow timeout instead of reporting this failure.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pid_file.exists():
        if proc.poll() is not None:
            stderr = _kill_and_read_stderr(proc)
            pytest.fail(
                f"Server died before writing pid file (rc={proc.returncode}). stderr:\n{stderr}"
            )
        time.sleep(0.1)
    if not pid_file.exists():
        stderr = _kill_and_read_stderr(proc)
        pytest.fail(f"pid file did not appear within {timeout}s. stderr:\n{stderr}")


def _kill_and_read_stderr(proc: subprocess.Popen, *, timeout: float = 20.0) -> str | None:
    """Kill *proc* and drain its stderr, or ``None`` if it can't be drained.

    ``proc.stderr.read()`` blocks until EOF, which needs every handle on
    the pipe's write end to be closed. On Windows a grandchild can hold an
    inherited handle after the server itself is gone, and the read then
    never returns — a CI job that hangs until the workflow times out
    rather than failing. ``communicate(timeout=...)`` bounds it; callers
    treat ``None`` as "no evidence" and skip the assertion instead of
    reading an empty string as proof the log line was absent.
    """
    proc.kill()
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return err.decode(errors="replace") if err else ""


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
    sys.platform == "win32",
    reason="no SIGTERM equivalent on Windows; teardown path is atexit-only (#817)",
)
def test_sigterm_unlinks_pid_file_end_to_end(tmp_path: Path) -> None:
    """Spawn ``memtomem-server`` as a subprocess, send SIGTERM, verify cleanup.

    Without this end-to-end coverage the unit tests above would still
    pass even if ``main()`` never installed the handler at all — the
    point of #387 is the observable behavior on a live process, not the
    handler shape in isolation.

    Also pins the #412 headline claim: with a fresh ``HOME`` (no
    pre-existing ``~/.memtomem/``), the server handshake must not
    create the state directory. The pid / flock write lives on the stable
    per-user runtime anchor, so the persistent data root stays untouched
    until a tool call writes to it.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)  # _runtime_paths validator requires owner-only

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    pid_file = _subprocess_runtime_dir(env) / _pin_store_pid_name(env, home)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, pid_file)

        # Headline claim for #412: the handshake must leave HOME alone.
        assert not (home / ".memtomem").exists(), (
            "~/.memtomem/ must not be created by MCP handshake (#412 goal); "
            "the server only writes to its stable runtime anchor"
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


def test_server_stable_anchor_is_not_redirected_by_tmp_env(tmp_path: Path) -> None:
    """Different TMP/XDG values do not redirect server coordination.

    The production resolver invariant is pinned in ``test_runtime_paths``;
    this live-process test verifies that the server reaches the suite's
    inherited stable-anchor equivalent and leaves the environment-named temp
    directory untouched.

    Cross-platform notes (#817):

    - ``Path.home()`` reads ``HOME`` on POSIX and ``USERPROFILE`` on
      Windows; setting both keeps the subprocess hermetic on either OS
      (mirrors ``tests/helpers.py::set_home``, which we cannot reuse here
      because it operates on ``monkeypatch`` not on a subprocess env dict).
    - The ``0o700`` mode-bit assert is POSIX-only: NTFS synthesizes
      mode bits and ``_runtime_paths.ensure_runtime_dir`` deliberately
      skips the chmod gate on Windows.
    """
    home = tmp_path / "home"
    home.mkdir()
    tmp_tmp = tmp_path / "tmp"
    tmp_tmp.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = str(tmp_tmp)
    env["TMP"] = str(tmp_tmp)
    env["TEMP"] = str(tmp_tmp)
    env.pop("XDG_RUNTIME_DIR", None)
    expected_dir = _subprocess_runtime_dir(env)
    expected_pid = expected_dir / _pin_store_pid_name(env, home)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, expected_pid)
        if os.name != "nt":
            assert stat_mode(expected_dir) == 0o700, "stable runtime dir must be owner-only"
        assert not any(tmp_tmp.iterdir()), "TMP variables must not receive coordination state"
        assert not (home / ".memtomem").exists()
    finally:
        _cleanup_proc(proc)
        proc.wait(timeout=5)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="no SIGTERM equivalent on Windows; teardown path is atexit-only (#817)",
)
def test_server_start_creates_no_legacy_pid_file_end_to_end(tmp_path: Path) -> None:
    """#2003: the retired B1 interlock must not be reintroduced.

    ``~/.memtomem/`` exists — the exact condition that used to trigger
    ``_try_hold_legacy_flock``'s acquisition (which *created*
    ``.server.pid`` on every start). A current server must never touch
    the legacy path: not while running, not on SIGTERM teardown.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".memtomem").mkdir()  # the dir that used to trigger acquisition
    legacy_pid = home / ".memtomem" / ".server.pid"
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    runtime_pid = _subprocess_runtime_dir(env) / _pin_store_pid_name(env, home)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, runtime_pid)
        assert not legacy_pid.exists(), (
            "server must not create the legacy pid file (#2003); the B1 "
            "interlock was retired and re-materializing the file keeps the "
            "liveness gate user-global"
        )

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("Server did not exit within 10s of SIGTERM")

        assert not legacy_pid.exists(), "legacy pid file must still be absent after SIGTERM (#2003)"
    finally:
        _cleanup_proc(proc)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the legacy compatibility file never existed on Windows (#817)",
)
def test_server_ignores_exclusive_legacy_holder(tmp_path: Path) -> None:
    """#2003: an exclusive legacy holder must not affect server startup.

    A pre-0.1.25 server (simulated with ``LOCK_EX``) holds
    ``~/.memtomem/.server.pid``. The current server no longer probes or
    locks that path: it must start, write its own runtime pid file, and
    leave the legacy file — which it never owned — untouched on exit.
    (Before #2003 this scenario logged a "pre-0.1.25 install" warning and
    took a lifetime ``LOCK_SH`` when uncontended.)
    """
    import fcntl as _fcntl

    home = tmp_path / "home"
    home.mkdir()
    (home / ".memtomem").mkdir()
    legacy_pid = home / ".memtomem" / ".server.pid"
    legacy_pid.write_text("54321\n")

    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    runtime_pid = _subprocess_runtime_dir(env) / _pin_store_pid_name(env, home)

    holder = open(legacy_pid, "a+b")  # held for test scope
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        proc = _spawn_server(env)
        try:
            _wait_for_pid_file(proc, runtime_pid)
            assert proc.poll() is None, "server must start regardless of the legacy holder (#2003)"
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pytest.fail("Server did not exit within 10s of SIGTERM")
        finally:
            _cleanup_proc(proc)
        assert legacy_pid.read_text() == "54321\n", (
            "server must not touch a legacy pid file it never owned (#2003)"
        )
    finally:
        try:
            _fcntl.flock(holder, _fcntl.LOCK_UN)
        except OSError:
            pass
        holder.close()


def test_two_servers_on_different_stores_do_not_contend(tmp_path: Path) -> None:
    """#1990 symptom 1: servers on *different* stores must not share a pid
    file, so the second one must neither warn about another instance nor
    contend for the first one's lock.

    Both subprocesses land in one runtime directory (same user) but point
    ``MEMTOMEM_STORAGE__SQLITE_PATH`` at different databases. Expected:
    two distinct ``server-*.pid`` files, both servers alive, and the
    second server's stderr free of the same-store contention warning.

    The real subprocesses inherit the suite's private stable-anchor equivalent
    on every platform; the XDG/TMP variables below deliberately differ from
    that location and must not redirect it.

    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    tmp_tmp = tmp_path / "tmp"
    tmp_tmp.mkdir()
    if os.name != "nt":
        os.chmod(xdg, 0o700)

    from memtomem._runtime_paths import store_pid_digest

    db1 = tmp_path / "store-a" / "memtomem.db"
    db2 = tmp_path / "store-b" / "memtomem.db"

    def _env_for(db: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env["XDG_RUNTIME_DIR"] = str(xdg)
        env["TMPDIR"] = str(tmp_tmp)
        env["TMP"] = str(tmp_tmp)
        env["TEMP"] = str(tmp_tmp)
        env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(db)
        return env

    first_env = _env_for(db1)
    runtime = _subprocess_runtime_dir(first_env)
    pid1 = runtime / f"server-{store_pid_digest(db1)}.pid"
    pid2 = runtime / f"server-{store_pid_digest(db2)}.pid"
    assert pid1 != pid2, "different stores must derive different pid file names"

    proc1 = _spawn_server(first_env)
    proc2 = None
    try:
        _wait_for_pid_file(proc1, pid1)
        proc2 = _spawn_server(_env_for(db2))
        _wait_for_pid_file(proc2, pid2)

        assert proc1.poll() is None, "first server must stay alive"
        assert proc2.poll() is None, "second server must stay alive"

        # Two distinct pid files, both written: with one shared name the
        # second server would have found the first one's lock held and
        # taken the contention branch, which never writes a pid. That the
        # warning itself stays silent across stores is pinned in-process
        # by ``TestContentionWarningScope`` (a subprocess's stderr is not
        # reliably capturable after teardown on Windows).
        assert pid1.exists() and pid2.exists()
        if os.name != "nt":
            # The Windows mandatory range lock (``msvcrt.locking``) blocks
            # reads from other handles while the server holds its lock
            # (#819), so the content check is POSIX-only; the two distinct
            # files above are the cross-platform half.
            assert pid2.read_text().splitlines()[0] == str(proc2.pid), (
                "the second server must own its own pid file, not fall "
                "through to the first one's contention branch"
            )
    finally:
        _cleanup_proc(proc1)
        if proc2 is not None:
            _cleanup_proc(proc2)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: fcntl module does not exist on Windows",
)
def test_legacy_lock_sh_allows_multiple_holders(tmp_path: Path) -> None:
    """Unit-level pin for the core fcntl semantics the liveness probes rely on.

    Servers no longer take the legacy ``LOCK_SH`` (#2003), but
    ``cli/_liveness.py:probe_legacy_pid_file``'s shared-vs-exclusive
    classification still depends on exactly these flock semantics: SH
    composes with SH, and EX fails while any SH is held. If a future
    Python / kernel quirk ever breaks this, the classification would
    stop proving what it claims; this test catches that regression at
    the primitive level.
    """
    import fcntl as _fcntl

    path = tmp_path / "shared-lock.pid"
    path.touch()

    fp1 = open(path, "a+b")
    fp2 = open(path, "a+b")
    try:
        _fcntl.flock(fp1, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        # The second acquire on a different fd of the same file must succeed.
        _fcntl.flock(fp2, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
        # And a LOCK_EX from a third handle must fail while both SH are held,
        # which is how cross-version mutex stays intact.
        fp3 = open(path, "a+b")
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


def test_contended_server_start_preserves_pid_file_content(tmp_path: Path) -> None:
    """Regression: a contended server start must NOT truncate the live
    server's pid file when the flock probe bails.

    Pre-fix, ``main()`` opened the pid file with ``open(..., "w")`` which
    truncates *before* the lock is checked. So a second server starting
    while the first held the lock zeroed out the file content even though
    the first server kept running. The user-visible symptom: ``mm
    uninstall`` reports ``Server still running (pid None)`` and ``lsof``
    loses the recorded process identity, defeating the whole point of
    writing the pid in the first place.

    Repro: pre-create the pid file with known content and hold
    ``LOCK_EX`` on it, spawn the server, and assert the recorded pid
    survived. The fix uses ``open(..., "a+")`` + post-lock truncate so
    contended starts leave the live file alone.

    Cross-platform via portalocker (#817): the simulator holds an
    exclusive lock the same way ``main()`` does. ``"rb+"`` open keeps
    the ``MsvcrtLocker`` Windows backend happy (read-only handles
    fail with ``EACCES``).

    The holder and child both use the suite's private stable-anchor
    equivalent, so the test contends on the exact file without touching a
    developer's real runtime state.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    tmp_tmp = tmp_path / "tmp"
    tmp_tmp.mkdir()
    if os.name != "nt":
        os.chmod(xdg, 0o700)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    env["TMPDIR"] = str(tmp_tmp)
    env["TMP"] = str(tmp_tmp)
    env["TEMP"] = str(tmp_tmp)
    sub = _subprocess_runtime_dir(env)
    sub.mkdir()
    if os.name != "nt":
        os.chmod(sub, 0o700)
    # Wide terminal so the rich log handler doesn't hard-wrap the
    # contention warning asserted below (wrap points shift with width).
    env["COLUMNS"] = "300"
    pid_file = sub / _pin_store_pid_name(env, home)
    pid_file.write_text("12345")

    import portalocker

    holder = open(pid_file, "rb+")  # held for test scope
    try:
        portalocker.lock(holder, portalocker.LOCK_EX | portalocker.LOCK_NB)
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
            # Read THROUGH the lock-owning handle, not via a separate
            # ``Path.read_text()`` (#819): on Windows the mandatory range
            # lock (``msvcrt.locking``) blocks reads from other handles, so
            # opening a fresh handle would raise ``PermissionError``
            # (ERROR_LOCK_VIOLATION) even though the file content is intact. POSIX ``flock`` is
            # advisory and lets ``read_text`` through, so this works on
            # both — the holder-handle read is the cross-platform form.
            holder.seek(0)
            content = holder.read().decode("utf-8")
            assert content == "12345", (
                "contended server start truncated the live pid file — this "
                "is the open(..., 'w') race the fix replaces with "
                "open(..., 'a+') + post-lock truncate. "
                f"Got: {content!r}"
            )
            # The warning this contention emits is pinned in-process by
            # ``TestContentionWarningScope`` — a killed child's stderr comes
            # back empty on Windows, and the rich handler re-wraps the text,
            # so ``caplog`` is the reliable place to assert a log line.
        finally:
            _cleanup_proc(proc)
    finally:
        try:
            portalocker.unlock(holder)
        except (portalocker.LockException, OSError):
            pass
        holder.close()


def stat_mode(path: Path) -> int:
    import stat as _stat

    return _stat.S_IMODE(path.stat().st_mode)


# ── in-process regression pin (cross-platform, #817) ─────────────────


def test_server_main_acquires_portalocker_pid_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run ``memtomem.server.main()`` in-process and pin that it acquires
    a real exclusive lock on the pid file via ``portalocker``.

    Cross-platform regression net for #817 — works identically on Linux,
    macOS, and Windows. Without this pin, a future "swap portalocker
    back to fcntl" tweak would slip past the AST guard (which only
    catches *module-level* ``import fcntl``) and re-break Windows.

    Aggressive isolation, in this order:

    1. Stub ``_install_sigterm_handler`` to a no-op so ``main()`` does
       not replace the test process's global ``signal.signal(SIGTERM, ...)``
       handler. Going through the function-level seam (rather than
       monkeypatching ``signal.signal``) is cleaner — that's the entire
       intent boundary.
    2. Stub ``mcp.run`` to a no-op so the asyncio loop never starts.
    3. Pin ``Path.home()``, ``ensure_runtime_dir``, and ``server_pid_path``
       to tmp paths so no real runtime or persistent state is touched.
    4. Capture ``atexit.register`` calls. Without this the lock fd and
       pid file outlive the test even though ``mcp.run`` is no-op'd —
       ``main()`` registers cleanups via ``atexit`` expecting them to
       fire at process exit, but pytest never exits between tests.
    """
    import atexit
    import pathlib

    import memtomem._runtime_paths as runtime_paths

    from memtomem import server as server_mod
    from memtomem.cli._liveness import probe_pid_file

    tmp_home = tmp_path / "home"
    tmp_home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    runtime = xdg / "memtomem"
    runtime.mkdir()
    if os.name != "nt":
        os.chmod(xdg, 0o700)
        os.chmod(runtime, 0o700)

    captured_atexit: list[tuple] = []

    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *a, **kw: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setenv("TMPDIR", str(xdg))
    monkeypatch.setenv("TMP", str(xdg))
    monkeypatch.setenv("TEMP", str(xdg))
    monkeypatch.setattr(runtime_paths, "ensure_runtime_dir", lambda: runtime)
    monkeypatch.setattr(
        runtime_paths, "server_pid_path", lambda db_path=None: runtime / "server.pid"
    )
    monkeypatch.setattr(
        atexit,
        "register",
        lambda fn, *a, **kw: captured_atexit.append((fn, a, kw)) or fn,
    )

    pid_file = runtime_paths.server_pid_path()
    cleanup_ran = False
    try:
        server_mod.main([])

        assert pid_file.exists(), "main() must create the pid file"
        # Cross-platform: pid file must be non-empty. The Windows mandatory
        # range lock (``msvcrt.locking``) blocks *content* reads from other
        # handles (#819), but file metadata via ``stat`` is unaffected — so a regression where
        # ``main()`` creates and locks an empty pid file would still trip
        # this assertion on Windows. ``probe_pid_file`` (and uninstall /
        # status diagnostics) call ``read_text().strip()`` on the file when
        # it isn't currently locked, so an empty pid file would surface as
        # ``int("")`` → ``pid=None`` in ``ServerState`` — degraded UX even
        # though liveness still works.
        assert pid_file.stat().st_size > 0, (
            "pid file must not be empty — main() must write its pid before "
            "returning, on every platform"
        )
        # POSIX-only: read pid-file content via a fresh handle. Windows
        # ``msvcrt.locking`` blocks reads from other handles, so this would
        # raise ``PermissionError`` (#819). The lock-owning handle lives
        # in ``main()``'s closure (``_lock_fp``) and isn't reachable from
        # here. The cross-platform ``stat().st_size`` check above pins the
        # "non-empty" half of the contract; the exact-pid check below is
        # additional POSIX-side coverage that the value is *this* process
        # and includes a parseable generation timestamp.
        if os.name != "nt":
            lines = pid_file.read_text().splitlines()
            assert lines[0] == str(os.getpid()), "pid file must contain this process's pid"
            assert lines[1] == "", "server pid metadata reserves the Web UI port slot"
            assert datetime.fromisoformat(lines[2]).tzinfo is not None
        # The probe opens its own handle and tries LOCK_EX | LOCK_NB —
        # if main() is holding the lock, the probe must report alive.
        # ``probe_pid_file`` is designed to handle the Windows
        # lock-blocks-reads case (catches ``OSError`` at open and treats
        # as alive), so it works on every platform.
        assert probe_pid_file(pid_file).alive is True, (
            "probe_pid_file must see the lock as held while main() owns it"
        )

        # Run the captured atexit callbacks in LIFO order (mirrors atexit
        # itself). After they've all run the pid file MUST be gone — that
        # is the regression net for #818 review: the original two-callback
        # ``register(close); register(unlink)`` pattern relied on POSIX's
        # unlink-while-open semantics, which Windows refuses (PermissionError,
        # WinError 32). The composite cleanup fixes this; if a future change
        # reverts to two registrations, this assert fails on Windows because
        # ``pid_file.unlink`` raises while the lock fd is still open.
        for fn, args, kwargs in reversed(captured_atexit):
            fn(*args, **kwargs)
        cleanup_ran = True
        assert not pid_file.exists(), (
            "atexit cleanup must unlink the pid file on every platform; "
            "Windows requires close-before-unlink (#818 review)"
        )
    finally:
        # Belt-and-suspenders: if the asserts above bailed before the
        # cleanup loop, run it now so the test environment is clean.
        if not cleanup_ran:
            for fn, args, kwargs in reversed(captured_atexit):
                try:
                    fn(*args, **kwargs)
                except Exception:
                    pass


# ── _resolve_store_db_path (#1990) ───────────────────────────────────


class TestResolveStoreDbPath:
    """Pin the config layering the pid-file digest depends on."""

    def test_env_var_decides_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.server import _resolve_store_db_path

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "env.db"))

        assert _resolve_store_db_path() == tmp_path / "env.db"

    def test_dotenv_runs_before_the_config_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dotenv-provided SQLite override must reach the pid digest —
        the lifespan loads dotenv before building its config, so pid
        naming has to see the same value or the pid file would name one
        store while the server opens another. The seam is stubbed (rather
        than a real ``.env`` file) because ``load_dotenv()`` discovers the
        file relative to the *calling module*, not the test cwd."""
        from memtomem.server import lifespan
        from memtomem.server import _resolve_store_db_path

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("MEMTOMEM_STORAGE__SQLITE_PATH", raising=False)

        def _fake_dotenv() -> None:
            os.environ["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(tmp_path / "dotenv.db")

        monkeypatch.setattr(lifespan, "_load_dotenv", _fake_dotenv)

        try:
            assert _resolve_store_db_path() == tmp_path / "dotenv.db"
        finally:
            # The stub writes through to os.environ like load_dotenv does;
            # monkeypatch only tracks its own mutations, so clean up.
            os.environ.pop("MEMTOMEM_STORAGE__SQLITE_PATH", None)

    def test_returns_none_when_config_loading_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any config failure degrades to ``None`` → the caller falls back
        to the transitional bare ``server.pid`` instead of crashing the
        server before the lock dance."""
        import memtomem.config as config_mod
        from memtomem.server import _resolve_store_db_path

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        def _boom(cfg, *, migrate: bool = True) -> None:
            raise OSError("config layer unavailable")

        monkeypatch.setattr(config_mod, "load_config_overrides", _boom)

        assert _resolve_store_db_path() is None


# ── contention warning scope (#1990), in-process ─────────────────────


class TestContentionWarningScope:
    """The startup contention warning must fire for a same-store holder and
    stay silent for a foreign one.

    Asserted in-process against ``caplog`` rather than a subprocess's
    stderr: a killed child returns an empty stderr on Windows, and the
    rich handler re-wraps the text, so substring checks on captured output
    are unreliable in exactly the direction that matters (an empty capture
    silently satisfies the "no warning" half).
    """

    _MSG = "Another memtomem-server is already"

    def _run_main(self, tmp_path, monkeypatch, *, store: Path) -> None:
        """Run ``main()`` with the pid dance isolated under ``tmp_path``."""
        import atexit
        import pathlib

        import memtomem._runtime_paths as runtime_paths

        from memtomem import server as server_mod

        tmp_home = tmp_path / "home"
        tmp_home.mkdir(exist_ok=True)
        runtime = tmp_path / "runtime"
        runtime.mkdir(exist_ok=True)
        if os.name != "nt":
            os.chmod(runtime, 0o700)

        captured: list[tuple] = []
        monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *a, **kw: None)
        monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_home))
        monkeypatch.setattr(runtime_paths, "ensure_runtime_dir", lambda: runtime)
        monkeypatch.setattr(server_mod, "_resolve_store_db_path", lambda: store)
        monkeypatch.setattr(
            atexit, "register", lambda fn, *a, **kw: captured.append((fn, a, kw)) or fn
        )

        try:
            server_mod.main([])
        finally:
            for fn, args, kwargs in reversed(captured):
                try:
                    fn(*args, **kwargs)
                except Exception:
                    pass

    @contextlib.contextmanager
    def _hold(self, pid_file: Path):
        import portalocker

        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("12345", encoding="utf-8")
        fp = open(pid_file, "rb+")
        try:
            portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
            yield
        finally:
            try:
                portalocker.unlock(fp)
            except Exception:
                pass
            fp.close()

    def test_same_store_holder_warns(self, tmp_path, monkeypatch, caplog) -> None:
        from memtomem._runtime_paths import store_pid_digest

        store = tmp_path / "store" / "memtomem.db"
        held = tmp_path / "runtime" / f"server-{store_pid_digest(store)}.pid"

        with self._hold(held), caplog.at_level("WARNING", logger="memtomem.server"):
            self._run_main(tmp_path, monkeypatch, store=store)

        assert any(self._MSG in r.getMessage() for r in caplog.records), (
            f"same-store contention must warn; records={[r.getMessage() for r in caplog.records]}"
        )

    def test_foreign_store_holder_is_silent(self, tmp_path, monkeypatch, caplog) -> None:
        """The foreign server's lock is held under *both* names it could
        plausibly own: its own ``server-<digest>.pid`` and the bare
        ``server.pid`` a store-blind build would have used. Without the
        second holder, reverting the fix would put this server on the bare
        name, find nothing held there, and stay silent — the test would
        pass on exactly the code it exists to reject.
        """
        from memtomem._runtime_paths import store_pid_digest

        store = tmp_path / "store" / "memtomem.db"
        other = tmp_path / "other" / "memtomem.db"
        held = tmp_path / "runtime" / f"server-{store_pid_digest(other)}.pid"
        held_bare = tmp_path / "runtime" / "server.pid"

        with (
            self._hold(held),
            self._hold(held_bare),
            caplog.at_level("WARNING", logger="memtomem.server"),
        ):
            self._run_main(tmp_path, monkeypatch, store=store)

        assert not any(self._MSG in r.getMessage() for r in caplog.records), (
            "a live server on a different store must not trigger the "
            f"contention warning; records={[r.getMessage() for r in caplog.records]}"
        )


def test_handshake_only_server_registers_a_presence_marker(tmp_path: Path) -> None:
    """A server that never opens a store is still visible to the registry (#2230).

    This is the whole gap: the store-scoped sentinel is written from lazy
    initialization, so a session that handshakes and stops — the majority
    population on a real machine, 34 of 35 when it was measured — used to
    register nothing at all. The marker below is written at startup instead,
    and it must not cost the #412 invariant this file already pins: the
    coordination write lands on the runtime anchor, never in ``~/.memtomem``.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    runtime = _subprocess_runtime_dir(env)
    pid_file = runtime / _pin_store_pid_name(env, home)
    presence = runtime / "presence"
    instances = runtime / "instances"

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, pid_file)
        deadline = time.time() + 10.0
        markers: list[Path] = []
        while time.time() < deadline:
            markers = list(presence.glob("*.lock")) if presence.exists() else []
            if markers:
                break
            time.sleep(0.05)

        assert markers, "a handshake-only server must register a presence marker"
        assert len(markers) == 1, f"one marker per process, found {[m.name for m in markers]}"
        # The store-scoped sentinel is still lazy — nothing has opened a DB.
        assert not instances.exists() or not list(instances.glob("*.lock")), (
            "no store is open, so no sentinel may exist"
        )
        assert not (home / ".memtomem").exists(), (
            "the startup marker must not resurrect the #412 handshake write"
        )
    finally:
        _cleanup_proc(proc)


def test_sigterm_targets_name_only_files_this_process_owns() -> None:
    """The handler unlinks without re-checking ownership, so the list must be exact."""
    pid_file = Path("/nonexistent/server.pid")
    marker = Path("/nonexistent/presence/1-2-a.lock")

    class _Registered:
        path = marker

    assert _sigterm_targets(pid_file, _Registered()) == (pid_file, marker)
    # A server that lost the pid-file flock owns its marker and nothing else;
    # unlinking the pid file there would yank it from the primary holder.
    assert _sigterm_targets(None, _Registered()) == (marker,)
    # Registration can decline (untrusted directory, lock timeout) — there is
    # then nothing extra to clean up, and no ``None`` may reach the handler.
    assert _sigterm_targets(pid_file, None) == (pid_file,)
    assert _sigterm_targets(None, None) == ()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is not delivered on Windows (#817)")
def test_sigterm_removes_the_presence_marker(tmp_path: Path) -> None:
    """``os._exit(0)`` bypasses ``atexit``, so the handler must unlink it itself.

    Left behind, the marker is not counted as a live server (the kernel drops
    its flock at exit, so it probes stale) but it is residue until a later
    registration sweeps it — and on a host whose servers are all killed by
    signal, no later registration may come.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    runtime = _subprocess_runtime_dir(env)
    pid_file = runtime / _pin_store_pid_name(env, home)
    presence = runtime / "presence"

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, pid_file)
        deadline = time.time() + 10.0
        while time.time() < deadline and not (presence.exists() and list(presence.glob("*.lock"))):
            time.sleep(0.05)
        assert list(presence.glob("*.lock")), "premise: the marker was written"

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("Server did not exit within 10s of SIGTERM")

        assert not list(presence.glob("*.lock")), (
            "the presence marker must be unlinked on SIGTERM, not left as residue"
        )
    finally:
        _cleanup_proc(proc)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is not delivered on Windows (#817)")
def test_second_server_cleans_its_marker_without_touching_the_primary(tmp_path: Path) -> None:
    """The lock-contended arm owns a marker even though it owns no pid file."""
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    runtime = _subprocess_runtime_dir(env)
    pid_file = runtime / _pin_store_pid_name(env, home)
    presence = runtime / "presence"

    first = _spawn_server(env)
    second = None
    try:
        _wait_for_pid_file(first, pid_file)
        second = _spawn_server(env)
        deadline = time.time() + 15.0
        while time.time() < deadline and len(list(presence.glob("*.lock"))) < 2:
            if second.poll() is not None:
                pytest.fail(f"second server died (rc={second.returncode})")
            time.sleep(0.1)
        assert len(list(presence.glob("*.lock"))) == 2, (
            "a second server on one store is exactly the accumulation this marker "
            "exists to show, and the pid file cannot represent it"
        )

        second.send_signal(signal.SIGTERM)
        second.wait(timeout=10)
        deadline = time.time() + 10.0
        while time.time() < deadline and len(list(presence.glob("*.lock"))) > 1:
            time.sleep(0.05)

        assert len(list(presence.glob("*.lock"))) == 1, "the secondary removed its own marker"
        assert pid_file.exists(), "and never touched the primary's pid file"
        assert first.poll() is None, "the primary is still running"
    finally:
        if second is not None:
            _cleanup_proc(second)
        _cleanup_proc(first)


def test_unregisterable_presence_directory_warns_but_still_starts(tmp_path: Path) -> None:
    """Registration declines by returning ``None``, not by raising.

    A server that cannot be counted must still start, and the operator must
    still be told — otherwise ``mm doctor`` under-reports with no signal, the
    exact silence #2230 was filed about.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    runtime = _subprocess_runtime_dir(env)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (runtime / "presence").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, runtime / _pin_store_pid_name(env, home))
        assert proc.poll() is None, "a coordination failure must not stop the server"
        assert list(elsewhere.iterdir()) == [], "and must not write through the link"
        stderr = _kill_and_read_stderr(proc)
        assert stderr is not None and "under-report" in stderr, (
            f"the dropped registration must be reported; stderr was:\n{stderr}"
        )
    finally:
        _cleanup_proc(proc)


@pytest.mark.skipif(sys.platform == "win32", reason="no pthread_sigmask on Windows (#817)")
def test_sigterm_deferral_restores_the_caller_mask() -> None:
    """The span defers SIGTERM; it does not decide the caller's mask.

    An embedder may run ``main()`` on a thread that already blocks SIGTERM.
    Unblocking on exit would hand back a mask it never chose and deliver the
    very signal it was deferring.
    """
    import signal

    original = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        # Entered unblocked: blocked inside, unblocked again on exit.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        with _sigterm_deferred():
            assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, set())

        # Entered blocked: it must still be blocked afterwards.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        with _sigterm_deferred():
            pass
        assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set()), (
            "the caller's deferral must survive ours"
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, original)
