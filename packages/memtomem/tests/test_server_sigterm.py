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
    sys.platform == "win32",
    reason="depends on signal.signal capture; Windows path does not register SIGTERM (#817)",
)
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
    create the state directory. The pid / flock write now lives on
    ``$XDG_RUNTIME_DIR/memtomem/server.pid``, so the persistent data
    root stays untouched until a tool call writes to it.
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
    pid_file = xdg / "memtomem" / _pin_store_pid_name(env, home)

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


def test_server_uses_tempdir_fallback_when_xdg_unset(tmp_path: Path) -> None:
    """With ``$XDG_RUNTIME_DIR`` unset the server must land on the
    ``{tempfile.gettempdir()}/memtomem-{uid}/`` fallback, not silently
    refuse to start or write somewhere unexpected.

    Covers the code path that the default sigterm test skips (XDG set).
    Uses an isolated tempdir under ``tmp_path`` so we don't litter the
    real ``/var/folders/.../T/`` (POSIX) or ``%LOCALAPPDATA%\\Temp``
    (Windows) during the run.

    Cross-platform notes (#817):

    - ``Path.home()`` reads ``HOME`` on POSIX and ``USERPROFILE`` on
      Windows; setting both keeps the subprocess hermetic on either OS
      (mirrors ``tests/helpers.py::set_home``, which we cannot reuse here
      because it operates on ``monkeypatch`` not on a subprocess env dict).
    - ``tempfile.gettempdir()`` reads ``TMPDIR`` on POSIX but on Windows
      it picks the first of ``TMP`` / ``TEMP`` / ``USERPROFILE`` it
      finds. Set all three to land in our isolated dir regardless of
      backend.
    - The ``uid`` suffix collapses to ``0`` on Windows (``os.geteuid``
      doesn't exist) — mirror ``_runtime_paths.runtime_dir()`` line 99.
    - The ``0o700`` mode-bit assert is POSIX-only: NTFS synthesizes
      mode bits and ``_runtime_paths.ensure_runtime_dir`` deliberately
      skips the chmod gate on Windows.
    """
    home = tmp_path / "home"
    home.mkdir()
    tmp_tmp = tmp_path / "tmp"
    tmp_tmp.mkdir()
    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    expected_dir = tmp_tmp / f"memtomem-{uid}"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = str(tmp_tmp)
    env["TMP"] = str(tmp_tmp)
    env["TEMP"] = str(tmp_tmp)
    env.pop("XDG_RUNTIME_DIR", None)
    expected_pid = expected_dir / _pin_store_pid_name(env, home)

    proc = _spawn_server(env)
    try:
        _wait_for_pid_file(proc, expected_pid)
        if os.name != "nt":
            assert stat_mode(expected_dir) == 0o700, (
                "tempdir fallback must create the subdir at owner-only mode"
            )
        assert not (home / ".memtomem").exists()
    finally:
        _cleanup_proc(proc)
        proc.wait(timeout=5)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "no SIGTERM equivalent on Windows; teardown path is atexit-only "
        "and the legacy flock probe short-circuits on Windows (#817)"
    ),
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

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    xdg_pid = xdg / "memtomem" / _pin_store_pid_name(env, home)

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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "legacy flock is POSIX-only by design (#817): #444 contention only "
        "matters for Linux pre-0.1.25 holdovers, which cannot exist on Windows"
    ),
)
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

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    xdg_pid = xdg / "memtomem" / _pin_store_pid_name(env, home)

    holder = open(legacy_pid, "a+b")  # held for test scope
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "LOCK_SH coexistence is only required against pre-0.1.25 legacy servers, "
        "which are POSIX-only by construction (#817)"
    ),
)
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

    xdg2 = tmp_path / "xdg2"
    xdg2.mkdir()
    os.chmod(xdg2, 0o700)

    env1 = os.environ.copy()
    env1["HOME"] = str(home)
    env1["USERPROFILE"] = str(home)
    env1["XDG_RUNTIME_DIR"] = str(xdg1)
    pid1 = xdg1 / "memtomem" / _pin_store_pid_name(env1, home)
    env2 = os.environ.copy()
    env2["HOME"] = str(home)
    env2["USERPROFILE"] = str(home)
    env2["XDG_RUNTIME_DIR"] = str(xdg2)
    pid2 = xdg2 / "memtomem" / _pin_store_pid_name(env2, home)

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


def test_two_servers_on_different_stores_do_not_contend(tmp_path: Path) -> None:
    """#1990 symptom 1: servers on *different* stores must not share a pid
    file, so the second one must neither warn about another instance nor
    contend for the first one's lock.

    Both subprocesses share one ``XDG_RUNTIME_DIR`` (same user) but point
    ``MEMTOMEM_STORAGE__SQLITE_PATH`` at different databases. Expected:
    two distinct ``server-*.pid`` files, both servers alive, and the
    second server's stderr free of the same-store contention warning.

    No shared ``HOME`` ``.memtomem`` dir is created, so the legacy
    interlock (POSIX-only ``LOCK_SH`` on ``~/.memtomem/.server.pid``)
    stays out of the picture on every platform.
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
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
        env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(db)
        return env

    pid1 = xdg / "memtomem" / f"server-{store_pid_digest(db1)}.pid"
    pid2 = xdg / "memtomem" / f"server-{store_pid_digest(db2)}.pid"
    assert pid1 != pid2, "different stores must derive different pid file names"

    proc1 = _spawn_server(_env_for(db1))
    proc2 = None
    try:
        _wait_for_pid_file(proc1, pid1)
        proc2 = _spawn_server(_env_for(db2))
        _wait_for_pid_file(proc2, pid2)

        assert proc1.poll() is None, "first server must stay alive"
        assert proc2.poll() is None, "second server must stay alive"

        # The contention warning logs synchronously during startup, before
        # the pid file is written on the winner branch — by the time pid2
        # exists, a spurious warning would already be in stderr, so a
        # plain kill (cross-platform, no SIGTERM dependency) loses nothing.
        proc2.kill()
        proc2.wait(timeout=10)
        stderr2 = proc2.stderr.read().decode(errors="replace") if proc2.stderr else ""
        assert "already writing to this store" not in stderr2, (
            f"cross-store server start must not log the contention warning; got:\n{stderr2}"
        )
        assert "Another instance is already running" not in stderr2, (
            f"old-form contention warning must not appear either; got:\n{stderr2}"
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
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    if os.name != "nt":
        os.chmod(xdg, 0o700)
    sub = xdg / "memtomem"
    sub.mkdir()
    if os.name != "nt":
        os.chmod(sub, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
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
            # ``Path.read_text()`` (#819): on Windows ``LockFileEx``
            # blocks reads from other handles, so opening a fresh handle
            # would raise ``PermissionError`` (ERROR_LOCK_VIOLATION) even
            # though the file content is intact. POSIX ``flock`` is
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
            # Positive pin for the same-store contention warning (#1990):
            # the cross-store test asserts the warning's *absence*, so
            # without this assertion the warning could be removed outright
            # and every test would stay green.
            proc.kill()
            proc.wait(timeout=10)
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            # The rich log handler hard-wraps the message and interleaves
            # its location column, so a single-substring match is brittle;
            # check whitespace-collapsed fragments instead.
            collapsed = " ".join(stderr.split())
            assert "Another memtomem-server is already" in collapsed and (
                "writing to this store" in collapsed
            ), f"same-store contention must log the warning; stderr:\n{stderr}"
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
    3. Pin ``Path.home()`` and ``XDG_RUNTIME_DIR`` to tmp paths so
       ``server_pid_path()`` and ``legacy_server_pid_path()`` land
       inside ``tmp_path``. Otherwise the test would write a real
       pid file into the developer's runtime dir.
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
        # Cross-platform: pid file must be non-empty. ``LockFileEx`` blocks
        # *content* reads from other handles on Windows (#819), but file
        # metadata via ``stat`` is unaffected — so a regression where
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
        # ``LockFileEx`` blocks reads from other handles, so this would
        # raise ``PermissionError`` (#819). The lock-owning handle lives
        # in ``main()``'s closure (``_lock_fp``) and isn't reachable from
        # here. The cross-platform ``stat().st_size`` check above pins the
        # "non-empty" half of the contract; the exact-pid check below is
        # additional POSIX-side coverage that the value is *this* process.
        if os.name != "nt":
            assert pid_file.read_text().strip() == str(os.getpid()), (
                "pid file must contain this process's pid"
            )
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
