"""``memtomem-server --shared NAME``: the stdio bridge and its admission (#3381).

The end-to-end tests run real bridges as subprocesses against daemons they
start themselves, in the isolated environment of ``test_server_daemon.py``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from test_server_daemon import Client, DaemonEnv, _wait
from test_server_daemon import denv  # noqa: F401 - the fixture, re-exported

posix_only = pytest.mark.skipif(os.name == "nt", reason="the shared daemon is POSIX only")

_BRIDGE = "import sys; from memtomem._server_entry import main; main(sys.argv[1:])"


# ── admission fields ─────────────────────────────────────────────────────────


def _fields(**overrides: object) -> dict:
    base = {
        "env": [["MEMTOMEM_A", "h1"], ["LANGFUSE_HOST", "h2"]],
        "tools": ["core", []],
        "home": "/home/u",
        "store": "/s/store.db",
        "install": ["/venv", "/venv/lib/memtomem", "1.0"],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "theirs, expected",
    [
        (_fields(), []),
        (_fields(tools=["full", []]), ["tools"]),
        (_fields(home="/home/v"), ["home"]),
        (_fields(install=["/venv", "/other/memtomem", "1.0"]), ["install"]),
        (_fields(install=["/venv", "/venv/lib/memtomem", "1.1"]), ["install"]),
        (_fields(store="/s/other.db"), ["store"]),
        (_fields(env=[["MEMTOMEM_A", "h9"], ["LANGFUSE_HOST", "h2"]]), ["env (MEMTOMEM_A)"]),
        (_fields(env=[["MEMTOMEM_A", "h1"]]), ["env (LANGFUSE_HOST)"]),
        (
            _fields(env=[["MEMTOMEM_A", "h1"], ["LANGFUSE_HOST", "h2"], ["memtomem_b", "h3"]]),
            ["env (memtomem_b)"],
        ),
        (_fields(env=[["LANGFUSE_HOST", "h2"], ["MEMTOMEM_A", "h1"]]), ["env (order)"]),
        (_fields(tools=["full", []], home="/x"), ["tools", "home"]),
    ],
)
def test_differences_name_what_differs(theirs: dict, expected: list[str]) -> None:
    from memtomem._admission import differences

    assert differences(_fields(), theirs) == expected


def test_the_fingerprint_carries_names_and_digests_never_values() -> None:
    from memtomem._admission import env_fingerprint

    secret = "sk-not-a-real-key-0123456789"
    environ = {
        "PATH": "/bin",
        "MEMTOMEM_TOOL_MODE": "full",
        "memtomem_lower": "x",
        "LANGFUSE_SECRET_KEY": secret,
        "_MEMTOMEM_PRIVATE": "y",
    }
    fingerprint = env_fingerprint(environ)

    assert [name for name, _ in fingerprint] == [
        "MEMTOMEM_TOOL_MODE",
        "memtomem_lower",
        "LANGFUSE_SECRET_KEY",
    ]
    assert secret not in json.dumps(fingerprint)


def test_the_store_field_is_the_path_the_caller_resolved(tmp_path: Path) -> None:
    from memtomem._admission import local_fields

    fields = local_fields(tools=["core", []], store=tmp_path / "store.db")

    assert fields["store"] == str(tmp_path / "store.db")


def test_the_tool_request_reads_the_environment_as_the_server_does() -> None:
    from memtomem._admission import tool_surface_request

    assert tool_surface_request({}) == ["core", []]
    assert tool_surface_request(
        {"MEMTOMEM_TOOL_MODE": "Standard", "MEMTOMEM_EXTRA_TOOLS": " b, a,,b "}
    ) == ["standard", ["a", "b"]]


def test_fields_read_tools_before_dotenv_and_leave_the_environment_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.env``-only tool mode reaches neither side's tool surface, so neither compares it."""
    import memtomem._bridge as bridge
    import memtomem._dotenv as dotenv_mod

    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "store.db"))
    monkeypatch.delenv("MEMTOMEM_TOOL_MODE", raising=False)

    def load_dotenv() -> list[str]:
        os.environ["MEMTOMEM_TOOL_MODE"] = "full"
        os.environ["MEMTOMEM_FROM_DOTENV"] = "1"
        return []

    monkeypatch.setattr(dotenv_mod, "load_dotenv", load_dotenv)
    before = dict(os.environ)

    fields = bridge._admission_fields()

    assert fields["tools"] == ["core", []]
    assert [n for n, _ in fields["env"] if n == "MEMTOMEM_FROM_DOTENV"] == ["MEMTOMEM_FROM_DOTENV"]
    assert dict(os.environ) == before


@posix_only
@pytest.mark.parametrize(
    "store, reason",
    [
        (":memory:", "cannot be named"),
        ("file:private-sentinel-value?mode=memory", "cannot be named"),
        ("relative.db", "relative configured path: storage.sqlite_path"),
    ],
)
def test_a_store_that_cannot_be_shared_falls_back_without_connecting(
    store: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", store)
    monkeypatch.setattr(bridge, "_join", _must_not_run)
    monkeypatch.setattr(bridge, "_spawn", _must_not_run)

    said = bridge.run_bridge("t")

    # The reason is logged and shown by ``mem_status``: it names the setting, not its value.
    assert reason in said and "storage.sqlite_path" in said
    assert store not in said


def test_windows_falls_back_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge.os, "name", "nt")
    monkeypatch.setattr(bridge, "_admission_fields", _must_not_run)

    assert bridge.run_bridge("t") == "shared daemons are POSIX only"


def _must_not_run(*args: object, **kwargs: object) -> None:
    raise AssertionError("must not run")


# ── the dispatcher ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--shared", "a", "--daemon", "a"], "cannot be combined"),
        (["--shared", "a", "--transport", "sse"], "stdio only"),
        (["--daemon", "a", "--port", "9"], "no transport options: --port"),
        # Given with its default value is still given.
        (["--daemon", "a", "--transport", "stdio"], "no transport options: --transport"),
        (["--daemon", "a", "--host", "127.0.0.1"], "no transport options: --host"),
        (["--daemon", "a", "--port", "8000"], "no transport options: --port"),
        (["--idle-timeout", "5"], "needs --daemon"),
        (["--daemon", "a", "--idle-timeout", "-1"], "0 or more"),
        (["--shared", "Bad_Name"], "not a daemon name"),
    ],
)
def test_usage_errors(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    import memtomem._bridge as bridge
    import memtomem.server as server_mod
    import memtomem.server.daemon as daemon_mod
    from memtomem._server_entry import main

    # Nothing may start: an argument list that is wrongly accepted fails here
    # and does not run a server, a bridge or a daemon inside the test.
    monkeypatch.setattr(server_mod, "run_standalone", _must_not_run)
    monkeypatch.setattr(bridge, "run_bridge", _must_not_run)
    monkeypatch.setattr(daemon_mod, "main", _must_not_run)
    with pytest.raises(SystemExit) as exc:
        main(argv)

    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> dict:
    import memtomem._bridge as bridge
    import memtomem._server_entry as entry_mod
    import memtomem.server as server_mod

    seen: dict = {"bridge": [], "standalone": []}

    def run_bridge(name: str) -> str:
        seen["bridge"].append(name)
        return "a reason"

    monkeypatch.setattr(bridge, "run_bridge", run_bridge)
    monkeypatch.setattr(
        server_mod,
        "run_standalone",
        lambda args, fallback=None: seen["standalone"].append(fallback),
    )
    monkeypatch.setattr(entry_mod, "_is_direct_stdio_terminal", lambda: False)
    return seen


def test_without_shared_nothing_is_attempted(entry: dict) -> None:
    from memtomem._server_entry import main

    main([])

    assert entry == {"bridge": [], "standalone": [None]}


def test_a_session_that_does_not_join_runs_standalone_with_the_reason(entry: dict) -> None:
    from memtomem._server_entry import main

    main(["--shared", "team"])

    assert entry == {"bridge": ["team"], "standalone": [{"name": "team", "reason": "a reason"}]}


def test_a_terminal_reaches_the_standalone_guard_before_any_bridge_work(
    entry: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._server_entry as entry_mod

    monkeypatch.setattr(entry_mod, "_is_direct_stdio_terminal", lambda: True)
    entry_mod.main(["--shared", "team"])

    assert entry == {"bridge": [], "standalone": [None]}


def test_mm_serve_passes_shared_through(entry: dict) -> None:
    from click.testing import CliRunner

    from memtomem.cli import cli

    result = CliRunner().invoke(cli, ["serve", "--shared", "team"])

    assert result.exit_code == 0, result.output
    assert entry == {"bridge": ["team"], "standalone": [{"name": "team", "reason": "a reason"}]}


def test_daemon_dispatches_with_its_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem.server.daemon as daemon_mod
    from memtomem._server_entry import main

    seen: list = []
    monkeypatch.setattr(daemon_mod, "main", lambda argv: seen.append(argv) or 0)
    with pytest.raises(SystemExit) as exc:
        main(["--daemon", "team", "--idle-timeout", "30"])

    assert exc.value.code == 0
    assert seen == [["team", "--idle-timeout", "30.0"]]


def test_mem_status_names_a_session_that_did_not_join(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem.server.context as server_context
    from memtomem.server.tools.status_config import _shared_daemon_runtime

    assert _shared_daemon_runtime() == {}
    monkeypatch.setattr(server_context, "_shared_fallback", {"name": "t", "reason": "why"})

    assert _shared_daemon_runtime() == {"shared_fallback": {"name": "t", "reason": "why"}}


# ── the protocol, against a stand-in daemon ──────────────────────────────────


class _Responder:
    """A socket that answers every connection's first line with ``reply``."""

    def __init__(self, path: Path, reply: dict) -> None:
        self.reply = reply
        self.lines: list[dict] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    self.lines.append(json.loads(data.split(b"\n", 1)[0]))
                    conn.sendall(json.dumps(self.reply).encode() + b"\n")

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    import tempfile

    import memtomem._runtime_paths as runtime_paths

    root = Path(tempfile.mkdtemp(prefix="mmf-", dir="/tmp"))
    monkeypatch.setattr(runtime_paths, "ensure_runtime_dir", lambda: root)
    yield root
    import shutil

    shutil.rmtree(root, ignore_errors=True)


@posix_only
@pytest.mark.parametrize(
    "reply, reason",
    [
        (
            {"v": 1, "state": "error", "error": "unsupported bridge protocol version 2"},
            "refused the session: unsupported bridge protocol version 2",
        ),
        ({"v": 2, "state": "accepted"}, "did not compare the session"),
        (
            {"v": 2, "state": "mismatch", "differs": ["env (MEMTOMEM_X)", "home"]},
            "differs from this session: env (MEMTOMEM_X), home",
        ),
    ],
)
def test_a_daemon_that_does_not_admit_a_compared_session_is_a_fallback(
    reply: dict, reason: str, fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "_spawn", _must_not_run)
    responder = _Responder(fake_runtime / "daemon-t.sock", reply)
    try:
        with pytest.raises(bridge._Fallback, match=reason.replace("(", r"\(").replace(")", r"\)")):
            bridge._join("t", {"env": []})
    finally:
        responder.close()
    (line,) = responder.lines
    assert line["v"] == 2 and line["admission"] == {"env": []}
    assert line["cwd"] == os.getcwd()


@posix_only
def test_retry_keeps_trying_until_the_deadline_without_spawning_under_a_held_lock(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "SPAWN_DEADLINE_S", 0.5)
    monkeypatch.setattr(bridge, "_spawn", _must_not_run)
    responder = _Responder(fake_runtime / "daemon-t.sock", {"v": 2, "state": "retry"})
    try:
        with pytest.raises(bridge._Fallback, match="did not accept within 0.5 s"):
            bridge._join("t", {})
    finally:
        responder.close()
    assert len(responder.lines) > 1


@posix_only
def test_no_daemon_and_a_held_lifetime_lock_means_one_is_starting(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import portalocker

    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "SPAWN_DEADLINE_S", 0.3)
    monkeypatch.setattr(bridge, "_spawn", _must_not_run)
    with open(fake_runtime / "server-daemon-t.pid", "a+b") as held:
        portalocker.lock(held, portalocker.LOCK_EX | portalocker.LOCK_NB)
        with pytest.raises(bridge._Fallback, match="did not accept"):
            bridge._join("t", {})


@posix_only
def test_each_bridge_spawns_at_most_once(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    spawns: list[str] = []
    monkeypatch.setattr(bridge, "SPAWN_DEADLINE_S", 0.4)
    monkeypatch.setattr(bridge, "_spawn", spawns.append)
    with pytest.raises(bridge._Fallback, match="did not accept"):
        bridge._join("t", {})

    assert spawns == ["t"]


@posix_only
def test_a_launcher_that_cannot_be_started_is_a_fallback(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "private-sentinel-value")

    monkeypatch.setattr(bridge.subprocess, "Popen", refuse)

    with pytest.raises(bridge._Fallback) as failed:
        bridge._join("t", {})

    assert str(failed.value) == "the launcher of daemon t could not be started (PermissionError)"


@posix_only
def test_a_daemon_that_takes_the_connection_and_never_answers_does_not_outlast_the_deadline(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "SPAWN_DEADLINE_S", 0.5)
    monkeypatch.setattr(bridge, "_spawn", _must_not_run)
    assert bridge.REPLY_TIMEOUT_S >= 5  # one unbounded wait for a reply would be this long
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(fake_runtime / "daemon-t.sock"))
    server.listen(8)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                held.append(server.accept()[0])  # taken, never answered
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(bridge._Fallback, match="did not accept within 0.5 s"):
            bridge._join("t", {})
    finally:
        server.close()
        for conn in held:
            conn.close()

    assert time.monotonic() - started < 3


@posix_only
def test_each_connect_gets_what_is_left_of_the_deadline(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    budgets: list[float] = []

    def connect(path: Path, timeout: float) -> None:
        budgets.append(timeout)

    monkeypatch.setattr(bridge, "SPAWN_DEADLINE_S", 0.3)
    monkeypatch.setattr(bridge, "_connect", connect)
    monkeypatch.setattr(bridge, "_lock_held", lambda path: True)
    with pytest.raises(bridge._Fallback, match="did not accept"):
        bridge._join("t", {})

    assert len(budgets) > 1 and budgets == sorted(budgets, reverse=True)
    assert 0.3 >= budgets[0] > budgets[-1] >= bridge.CONNECT_INTERVAL_S


@posix_only
@pytest.mark.parametrize("not_taken", [TimeoutError, BlockingIOError])
def test_a_connect_that_is_not_taken_in_time_is_not_a_connection(
    not_taken: type[OSError], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    made: list[object] = []

    class Stuck:
        def __init__(self, *args: object) -> None:
            self.timeout: float | None = None
            self.closed = False
            made.append(self)

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, address: str) -> None:
            assert self.timeout == 0.25  # set before the connect, not after
            raise not_taken

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(bridge.socket, "socket", Stuck)

    assert bridge._connect(tmp_path / "daemon-t.sock", 0.25) is None
    (stuck,) = made
    assert stuck.closed  # type: ignore[attr-defined]


@posix_only
def test_the_lock_probe_reads_a_regular_file_only_and_never_waits(tmp_path: Path) -> None:
    import memtomem._bridge as bridge

    lock = tmp_path / "server-daemon-t.pid"
    assert bridge._lock_held(lock) is False  # no file: no daemon
    lock.write_text("1\n")
    assert bridge._lock_held(lock) is False  # nobody holds it
    link = tmp_path / "link.pid"
    link.symlink_to(lock)
    assert bridge._lock_held(link) is True  # not followed: unknown counts as held
    folder = tmp_path / "dir.pid"
    folder.mkdir()
    assert bridge._lock_held(folder) is True  # it can be opened and locked, and is no lock
    fifo = tmp_path / "fifo.pid"
    os.mkfifo(fifo)
    answers: list[bool] = []
    probe = threading.Thread(target=lambda: answers.append(bridge._lock_held(fifo)), daemon=True)
    probe.start()
    probe.join(5)
    in_time = list(answers)
    if not in_time:
        os.close(os.open(fifo, os.O_WRONLY))  # let the stuck open go before failing

    assert in_time == [True]


# ── nothing before the session costs the client its server ───────────────────


class _Boom(Exception):
    """An exception no step expects, carrying text a reason must not repeat."""

    def __init__(self) -> None:
        super().__init__("private-sentinel-value")


def _boom(*args: object, **kwargs: object) -> None:
    raise _Boom


_STEPS = [
    # (module, attribute, the reason run_bridge gives)
    ("memtomem._admission", "tool_surface_request", "joining daemon t failed (_Boom)"),
    ("memtomem._dotenv", "load_dotenv", "joining daemon t failed (_Boom)"),
    (
        "memtomem.config_signature",
        "build_fresh_config",
        "the configuration could not be read (_Boom)",
    ),
    ("memtomem.config", "relative_config_paths", "joining daemon t failed (_Boom)"),
    ("memtomem._store_identity", "resolve_shared_store", "joining daemon t failed (_Boom)"),
    ("memtomem._admission", "local_fields", "joining daemon t failed (_Boom)"),
    ("memtomem._runtime_paths", "ensure_runtime_dir", "joining daemon t failed (_Boom)"),
    ("memtomem._bridge", "_connect", "joining daemon t failed (_Boom)"),
    ("memtomem._bridge", "_lock_held", "joining daemon t failed (_Boom)"),
    ("memtomem._bridge", "_spawn", "joining daemon t failed (_Boom)"),
]


@posix_only
@pytest.mark.parametrize("module, attribute, reason", _STEPS, ids=[s[1] for s in _STEPS])
def test_a_step_that_fails_in_a_way_nobody_foresaw_is_still_a_fallback(
    module: str,
    attribute: str,
    reason: str,
    tmp_path: Path,
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    import memtomem._bridge as bridge

    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "store.db"))
    monkeypatch.setattr(bridge, "_pump", _must_not_run)
    before = dict(os.environ)
    monkeypatch.setattr(importlib.import_module(module), attribute, _boom)

    assert bridge.run_bridge("t") == reason
    assert dict(os.environ) == before


@posix_only
@pytest.mark.parametrize(
    "failure, reason",
    [
        (
            RuntimeError("private-sentinel-value"),
            "a configured path cannot be checked (RuntimeError)",
        ),
        (OSError(24, "private-sentinel-value"), "a configured path cannot be checked (OSError)"),
    ],
)
def test_a_configured_path_that_cannot_be_checked_is_a_fallback(
    failure: Exception, reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.expanduser`` raises ``RuntimeError`` for a ``~user`` nobody has."""
    import memtomem._bridge as bridge
    import memtomem.config as config

    def refuse(cfg: object) -> list[str]:
        raise failure

    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "store.db"))
    monkeypatch.setattr(config, "relative_config_paths", refuse)
    monkeypatch.setattr(bridge, "_join", _must_not_run)

    assert bridge.run_bridge("t") == reason


@posix_only
def test_a_handshake_that_fails_unforeseen_closes_its_connection(
    fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    ours, theirs = socket.socketpair()
    monkeypatch.setattr(bridge, "_connect", lambda path, timeout: ours)
    monkeypatch.setattr(bridge, "_handshake", _boom)
    try:
        with pytest.raises(_Boom):
            bridge._join("t", {})
        assert ours.fileno() == -1
    finally:
        theirs.close()


@posix_only
def test_an_accepted_session_is_handed_over_with_the_bytes_that_followed_the_reply(
    tmp_path: Path, fake_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(fake_runtime / "daemon-t.sock"))
    server.listen(1)
    reply = json.dumps({"v": 2, "state": "accepted", "compared": True}).encode()

    def accept() -> None:
        conn, _ = server.accept()
        conn.recv(65536)  # the preamble
        conn.sendall(reply + b"\nthe session's first bytes")  # in one write with the reply
        held.append(conn)

    held: list[socket.socket] = []
    handed: list[tuple] = []
    threading.Thread(target=accept, daemon=True).start()
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "store.db"))
    monkeypatch.setattr(bridge, "_spawn", _must_not_run)
    monkeypatch.setattr(bridge, "_pump", lambda *args: handed.append(args))
    try:
        bridge.run_bridge("t")
        ((name, sock, rest),) = handed
        assert name == "t" and rest == b"the session's first bytes"
        assert sock.fileno() != -1 and sock.gettimeout() is None  # open, and blocking again
        sock.close()
    finally:
        server.close()
        for conn in held:
            conn.close()


def _handshake_against(send: object, budget: float) -> tuple[object, float]:
    """``_handshake``'s answer and how long it took, with *send* playing the daemon."""
    import memtomem._bridge as bridge

    ours, theirs = socket.socketpair()
    threading.Thread(target=send, args=(theirs,), daemon=True).start()  # type: ignore[arg-type]
    started = time.monotonic()
    try:
        answer = bridge._handshake(ours, b"{}\n", started + budget)
        return answer, time.monotonic() - started
    finally:
        ours.close()
        theirs.close()


@posix_only
def test_a_deadline_already_passed_is_no_reply_and_nothing_is_sent() -> None:
    import memtomem._bridge as bridge

    ours, theirs = socket.socketpair()
    theirs.setblocking(False)
    try:
        assert bridge._handshake(ours, b"{}\n", time.monotonic() - 1) == (None, b"")
        with pytest.raises(BlockingIOError):
            theirs.recv(16)
    finally:
        ours.close()
        theirs.close()


@posix_only
def test_a_reply_that_trickles_in_does_not_outlast_the_deadline() -> None:
    reply = json.dumps({"v": 2, "state": "retry", "pad": "x" * 200}).encode() + b"\n"

    def trickle(conn: socket.socket) -> None:
        try:
            for i in range(len(reply)):
                conn.sendall(reply[i : i + 1])
                time.sleep(0.01)  # each byte is well inside any per-receive timeout
        except OSError:
            pass  # the handshake gave up and closed

    answer, took = _handshake_against(trickle, 0.3)

    assert answer == (None, b"")
    assert took < 1.5  # the whole reply would take more than two seconds


def test_a_reply_line_over_the_limit_is_no_reply() -> None:
    """Also when the newline arrives in the very chunk that passes the limit."""
    import memtomem._bridge as bridge

    # A reply that would be taken if it were shorter, one byte over the limit.
    pad = "x" * (bridge.REPLY_LIMIT + 1 - len(json.dumps({"v": 2, "state": "retry", "pad": ""})))
    line = json.dumps({"v": 2, "state": "retry", "pad": pad}).encode()
    assert len(line) == bridge.REPLY_LIMIT + 1

    class Scripted:
        def __init__(self) -> None:
            self.chunks = [line[: bridge.REPLY_LIMIT], line[bridge.REPLY_LIMIT :] + b"\nrest"]

        def settimeout(self, timeout: float | None) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, size: int) -> bytes:
            return self.chunks.pop(0)

    sock = Scripted()

    answer = bridge._handshake(sock, b"{}\n", time.monotonic() + 5)  # type: ignore[arg-type]

    assert answer == (None, b"") and sock.chunks == []


@posix_only
@pytest.mark.parametrize(
    "bad",
    # 30,000 levels fit in the reply limit; CPython 3.12 gives up on them with RecursionError.
    [b"not json", b"[1, 2]", b"[" * 30000 + b"]" * 30000],
    ids=["not-json", "not-an-object", "nested-past-the-parser"],
)
def test_a_reply_that_is_not_a_json_object_is_no_reply(bad: bytes) -> None:
    answer, _ = _handshake_against(lambda conn: conn.sendall(bad + b"\n"), 5.0)

    assert answer == (None, b"")


class _Helper:
    """A launcher process that ends as the test says."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        self.calls: list[object] = []

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(("wait", timeout))
        if self.code is None:
            raise subprocess.TimeoutExpired("launcher", timeout or 0)
        return self.code

    def kill(self) -> None:
        self.calls.append("kill")


@posix_only
def test_a_launcher_that_exits_nonzero_is_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: _Helper(3))

    with pytest.raises(bridge._Fallback, match=r"the launcher of daemon t failed \(exit 3\)"):
        bridge._spawn("t")


@posix_only
def test_a_launcher_that_never_ends_is_killed_and_waited_for_only_so_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memtomem._bridge as bridge

    helper = _Helper(None)  # it does not even die when killed
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *args, **kwargs: helper)

    with pytest.raises(bridge._Fallback, match="did not finish within 5 s"):
        bridge._spawn("t")

    assert helper.calls == [
        ("wait", bridge.HELPER_TIMEOUT_S),
        "kill",
        ("wait", bridge.HELPER_REAP_S),
    ]


def test_the_environment_is_put_back_key_by_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setenv("MM_T_CHANGED", "before")
    monkeypatch.setenv("MM_T_REMOVED", "kept")
    monkeypatch.delenv("MM_T_ADDED", raising=False)
    snapshot = dict(os.environ)
    os.environ["MM_T_CHANGED"] = "after"
    os.environ["MM_T_ADDED"] = "new"
    del os.environ["MM_T_REMOVED"]

    bridge._restore_environ(snapshot)

    assert dict(os.environ) == snapshot


# ── end to end ───────────────────────────────────────────────────────────────


class Bridge:
    """A ``memtomem-server --shared`` process speaking newline JSON-RPC on stdio."""

    def __init__(self, env_: DaemonEnv, name: str = "t", **env: str) -> None:
        self.err = env_.root / f"bridge-{time.monotonic_ns()}.err"
        with self.err.open("w") as err:
            self.proc = subprocess.Popen(
                [sys.executable, "-c", _BRIDGE, "--shared", name],
                env={**env_.env(), **env},
                cwd=str(env_.dir("client")),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
            )
        self._id = 0

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise EOFError(self.err.read_text()[-2000:])
            obj = json.loads(line)
            if obj.get("id") == self._id:
                return obj

    def initialize(self) -> Bridge:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        assert self.proc.stdin is not None
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()
        return self

    def shared_line(self) -> str:
        reply = self.request("tools/call", {"name": "mem_status", "arguments": {}})
        text = reply["result"]["content"][0]["text"]
        (line,) = [line for line in text.splitlines() if line.startswith("Shared daemon:")]
        return line.split(":", 1)[1].strip()

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()


def _daemon_pid(env_: DaemonEnv, name: str = "t") -> int | None:
    try:
        return int((env_.runtime / f"server-daemon-{name}.pid").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _ppid(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def benv(request: pytest.FixtureRequest) -> Iterator[DaemonEnv]:
    """``denv``, plus the end of every daemon a bridge started and the bridges themselves."""
    _denv: DaemonEnv = request.getfixturevalue("denv")
    bridges: list[Bridge] = []
    _denv.bridges = bridges  # type: ignore[attr-defined]
    try:
        yield _denv
    finally:
        for b in bridges:
            if b.proc.poll() is None:
                b.proc.kill()
                b.proc.wait()
        pid = _daemon_pid(_denv)
        if pid is not None and _alive(pid):
            os.kill(pid, signal.SIGTERM)
            _wait(lambda: not _alive(pid), timeout=20)


def _bridge(benv: DaemonEnv, **env: str) -> Bridge:
    b = Bridge(benv, **env)
    benv.bridges.append(b)  # type: ignore[attr-defined]
    return b


@posix_only
def test_two_bridges_start_and_share_one_daemon_that_is_nobodys_child(benv: DaemonEnv) -> None:
    first = _bridge(benv).initialize()
    second = _bridge(benv).initialize()

    pid = _daemon_pid(benv)
    assert pid is not None
    assert first.shared_line() == second.shared_line() == f"t (pid {pid}, 2 connection(s))"
    assert _ppid(pid) not in (first.proc.pid, second.proc.pid, os.getpid())
    # The launcher was reaped: no bridge is left with a child.
    children = subprocess.run(
        ["pgrep", "-P", str(first.proc.pid)], capture_output=True, text=True
    ).stdout.split()
    assert children == []
    log = benv.runtime / "daemon-t.log"
    assert log.exists() and (log.stat().st_mode & 0o777) == 0o600


@posix_only
def test_a_client_that_closes_stdin_ends_its_bridge_with_zero(benv: DaemonEnv) -> None:
    first = _bridge(benv).initialize()
    second = _bridge(benv).initialize()
    pid = _daemon_pid(benv)

    first.close_stdin()

    assert first.proc.wait(timeout=20) == 0
    assert _wait(lambda: second.shared_line() == f"t (pid {pid}, 1 connection(s))")


@posix_only
def test_a_daemon_that_goes_away_ends_its_bridges_with_one(benv: DaemonEnv) -> None:
    first = _bridge(benv).initialize()
    second = _bridge(benv).initialize()
    pid = _daemon_pid(benv)
    assert pid is not None

    os.kill(pid, signal.SIGTERM)

    assert first.proc.wait(timeout=30) == 1
    assert second.proc.wait(timeout=30) == 1


@posix_only
def test_sigterm_ends_a_bridge_with_zero_and_unregisters_it(benv: DaemonEnv) -> None:
    from memtomem._instance_registry import (
        bridge_name_digest,
        snapshot_all_instances,
        snapshot_bridges,
    )

    bridge = _bridge(benv).initialize()
    bridge.shared_line()
    (entry,) = snapshot_bridges().bridges
    assert entry.pid == bridge.proc.pid and entry.digest == bridge_name_digest("t")
    # Not a server: no gate that counts servers sees it.
    registry = snapshot_all_instances()
    assert bridge.proc.pid not in {i.pid for i in (*registry.instances, *registry.presence)}

    bridge.proc.send_signal(signal.SIGTERM)  # its stdin still open

    assert bridge.proc.wait(timeout=20) == 0
    assert snapshot_bridges().bridges == ()
    # Removed, not merely unlocked: no residue is left for a sweep to collect.
    assert not entry.path.exists()


@posix_only
def test_doctor_counts_the_bridges_of_a_daemon(
    benv: DaemonEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_server_daemon import _mm

    bridge = _bridge(benv).initialize()
    bridge.shared_line()
    pid = _daemon_pid(benv)

    payload = json.loads(_mm(benv, monkeypatch, "doctor", "--json").stdout)
    (check,) = [c for c in payload["checks"] if c["name"] == "shared-daemons"]

    assert check["message"] == f"1 shared daemon(s) running: t (pid {pid}, 1 bridge(s))"
    assert check["data"]["bridges_without_daemon"] == 0


@posix_only
def test_a_session_that_differs_runs_standalone_and_says_why(benv: DaemonEnv) -> None:
    secret = "value-that-must-not-travel"
    daemon = benv.start("t")
    daemon_pid = daemon.pid

    bridge = _bridge(benv, MEMTOMEM_TOOL_MODE="standard", MEMTOMEM_EXTRA=secret).initialize()
    line = bridge.shared_line()

    assert line == (
        "t not joined (daemon t differs from this session: "
        "env (MEMTOMEM_EXTRA, MEMTOMEM_TOOL_MODE), tools)"
    )
    assert secret not in line and secret not in bridge.err.read_text()
    # Nothing was admitted: the daemon's first session is still this one.
    client = benv.connect().initialize()
    assert "t (pid %d, 1 connection(s))" % daemon_pid in client.tool("mem_status")
    assert daemon.poll() is None


@posix_only
def test_a_joined_session_uses_the_daemons_live_configuration(benv: DaemonEnv) -> None:
    """A name shares live configuration (design §5.5): equal HOME names the file only."""
    first = _bridge(benv).initialize()
    first.request("tools/call", {"name": "mem_status", "arguments": {}})
    benv.write_config(search={"default_top_k": 7})

    second = _bridge(benv).initialize()

    assert second.shared_line().startswith("t (pid ")
    # A fresh standalone server would read 7 here; the daemon keeps what it has.
    assert _top_k(second) == _top_k(first) == "10"


def _top_k(bridge: Bridge) -> str:
    reply = bridge.request("tools/call", {"name": "mem_status", "arguments": {}})
    text = reply["result"]["content"][0]["text"]
    (line,) = [line for line in text.splitlines() if line.startswith("Top-K:")]
    return line.split(":", 1)[1].strip()


@posix_only
def test_a_store_file_replaced_under_a_daemon_does_not_turn_sessions_away(
    benv: DaemonEnv,
) -> None:
    """The path is what is compared; the daemon keeps the file it opened (documented)."""
    import sqlite3

    benv.start("t")
    first = benv.connect().initialize()
    first.tool("mem_status")  # first initialization creates the store
    benv.store.rename(benv.home / "aside.db")
    sqlite3.connect(benv.store).close()  # another file at the same path

    late = _bridge(benv).initialize()

    assert late.shared_line().startswith("t (pid ")


@posix_only
def test_a_configuration_error_reason_names_fields_never_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(tmp_path / "store.db"))
    monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "private-sentinel-value")
    monkeypatch.setattr(bridge, "_join", _must_not_run)

    reason = bridge.run_bridge("t")

    assert reason.startswith("the configuration is not valid: ")
    assert "default_top_k" in reason
    assert "private-sentinel-value" not in reason


# ── the session, without a process ───────────────────────────────────────────


def _events(*names: str):
    import queue

    events: queue.Queue[str] = queue.Queue()
    for name in names:
        events.put(name)
    return events


@pytest.mark.parametrize(
    "order, code",
    [
        # The client left, all it sent was passed on, the daemon answered and closed.
        (("stdin-eof", "sent", "socket-eof", "flushed"), 0),
        (("stdin-eof", "socket-eof", "flushed", "sent"), 0),
        # A client that is gone has closed its stdout too.
        (("stdin-eof", "sent", "stdout-error", "socket-eof"), 0),
        (("socket-eof", "stdin-eof", "flushed"), 1),  # the daemon left first
        (("socket-eof", "flushed"), 1),
        # Bytes lost on the way to or from the daemon: 1, whoever left first, and
        # only once what the daemon had sent was written.
        (("stdin-eof", "error", "socket-eof", "flushed"), 1),
        (("error", "stdin-eof", "socket-eof", "flushed"), 1),
        (("error", "flushed"), 1),
        (("stdin-eof", "error", "stdout-error"), 1),
        # The write to the daemon was still under way when the daemon's answers
        # were all written, and then it failed.
        (("stdin-eof", "socket-eof", "flushed", "error"), 1),
        (("overflow",), 1),
        (("stdin-eof", "overflow"), 1),  # bytes were dropped, whoever left first
    ],
)
def test_who_ended_the_session_and_whether_bytes_were_lost_decide_the_exit_code(
    order: tuple[str, ...], code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "_terminated", False)
    events = _events(*order)
    started = time.monotonic()

    assert bridge._wait_for_end(events) == code
    assert events.empty()  # nothing was decided before the last of them
    assert time.monotonic() - started < bridge.CONNECTION_DRAIN_S / 2  # nor by the deadline


def _histories() -> list[tuple[str, ...]]:
    """Every order in which the four threads of a session can report, one outcome each.

    A writer reports success only after the reader it follows has reported;
    a failure can come at any time.
    """
    import itertools

    found: set[tuple[str, ...]] = set()
    for from_client, to_daemon, from_daemon, to_client in itertools.product(
        ("stdin-eof", "error"),
        ("sent", "error"),
        ("socket-eof", "error"),
        ("flushed", "stdout-error"),
    ):
        for order in itertools.permutations(range(4)):
            position = {thread: at for at, thread in enumerate(order)}
            if to_daemon == "sent" and position[1] < position[0]:
                continue
            if to_client == "flushed" and position[3] < position[2]:
                continue
            outcome = (from_client, to_daemon, from_daemon, to_client)
            found.add(tuple(outcome[thread] for thread in order))
    return sorted(found)


_HISTORIES = _histories()


def test_the_histories_are_all_of_them() -> None:
    # 216 orders of 16 outcomes; the three threads that can fail all say ``error``.
    assert len(_HISTORIES) == 147
    assert sum("error" not in history for history in _HISTORIES) == 18
    assert ("stdin-eof", "socket-eof", "flushed", "error") in _HISTORIES
    assert ("stdin-eof", "sent", "socket-eof", "flushed") in _HISTORIES


@pytest.mark.parametrize("paused", [False, True], ids=["on-time", "read-after-the-deadline"])
@pytest.mark.parametrize("history", _HISTORIES, ids="/".join)
def test_no_order_of_reports_ends_a_session_with_zero_when_bytes_were_lost(
    history: tuple[str, ...], paused: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 0 is what the reports say taken together; only the two EOFs have an order.

    *paused*: the process stood still past the drain time after the first
    report. What was reported by then still counts.
    """
    import types

    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "_terminated", False)
    if paused:
        clock = iter([0.0])
        late = bridge.CONNECTION_DRAIN_S + 1
        monkeypatch.setattr(
            bridge, "time", types.SimpleNamespace(monotonic=lambda: next(clock, late))
        )
    events = _events(*history)
    clean = "error" not in history and history.index("stdin-eof") < history.index("socket-eof")
    started = time.monotonic()

    code = bridge._wait_for_end(events)

    assert code == (0 if clean else 1)
    if code == 0:
        assert events.empty()  # zero took every report
    # All four reported: the reports decide, not the deadline.
    assert time.monotonic() - started < bridge.CONNECTION_DRAIN_S / 2


@pytest.mark.parametrize(
    "seen, code",
    [
        (("stdin-eof",), 0),
        (("socket-eof",), 1),
        (("error",), 1),
        (("stdin-eof", "error"), 1),
        # The client closed its stdout and nothing says it closed stdin.
        (("stdout-error",), 1),
        # The client left; the write to the daemon is stuck, or the daemon stays.
        (("stdin-eof", "socket-eof", "flushed"), 0),
        (("stdin-eof", "sent", "stdout-error"), 0),
    ],
)
def test_the_wait_for_a_report_that_does_not_come_is_bounded(
    seen: tuple[str, ...], code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first report the session goes on, but only for the drain time."""
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "_terminated", False)
    monkeypatch.setattr(bridge, "CONNECTION_DRAIN_S", 0.2)
    started = time.monotonic()

    assert bridge._wait_for_end(_events(*seen)) == code
    assert 0.2 <= time.monotonic() - started < 5


def test_a_buffer_holds_no_more_than_its_limit() -> None:
    import memtomem._bridge as bridge

    buffer = bridge._Buffer(10)
    assert buffer.put(b"123456") and not buffer.put(b"12345")
    assert buffer.put(b"1234")  # exactly the limit
    assert buffer.get() == b"123456"
    assert not buffer.put(b"1")  # taken off the queue, not yet written
    buffer.written(6)
    assert buffer.put(b"123456") and not buffer.put(b"1")


def test_the_guide_states_the_limit_a_session_has() -> None:
    import memtomem._bridge as bridge

    guide = Path(__file__).parents[3] / "docs/guides/reference/data-config-cli.md"

    assert bridge.BUFFER_LIMIT == 32 * 1024 * 1024
    text = guide.read_text(encoding="utf-8")
    assert "more than 32 MiB is waiting in either direction" in " ".join(text.split())


def test_an_end_of_input_is_reported_before_its_buffer_is_closed() -> None:
    """Closing the client's buffer shuts the write half; the daemon's EOF answers that."""
    import memtomem._bridge as bridge

    events = _events()
    seen: list[list[str]] = []

    class Recording(bridge._Buffer):
        def close(self) -> None:
            seen.append(list(events.queue))
            super().close()

    bridge._fill(lambda: b"", Recording(10), events, "stdin-eof")

    assert seen == [["stdin-eof"]]


def test_a_reader_ahead_of_the_limit_stops_and_says_overflow() -> None:
    import memtomem._bridge as bridge

    chunks = [b"x" * 6, b"y" * 6, b"never read"]
    events, buffer = _events(), bridge._Buffer(10)

    bridge._fill(lambda: chunks.pop(0), buffer, events, "socket-eof")

    assert list(events.queue) == ["overflow"]
    assert chunks == [b"never read"]
    assert (buffer.get(), buffer.get()) == (b"x" * 6, None)


def test_a_read_error_is_reported_and_closes_the_buffer() -> None:
    import memtomem._bridge as bridge

    def read() -> bytes:
        raise OSError("gone")

    events, buffer = _events(), bridge._Buffer(10)

    bridge._fill(read, buffer, events, "socket-eof")

    assert list(events.queue) == ["error"] and buffer.get() is None


def test_what_was_written_no_longer_counts_and_the_end_comes_after_it() -> None:
    import memtomem._bridge as bridge

    buffer, events = bridge._Buffer(10), _events()
    assert buffer.put(b"123456")
    buffer.close()
    written: list[bytes] = []
    at_finish: list[list[bytes]] = []

    bridge._drain(buffer, written.append, events, lambda: at_finish.append(list(written)))

    assert at_finish == [[b"123456"]] and events.empty()
    assert buffer.put(b"1234567890")  # the whole limit is free again


@pytest.mark.parametrize("named, reported", [((), "error"), (("stdout-error",), "stdout-error")])
def test_a_write_error_is_reported_under_its_name_and_nothing_is_finished(
    named: tuple[str, ...], reported: str
) -> None:
    import memtomem._bridge as bridge

    def write(data: bytes) -> None:
        raise BrokenPipeError

    buffer, events = bridge._Buffer(10), _events()
    buffer.put(b"1")
    buffer.close()

    bridge._drain(buffer, write, events, _must_not_run, *named)

    assert list(events.queue) == [reported]


class _Session:
    """``_start_session`` over a socket pair and two pipes: the test is client and daemon."""

    def __init__(self, rest: bytes = b"", sndbuf: int | None = None, ours: object = None) -> None:
        """With *ours*, a stand-in for the bridge's end, there is no daemon end to play."""
        import memtomem._bridge as bridge

        if ours is None:
            ours, self.daemon = socket.socketpair()
            if sndbuf is not None:
                ours.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
            self.daemon.settimeout(10)
            self._socks = [self.daemon, ours]
        else:
            self._socks = [ours]
        stdin_r, self.stdin = os.pipe()
        self.stdout, stdout_w = os.pipe()
        self._fds = [stdin_r, self.stdin, self.stdout, stdout_w]
        self.events = bridge._start_session(ours, rest, stdin_r, stdout_w)

    def close_stdin(self) -> None:
        os.close(self.stdin)
        self._fds.remove(self.stdin)

    def close_stdout(self) -> None:
        os.close(self.stdout)
        self._fds.remove(self.stdout)

    def read_stdout(self, count: int, timeout: float = 10.0) -> bytes:
        """Up to *count* bytes of what reached the client; fewer if they do not come in time."""
        import select

        data, deadline = b"", time.monotonic() + timeout
        while len(data) < count:
            ready, _, _ = select.select(
                [self.stdout], [], [], max(0.0, deadline - time.monotonic())
            )
            if not ready:
                break
            data += os.read(self.stdout, count - len(data))
        return data

    def write_stdin(self, data: bytes, *, close: bool = False) -> None:
        """From a thread: a client's write blocks for as long as the bridge does not read."""

        def write() -> None:
            try:
                os.write(self.stdin, data)
                if close:
                    self.close_stdin()
            except OSError:
                pass  # the test closed the pipe

        threading.Thread(target=write, daemon=True).start()

    def exit_code(self, timeout: float = 10.0) -> int | None:
        """``_wait_for_end``'s answer, or ``None`` if it has none within *timeout*."""
        import memtomem._bridge as bridge

        codes: list[int] = []
        waiter = threading.Thread(
            target=lambda: codes.append(bridge._wait_for_end(self.events)), daemon=True
        )
        waiter.start()
        waiter.join(timeout)
        return codes[0] if codes else None

    def close(self) -> None:
        for sock in self._socks:
            sock.close()
        for fd in list(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass  # the writer thread closed stdin meanwhile


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Session]]:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "_terminated", False)
    made: list[_Session] = []
    yield made
    for one in made:
        one.close()


@posix_only
def test_a_session_passes_bytes_both_ways_and_the_clients_end_on(session: list) -> None:
    s = _Session(rest=b"first,")
    session.append(s)

    os.write(s.stdin, b"request")
    assert s.daemon.recv(64) == b"request"
    s.daemon.sendall(b"second")
    assert s.read_stdout(12) == b"first,second"  # what followed the reply line comes first

    s.close_stdin()
    assert s.daemon.recv(64) == b""  # the write half was shut: the daemon sees the end
    s.daemon.close()

    assert s.exit_code() == 0


@posix_only
def test_a_session_whose_daemon_closes_first_ends_with_one(session: list) -> None:
    s = _Session()
    session.append(s)

    s.daemon.sendall(b"last")
    s.daemon.close()

    assert s.read_stdout(4) == b"last"
    assert s.exit_code() == 1


@posix_only
def test_a_client_that_left_and_closed_its_stdout_too_is_a_normal_end(session: list) -> None:
    s = _Session()
    session.append(s)

    s.close_stdin()
    assert s.daemon.recv(64) == b""  # the client's end is recorded and passed on
    s.close_stdout()
    s.daemon.sendall(b"an answer nobody reads")  # only the write to the client fails
    s.daemon.close()

    assert s.exit_code() == 0


@posix_only
def test_input_that_cannot_reach_the_daemon_is_not_a_normal_end(
    session: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client left after writing, and the write to the daemon failed: its bytes are lost."""
    import memtomem._bridge as bridge

    released, fail = threading.Event(), threading.Event()

    class Deaf:
        """A connection whose daemon takes nothing more and says nothing, not even EOF."""

        def recv(self, size: int) -> bytes:
            released.wait(30)
            return b""

        def sendall(self, data: bytes) -> None:
            fail.wait(30)  # set once the client's EOF is recorded
            raise BrokenPipeError

        def shutdown(self, how: int) -> None:
            pass

        def close(self) -> None:
            released.set()
            fail.set()

    def reported(*events: str) -> bool:
        """Whether the session's reports so far are *events*, in order, within 10 s."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with s.events.mutex:
                if tuple(s.events.queue) == events:
                    return True
            time.sleep(0.01)
        return False

    # Nothing answers, so the wait ends at the drain deadline. Both reports are
    # queued before the wait starts, and a report already queued is read before
    # the deadline counts: how fast the threads run cannot move the failure past it.
    monkeypatch.setattr(bridge, "CONNECTION_DRAIN_S", 0.3)
    s = _Session(ours=Deaf())
    session.append(s)

    s.write_stdin(b"a request", close=True)
    assert reported("stdin-eof")
    fail.set()
    assert reported("stdin-eof", "error")

    assert s.exit_code() == 1


@posix_only
def test_nothing_is_decided_while_the_write_to_the_daemon_is_under_way(
    session: list,
) -> None:
    """The daemon's answers are all written and the client's last write then fails: not a 0."""
    import memtomem._bridge as bridge

    daemon_closes, write_fails = threading.Event(), threading.Event()

    class Closing:
        """A daemon that closes while the bridge is still writing to it."""

        def recv(self, size: int) -> bytes:
            daemon_closes.wait(30)
            return b""

        def sendall(self, data: bytes) -> None:
            write_fails.wait(30)
            raise BrokenPipeError

        def shutdown(self, how: int) -> None:
            pass

        def close(self) -> None:
            daemon_closes.set()
            write_fails.set()

    s = _Session(ours=Closing())
    session.append(s)
    s.write_stdin(b"a request", close=True)
    assert _wait(lambda: "stdin-eof" in s.events.queue)
    daemon_closes.set()
    assert _wait(lambda: "flushed" in s.events.queue)
    assert list(s.events.queue) == ["stdin-eof", "socket-eof", "flushed"]

    codes: list[int] = []
    waiter = threading.Thread(
        target=lambda: codes.append(bridge._wait_for_end(s.events)), daemon=True
    )
    waiter.start()
    waiter.join(0.5)
    assert codes == []  # the writer to the daemon has not reported

    write_fails.set()
    waiter.join(10)
    assert codes == [1]


@posix_only
def test_a_client_that_leaves_while_the_daemon_is_not_reading_still_ends_the_session(
    session: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write to the daemon is stuck; stdin's EOF is seen all the same, and the wait is bounded."""
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "CONNECTION_DRAIN_S", 0.3)
    s = _Session(sndbuf=1024)
    session.append(s)

    # Far more than the socket takes, and the daemon reads none of it.
    s.write_stdin(b"x" * (1024 * 1024), close=True)

    assert s.exit_code(timeout=10) == 0


@posix_only
def test_a_daemon_that_stops_reading_ends_the_session_at_the_limit(
    session: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem._bridge as bridge

    monkeypatch.setattr(bridge, "BUFFER_LIMIT", 256 * 1024)
    s = _Session(sndbuf=1024)
    session.append(s)

    s.write_stdin(b"x" * (8 * 1024 * 1024))

    assert s.exit_code(timeout=10) == 1  # stdin is still open: nothing but the limit ended it


_UNREAD_STDOUT_BRIDGE = (
    "import sys; import memtomem._bridge as b; b.CONNECTION_DRAIN_S = 1.0; "
    "from memtomem._server_entry import main; main(sys.argv[1:])"
)


@posix_only
def test_a_daemon_that_goes_away_ends_a_bridge_whose_client_stopped_reading(
    benv: DaemonEnv,
) -> None:
    """stdout full and stdin open: the daemon's EOF is still seen, and the exit is bounded."""
    flood = b"x" * (4 * 1024 * 1024)

    def serve(conn: socket.socket) -> None:
        conn.sendall(json.dumps({"v": 2, "state": "accepted", "compared": True}).encode() + b"\n")
        try:
            conn.sendall(flood)  # more than any pipe holds; the bridge must take it
        finally:
            conn.close()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(benv.socket_path("t")))
    server.listen(1)

    def accept() -> None:
        conn, _ = server.accept()
        conn.recv(65536)  # the preamble
        serve(conn)

    threading.Thread(target=accept, daemon=True).start()
    proc = subprocess.Popen(
        [sys.executable, "-c", _UNREAD_STDOUT_BRIDGE, "--shared", "t"],
        env=benv.env(),
        cwd=str(benv.dir("client")),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,  # never read
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.wait(timeout=30) == 1
    finally:
        server.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


_SMALL_LIMIT_BRIDGE = (
    "import sys; import memtomem._bridge as b; b.BUFFER_LIMIT = 1024 * 1024; "
    "from memtomem._server_entry import main; main(sys.argv[1:])"
)


@posix_only
def test_a_client_that_stops_reading_ends_its_session_at_the_limit(benv: DaemonEnv) -> None:
    """stdin open, the daemon still there and still sending: the bridge does not grow."""
    flood = b"x" * (8 * 1024 * 1024)
    done = threading.Event()
    refused: list[bool] = []

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(benv.socket_path("t")))
    server.listen(1)

    def accept() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)  # the preamble
            reply = {"v": 2, "state": "accepted", "compared": True}
            conn.sendall(json.dumps(reply).encode() + b"\n")
            try:
                conn.sendall(flood)
            except OSError:
                refused.append(True)  # the bridge left with most of it unsent
            done.wait(60)  # the daemon never closes first

    threading.Thread(target=accept, daemon=True).start()
    proc = subprocess.Popen(
        [sys.executable, "-c", _SMALL_LIMIT_BRIDGE, "--shared", "t"],
        env=benv.env(),
        cwd=str(benv.dir("client")),
        stdin=subprocess.PIPE,  # stays open
        stdout=subprocess.PIPE,  # never read
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.wait(timeout=20) == 1
        assert _wait(lambda: refused == [True])
    finally:
        done.set()
        server.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@posix_only
def test_an_older_daemon_is_not_joined(benv: DaemonEnv) -> None:
    """A version-1 daemon answers a version-2 preamble with ``error``."""
    responder = _Responder(
        benv.socket_path("t"),
        {"v": 1, "state": "error", "error": "unsupported bridge protocol version 2"},
    )
    try:
        line = _bridge(benv).initialize().shared_line()
    finally:
        responder.close()

    assert line == (
        "t not joined (daemon t refused the session: unsupported bridge protocol version 2)"
    )


@posix_only
def test_client_helper_matches_the_daemon(benv: DaemonEnv) -> None:
    """The suite's raw client is admitted, so its fields are a bridge's fields."""
    benv.start("t")
    client = Client(benv.socket_path("t"), benv.dir("client"), benv.admission())

    assert client.state == "accepted" and client.reply["compared"] is True


# ── uninstall ────────────────────────────────────────────────────────────────


@posix_only
def test_uninstall_removes_a_dead_daemons_log_and_keeps_a_live_ones(tmp_path: Path) -> None:
    import portalocker

    from memtomem.cli.uninstall_cmd import _sweep_stale_daemon_files

    (tmp_path / "daemon-dead.log").write_text("x")
    (tmp_path / "daemon-live.log").write_text("x")
    with open(tmp_path / "server-daemon-live.pid", "a+b") as held:
        portalocker.lock(held, portalocker.LOCK_EX | portalocker.LOCK_NB)
        _sweep_stale_daemon_files([tmp_path])

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "daemon-live.log",
            "server-daemon-live.pid",
        ]


@posix_only
def test_a_stale_bridge_entry_is_swept_and_a_live_one_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import memtomem._instance_registry as registry

    monkeypatch.setattr(registry, "_STALE_GRACE_S", -1.0)
    live = registry.register_bridge("t")
    assert live is not None
    stale = live.path.with_name(live.path.name.rsplit("-", 1)[0] + "-00000000.lock")
    stale.write_bytes(b"")
    try:
        registry.sweep_stale_presence()

        assert sorted(p.name for p in registry.bridges_dir().iterdir()) == [live.path.name]
    finally:
        live.cleanup()
