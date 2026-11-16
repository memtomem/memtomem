"""The shared daemon (#3381 pull request 2): one engine, one session per connection.

Each daemon runs as a real subprocess through ``tests/_daemon_harness.py``,
with an isolated ``HOME``, a short runtime directory (a unix socket path is
limited to about 104 bytes, and ``tmp_path`` is longer on macOS), its store and
memory directory set in ``config.json`` and BM25-only search.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from _subprocess_env import subprocess_env

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the shared daemon is POSIX only")

HARNESS = Path(__file__).with_name("_daemon_harness.py")
START_TIMEOUT_S = 30.0


# ── client ───────────────────────────────────────────────────────────────────


class Client:
    """One connection: the preamble, then newline JSON-RPC."""

    def __init__(self, sock_path: Path, cwd: Path, *, timeout: float = 30.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(sock_path))
        self.file = self.sock.makefile("rwb", buffering=0)
        self.file.write(json.dumps({"v": 1, "cwd": str(cwd)}).encode() + b"\n")
        line = self.file.readline()
        self.reply = json.loads(line) if line else None
        self._next_id = 1

    @property
    def state(self) -> str | None:
        return self.reply["state"] if self.reply else None

    def send(self, message: dict) -> None:
        self.file.write(json.dumps(message).encode() + b"\n")

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        ident = self._next_id
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            line = self.file.readline()
            if not line:
                raise EOFError("connection closed")
            obj = json.loads(line)
            if obj.get("id") == ident:
                return obj

    def initialize(self) -> Client:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def tool(self, name: str, arguments: dict | None = None) -> str:
        reply = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        content = reply.get("result", {}).get("content") or [{}]
        return str(content[0].get("text", reply))

    def at_eof(self, timeout: float = 15.0) -> bool:
        self.sock.settimeout(timeout)
        try:
            return self.file.readline() == b""
        except (TimeoutError, OSError):
            return False

    def close(self) -> None:
        for closer in (self.file.close, self.sock.close):
            try:
                closer()
            except OSError:
                pass


# ── environment and process ──────────────────────────────────────────────────


@dataclass
class DaemonEnv:
    root: Path
    runtime: Path
    home: Path
    procs: list[subprocess.Popen] = field(default_factory=list)

    @property
    def store(self) -> Path:
        return self.home / "store.db"

    @property
    def mem(self) -> Path:
        return self.home / "mem"

    @property
    def config_path(self) -> Path:
        return self.home / ".memtomem" / "config.json"

    def write_config(self, **sections: dict) -> None:
        config = {
            "storage": {"sqlite_path": str(self.store)},
            "indexing": {"memory_dirs": [str(self.mem)]},
            "embedding": {"provider": "none"},
        }
        for key, value in sections.items():
            config.setdefault(key, {}).update(value)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config))

    def env(self, **harness: object) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith("MEMTOMEM_")}
        return subprocess_env(
            base,
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            _MEMTOMEM_TEST_RUNTIME_DIR=str(self.runtime),
            PYTHON_DOTENV_DISABLED="1",
            COLUMNS="1000",  # keep log lines whole for the assertions
            MMD_HARNESS=json.dumps(harness),
        )

    def socket_path(self, name: str) -> Path:
        return self.runtime / f"daemon-{name}.sock"

    def dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def spawn(self, name: str, *args: str, **harness: object) -> subprocess.Popen:
        # Output goes to a file: an unread pipe fills and blocks the daemon's logging.
        log = self.root / f"daemon-{len(self.procs)}.log"
        with log.open("w") as out:
            proc = subprocess.Popen(
                [sys.executable, str(HARNESS), name, *args],
                env=self.env(**harness),
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        proc.log = log  # type: ignore[attr-defined]
        self.procs.append(proc)
        return proc

    def start(self, name: str = "t", *args: str, **harness: object) -> subprocess.Popen:
        proc = self.spawn(name, *args, **harness)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.socket_path(name).exists():
                return proc
            if proc.poll() is not None:
                raise AssertionError(f"daemon exited early: {_log(proc)[-2000:]}")
            time.sleep(0.05)
        raise AssertionError("daemon did not bind its socket")

    def connect(self, name: str = "t", cwd: Path | None = None) -> Client:
        """An admitted connection; ``retry`` (a daemon still starting) is retried."""
        deadline = time.monotonic() + START_TIMEOUT_S
        while True:
            client = Client(self.socket_path(name), cwd or self.dir("client"))
            if client.state != "retry" or time.monotonic() > deadline:
                return client
            client.close()
            time.sleep(0.05)


def _wait(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _log(proc: subprocess.Popen) -> str:
    return proc.log.read_text()  # type: ignore[attr-defined]


def _status(path: Path) -> dict:
    return json.loads(path.read_text())


def _fresh_status(path: Path) -> dict:
    """A snapshot the daemon took after this call, not one already on disk."""
    seen = _status(path)["seq"]
    assert _wait(lambda: _status(path)["seq"] > seen)
    return _status(path)


def _exit_code(proc: subprocess.Popen, timeout: float = 20.0) -> int:
    return proc.wait(timeout=timeout)


@pytest.fixture
def denv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DaemonEnv]:
    runtime = Path(tempfile.mkdtemp(prefix="mmd-", dir="/tmp"))
    home = tmp_path / "home"
    home.mkdir()
    # The probes in this process must look at the daemon's runtime directory:
    # the variable for the liveness probes, and the two resolvers the suite's
    # registry fixture patches for the barrier and registry probes.
    import memtomem._instance_registry as registry

    monkeypatch.setenv("_MEMTOMEM_TEST_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(registry, "runtime_dir", lambda: runtime)
    monkeypatch.setattr(registry, "ensure_runtime_dir", lambda: runtime)
    env = DaemonEnv(root=tmp_path, runtime=runtime, home=home)
    env.mem.mkdir()
    env.write_config()
    try:
        yield env
    finally:
        for proc in env.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        shutil.rmtree(runtime, ignore_errors=True)


def _barrier_refuses() -> bool:
    from memtomem._instance_registry import BarrierTimeout, acquire_uninstall_lifecycle_barrier

    try:
        held = acquire_uninstall_lifecycle_barrier(timeout_s=0.2)
    except BarrierTimeout:
        return True
    held.release()
    return False


def _store_live(store: Path) -> bool:
    from memtomem.cli._liveness import check_server_liveness

    return check_server_liveness(store).alive


# ── sharing and per-connection context ───────────────────────────────────────


def test_two_connections_share_one_engine(denv: DaemonEnv) -> None:
    status = denv.root / "status.json"
    denv.start(status_file=str(status))
    a = denv.connect().initialize()
    b = denv.connect().initialize()
    a.tool("mem_search", {"query": "anything"})  # initializes the owner
    b.tool("mem_search", {"query": "anything"})

    def settled() -> bool:
        if not status.exists():
            return False
        seen = _status(status)
        return seen["admitted"] == 2 and seen["watcher_id"] is not None

    assert _wait(settled), _status(status)
    seen = _status(status)
    assert seen["components_id"] is not None
    # The daemon's own reference plus one per connection.
    assert seen["references"] == 3


def test_each_connection_has_its_own_cwd_and_session(denv: DaemonEnv) -> None:
    denv.start()
    project_a, project_b = denv.dir("project-a"), denv.dir("project-b")
    a = denv.connect(cwd=project_a).initialize()
    b = denv.connect(cwd=project_b).initialize()
    assert f"CWD:             {project_a.resolve()}" in a.tool("mem_status")
    assert f"CWD:             {project_b.resolve()}" in b.tool("mem_status")

    a.tool("mem_do", {"action": "session_start", "params": {"agent_id": "alpha"}})
    b.tool("mem_do", {"action": "session_start", "params": {"agent_id": "beta"}})
    added_a = a.tool("mem_add", {"content": "alpha writes this"})
    added_b = b.tool("mem_add", {"content": "beta writes this"})
    assert "agent-runtime:alpha" in added_a, added_a
    assert "agent-runtime:beta" in added_b, added_b


def test_resources_answer_inside_each_connections_project(denv: DaemonEnv) -> None:
    import sqlite3

    projects = {name: denv.dir(name) for name in ("project-a", "project-b")}
    tiers = {name: root / ".memtomem" / "memories.local" for name, root in projects.items()}
    for tier in tiers.values():
        tier.mkdir(parents=True)
    denv.write_config(
        indexing={
            "memory_dirs": [str(denv.mem)],
            "project_memory_dirs": [str(tier) for tier in tiers.values()],
        }
    )
    denv.start()
    a = denv.connect(cwd=projects["project-a"]).initialize()
    b = denv.connect(cwd=projects["project-b"]).initialize()
    added_a = a.tool("mem_add", {"content": "alpha", "tags": ["alpha"], "scope": "project_local"})
    added_b = b.tool("mem_add", {"content": "beta", "tags": ["beta"], "scope": "project_local"})
    # Each write lands in its own connection's project.
    assert str(tiers["project-a"].resolve()) in added_a, added_a
    assert str(tiers["project-b"].resolve()) in added_b, added_b

    def tags(client: Client) -> set[str]:
        reply = client.request("resources/read", {"uri": "memtomem://tags"})
        return {row["tag"] for row in json.loads(reply["result"]["contents"][0]["text"])}

    assert tags(a) == {"alpha"}
    assert tags(b) == {"beta"}

    with sqlite3.connect(f"file:{denv.store}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id, source_file FROM chunks").fetchall()
    chunk_a = next(cid for cid, source in rows if "project-a" in source)

    def chunk(client: Client) -> str:
        reply = client.request("resources/read", {"uri": f"memtomem://chunks/{chunk_a}"})
        result = reply.get("result")
        return result["contents"][0]["text"] if result else json.dumps(reply.get("error"))

    assert "alpha" in chunk(a)
    assert "alpha" not in chunk(b)


# ── locks and the barrier ────────────────────────────────────────────────────


def test_barrier_is_held_before_admission_and_across_a_rollback(denv: DaemonEnv) -> None:
    denv.write_config(warmup={"enabled": False})
    denv.start(fail_watcher_start_once=True)
    a = denv.connect().initialize()
    b = denv.connect().initialize()
    # Both admitted, neither initialized: the daemon's own hold refuses uninstall.
    assert _barrier_refuses()
    assert _store_live(denv.store)

    first = a.tool("mem_status")
    assert "injected watcher start failure" in first, first
    # The owner's rollback released its own hold; the daemon's is still there.
    assert _barrier_refuses()
    assert _store_live(denv.store)
    assert "memtomem Status" in b.tool("mem_status")


def test_a_second_daemon_under_the_same_name_opens_nothing(denv: DaemonEnv) -> None:
    denv.start()
    other_home = denv.dir("other-home")
    other_store = other_home / "other.db"
    other = DaemonEnv(root=denv.root, runtime=denv.runtime, home=other_home)
    (other_home / "mem").mkdir()
    other.write_config(storage={"sqlite_path": str(other_store)})
    proc = other.spawn("t")
    denv.procs.append(proc)  # the fixture's teardown kills it if the wait times out
    assert _exit_code(proc) == 2
    assert "already running under this name" in _log(proc)
    assert not other_store.exists()


def test_a_failure_after_the_lifetime_lock_removes_it(denv: DaemonEnv) -> None:
    # The barrier is taken after both pid locks; holding it exclusively here
    # makes the daemon fail at that step.
    from memtomem._instance_registry import acquire_uninstall_lifecycle_barrier

    held = acquire_uninstall_lifecycle_barrier(timeout_s=1)
    try:
        proc = denv.spawn("t")
        assert _exit_code(proc, timeout=60) == 1
    finally:
        held.release()
    assert not (denv.runtime / "server-daemon-t.pid").exists()


def test_store_pid_lock_is_shared_with_a_standalone_server_either_way(
    denv: DaemonEnv,
) -> None:
    from memtomem._runtime_paths import server_pid_path

    pid_file = denv.runtime / server_pid_path(denv.store).name
    contended = "Another memtomem-server is already writing to this store"

    def standalone(log: Path) -> subprocess.Popen:
        with log.open("w") as out:
            proc = subprocess.Popen(
                [sys.executable, "-m", "memtomem.server"],
                env=denv.env(),
                cwd=str(denv.root),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=out,
            )
        denv.procs.append(proc)
        return proc

    def holder() -> str:
        return pid_file.read_text().split("\n")[0] if pid_file.exists() else ""

    # Daemon first: the standalone server warns and runs; the pid file still
    # names the daemon.
    daemon = denv.start()
    assert holder() == str(daemon.pid)
    server_log = denv.root / "server-1.log"
    server = standalone(server_log)
    assert _wait(lambda: contended in server_log.read_text())
    assert server.poll() is None
    assert holder() == str(daemon.pid)
    server.stdin.close()
    server.wait(timeout=20)
    daemon.terminate()
    daemon.wait(timeout=20)

    # Standalone first: the daemon warns and runs; the pid file still names
    # the server.
    server = standalone(denv.root / "server-2.log")
    assert _wait(lambda: holder() == str(server.pid))
    second = denv.start()
    assert _wait(lambda: contended in _log(second))
    assert holder() == str(server.pid)
    assert "memtomem Status" in denv.connect().initialize().tool("mem_status")


# ── frozen store and relative paths ──────────────────────────────────────────


def test_a_changed_store_ends_the_daemon_without_opening_it(denv: DaemonEnv) -> None:
    proc = denv.start()
    client = denv.connect().initialize()
    moved = denv.home / "moved.db"
    denv.write_config(storage={"sqlite_path": str(moved)})
    reply = client.tool("mem_status")
    assert "this daemon serves" in reply, reply
    assert client.at_eof()
    assert _exit_code(proc) == 1
    assert not moved.exists()
    assert not denv.store.exists()


def test_an_unresolvable_store_ends_the_daemon(denv: DaemonEnv) -> None:
    proc = denv.start()
    client = denv.connect().initialize()
    loop = denv.home / "loop"
    loop.symlink_to(loop)
    denv.write_config(storage={"sqlite_path": str(loop / "store.db")})
    reply = client.tool("mem_status")
    assert "cannot be resolved" in reply, reply
    assert _exit_code(proc) == 1


def test_a_store_change_seen_by_warmup_ends_the_daemon(denv: DaemonEnv) -> None:
    # Warmup starts first initialization as soon as the daemon starts, with no
    # client. The gate holds it immediately before its config rebuild, so the
    # edit is in place when the rebuild reads the file.
    gate = denv.root / "go"
    denv.write_config(warmup={"enabled": True})
    proc = denv.spawn("t", gate_rebuild=str(gate))
    assert _wait(lambda: Path(str(gate) + ".entered").exists())
    moved = denv.home / "moved.db"
    denv.write_config(storage={"sqlite_path": str(moved)}, warmup={"enabled": True})
    gate.touch()
    assert _exit_code(proc) == 1
    assert "this daemon serves" in _log(proc)
    assert not moved.exists()
    assert not denv.store.exists()


def test_a_relative_configured_path_refuses_start(denv: DaemonEnv) -> None:
    denv.write_config(session_trace={"jsonl_path": "traces.jsonl"})
    proc = denv.spawn("t")
    assert _exit_code(proc) == 2
    err = _log(proc)
    assert "session_trace.jsonl_path" in err
    assert not denv.store.exists()


def test_a_relative_root_added_later_is_not_installed(denv: DaemonEnv) -> None:
    denv.start()
    client = denv.connect().initialize()
    assert "memtomem Status" in client.tool("mem_status")

    def roots_after_edit(value: list[str], bump: float) -> str:
        config = json.loads(denv.config_path.read_text())
        config["indexing"]["memory_dirs"] = value
        denv.config_path.write_text(json.dumps(config))
        os.utime(denv.config_path, (time.time() + bump, time.time() + bump))
        client.tool("mem_status")  # a handler entering reconciles the roots
        return client.tool(
            "mem_do", {"action": "config", "params": {"key": "indexing.memory_dirs"}}
        )

    # Control: an absolute edit is installed, so reconciliation ran.
    other = denv.dir("other-mem")
    assert str(other) in roots_after_edit([str(other)], 5)
    # A relative edit is refused and the previous roots stay.
    after = roots_after_edit(["."], 10)
    assert str(other) in after, after
    assert "PosixPath('.')" not in after


# ── idle, draining and teardown ──────────────────────────────────────────────


def test_a_daemon_nobody_connects_to_expires(denv: DaemonEnv) -> None:
    started = time.monotonic()
    proc = denv.start("t", "--idle-timeout", "2")
    assert _exit_code(proc) == 0
    assert time.monotonic() - started >= 2
    assert not denv.socket_path("t").exists()
    assert not (denv.runtime / "server-daemon-t.pid").exists()


def test_the_engine_survives_the_grace_period(denv: DaemonEnv) -> None:
    status = denv.root / "status.json"
    proc = denv.start("t", "--idle-timeout", "3", status_file=str(status))
    first = denv.connect().initialize()
    first.tool("mem_status")
    assert _wait(lambda: _status(status)["components_id"] is not None)
    components = _status(status)["components_id"]
    first.close()
    time.sleep(1.5)
    assert proc.poll() is None
    second = denv.connect().initialize()
    second.tool("mem_status")
    after = _fresh_status(status)
    assert after["admitted"] == 1
    assert after["components_id"] == components
    second.close()
    assert _exit_code(proc) == 0


def test_idle_timeout_zero_never_expires(denv: DaemonEnv) -> None:
    proc = denv.start("t", "--idle-timeout", "0")
    time.sleep(3)
    assert proc.poll() is None


def test_a_connection_during_idle_teardown_is_told_to_retry(denv: DaemonEnv) -> None:
    gate = denv.root / "release"
    proc = denv.start("t", "--idle-timeout", "1", gate_close=str(gate))
    first = denv.connect().initialize()
    first.tool("mem_status")  # an engine to close
    first.close()
    # Idle expires; teardown is held in the owner's close, with the socket
    # still answering.
    assert _wait(lambda: Path(str(gate) + ".entered").exists())
    assert proc.poll() is None
    late = Client(denv.socket_path("t"), denv.dir("client"))
    assert late.state == "retry"
    late.close()
    gate.touch()
    assert _exit_code(proc) == 0
    assert not denv.socket_path("t").exists()


def test_a_connection_after_a_terminal_failure_is_refused(denv: DaemonEnv) -> None:
    gate = denv.root / "release"
    proc = denv.start(gate_close=str(gate))
    client = denv.connect().initialize()
    denv.write_config(storage={"sqlite_path": str(denv.home / "moved.db")})
    client.tool("mem_status")  # first initialization refuses the moved store
    late = Client(denv.socket_path("t"), denv.dir("client"))
    assert late.state == "refused"
    late.close()
    gate.touch()
    assert _exit_code(proc) == 1


def test_teardown_waits_for_a_connection_inside_initialization(denv: DaemonEnv) -> None:
    gate = denv.root / "release"
    status = denv.root / "status.json"
    proc = denv.start(gate_init=str(gate), status_file=str(status), drain_s=0.5)
    client = denv.connect().initialize()
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "mem_status", "arguments": {}},
        }
    )
    assert _wait(lambda: Path(str(gate) + ".entered").exists())
    os.unlink(denv.socket_path("t"))  # the self-check ends the daemon
    # Past the shortened drain: what holds now is the session group's join.
    time.sleep(2.5)
    assert proc.poll() is None
    assert _barrier_refuses()
    # The daemon's own hold, not only the owner's: teardown is waiting.
    assert _status(status)["barrier_held"] is True
    assert _status(status)["state"] == "exiting"
    gate.touch()
    assert _exit_code(proc) == 1
    assert not _barrier_refuses()


def test_teardown_waits_for_a_connection_inside_a_tool_call(denv: DaemonEnv) -> None:
    gate = denv.root / "release"
    status = denv.root / "status.json"
    proc = denv.start(gate_status=str(gate), status_file=str(status), drain_s=0.5)
    client = denv.connect().initialize()
    client.tool("mem_search", {"query": "warm"})  # initialize first
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "mem_status", "arguments": {}},
        }
    )
    assert _wait(lambda: Path(str(gate) + ".entered").exists())
    os.unlink(denv.socket_path("t"))  # the self-check ends the daemon
    # Past the shortened drain: what holds now is the session group's join.
    time.sleep(2.5)
    assert proc.poll() is None
    assert _barrier_refuses()
    # The daemon's own hold, not only the owner's: teardown is waiting.
    assert _status(status)["barrier_held"] is True
    assert _status(status)["state"] == "exiting"
    gate.touch()
    assert _exit_code(proc) == 1
    assert not _barrier_refuses()


def test_a_failed_engine_release_still_frees_the_rest(denv: DaemonEnv) -> None:
    proc = denv.start("t", "--idle-timeout", "1", fail_engine_release=True)
    assert _exit_code(proc) == 1
    assert not denv.socket_path("t").exists()
    assert not (denv.runtime / "server-daemon-t.pid").exists()
    assert not _barrier_refuses()


def test_an_owner_close_failure_is_a_failed_exit(denv: DaemonEnv) -> None:
    proc = denv.start("t", "--idle-timeout", "1", fail_owner_close=True)
    client = denv.connect().initialize()
    client.tool("mem_search", {"query": "x"})  # an engine to close
    client.close()
    assert _exit_code(proc) == 1
    assert "could not be confirmed closed" in _log(proc)


def test_an_owner_close_failure_before_initialization_is_a_failed_exit(
    denv: DaemonEnv,
) -> None:
    proc = denv.start("t", "--idle-timeout", "1", fail_owner_close=True)
    assert _exit_code(proc) == 1
    assert "closing the engine failed" in _log(proc)


def test_a_background_stop_failure_is_logged_not_failed(denv: DaemonEnv) -> None:
    # The exit code reports what outlives the process (an unconfirmed store, a
    # held reference). A watcher that would not stop dies with it.
    proc = denv.start("t", "--idle-timeout", "1", fail_watcher_stop=True)
    client = denv.connect().initialize()
    client.tool("mem_search", {"query": "x"})  # starts the watcher
    client.close()
    assert _exit_code(proc) == 0
    log = _log(proc)
    assert "Shutdown step 'watcher' failed" in log, log
    assert "closing the engine failed" not in log


def test_a_supervisor_failure_still_tears_down(denv: DaemonEnv) -> None:
    proc = denv.start(fail_supervise=True)
    assert _exit_code(proc) == 1
    assert not denv.socket_path("t").exists()
    assert not (denv.runtime / "server-daemon-t.pid").exists()
    assert not _barrier_refuses()


@pytest.mark.parametrize("step", ["supervise", "stop_sessions", "engine_release", "listener_close"])
def test_a_cancellation_at_a_teardown_step_still_frees_the_rest(denv: DaemonEnv, step: str) -> None:
    gate = denv.root / "cancel.gate"
    proc = denv.start("t", cancel_at=step, cancel_gate=str(gate))
    # Teardown starts with an open session over initialized components, so each
    # step has its real work to do when the cancellation lands.
    client = denv.connect().initialize()
    client.tool("mem_search", {"query": "x"})
    locks = sorted(p.name for p in denv.runtime.glob("server-*.pid"))
    assert "server-daemon-t.pid" in locks and len(locks) == 2, locks  # + the store's
    gate.touch()
    assert _exit_code(proc) != 0, _log(proc)
    client.close()
    assert (denv.root / "cancel.gate.closed").exists(), "the engine was not released"
    assert not denv.socket_path("t").exists()
    assert not list(denv.runtime.glob("server-*.pid"))


def test_a_session_that_raises_ends_only_itself(denv: DaemonEnv) -> None:
    status = denv.root / "status.json"
    denv.start(raise_in_session=True, status_file=str(status))
    good = denv.connect().initialize()
    bad = Client(denv.socket_path("t"), denv.dir("boom"))
    assert bad.state == "accepted"
    assert bad.at_eof()
    assert "memtomem Status" in good.tool("mem_status")
    assert _wait(lambda: _status(status)["admitted"] == 1)


def test_a_bad_connection_does_not_end_the_others(denv: DaemonEnv) -> None:
    denv.start()
    good = denv.connect().initialize()
    for payload in (b"x" * (33 * 1024 * 1024) + b"\n", b"not json\n"):
        bad = denv.connect().initialize()
        try:
            bad.sock.sendall(payload)
        except OSError:
            pass
        bad.close()
        assert "memtomem Status" in good.tool("mem_status")
    half = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    half.connect(str(denv.socket_path("t")))
    half.sendall(b'{"v": 1, "cw')
    half.close()
    assert "memtomem Status" in good.tool("mem_status")


def test_a_replaced_socket_ends_the_daemon_and_survives(denv: DaemonEnv) -> None:
    proc = denv.start()
    path = denv.socket_path("t")
    os.unlink(path)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(path))
    try:
        assert _exit_code(proc) == 1
        assert path.exists()
    finally:
        replacement.close()


def test_a_removed_socket_ends_the_daemon(denv: DaemonEnv) -> None:
    proc = denv.start()
    os.unlink(denv.socket_path("t"))
    assert _exit_code(proc) == 1


# ── refusals at start ────────────────────────────────────────────────────────


@pytest.mark.parametrize("store", [":memory:", "file:x.db?mode=memory"])
def test_an_unnameable_store_refuses_start(denv: DaemonEnv, store: str) -> None:
    denv.write_config(storage={"sqlite_path": store})
    proc = denv.spawn("t")
    assert _exit_code(proc) == 2
    assert "cannot be named" in _log(proc)


def test_an_unresolvable_store_refuses_start(denv: DaemonEnv) -> None:
    loop = denv.home / "loop"
    loop.symlink_to(loop)
    denv.write_config(storage={"sqlite_path": str(loop / "store.db")})
    proc = denv.spawn("t")
    assert _exit_code(proc) == 2
    assert "cannot be" in _log(proc)


def test_an_invalid_name_refuses_start(denv: DaemonEnv) -> None:
    proc = denv.spawn("Bad_Name")
    assert _exit_code(proc) == 2


# ── SIGTERM ──────────────────────────────────────────────────────────────────


def test_sigterm_removes_the_socket_and_both_pid_files(denv: DaemonEnv) -> None:
    from memtomem._runtime_paths import server_pid_path

    proc = denv.start()
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=20)
    assert not denv.socket_path("t").exists()
    assert not (denv.runtime / "server-daemon-t.pid").exists()
    assert not (denv.runtime / server_pid_path(denv.store).name).exists()
    assert not any((denv.runtime / "presence").iterdir())


def test_sigterm_leaves_a_replaced_socket_alone(denv: DaemonEnv) -> None:
    proc = denv.start()
    path = denv.socket_path("t")
    os.unlink(path)
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(path))
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        assert path.exists()
    finally:
        replacement.close()


def test_sigterm_handler_runs_callables_first_and_survives_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem.server as server

    order: list[str] = []
    installed: dict[str, object] = {}
    monkeypatch.setattr(signal, "signal", lambda _sig, fn: installed.setdefault("fn", fn))
    monkeypatch.setattr(os, "_exit", lambda code: order.append(f"exit {code}"))
    pid = tmp_path / "x.pid"
    pid.write_text("1")

    def boom() -> None:
        order.append("callable")
        raise RuntimeError("not an OSError")

    targets: list[object] = [pid]
    server._install_sigterm_handler(targets)
    targets.insert(0, boom)
    installed["fn"](signal.SIGTERM, None)  # type: ignore[operator]
    assert order == ["callable", "exit 0"]
    assert not pid.exists()


# ── units ────────────────────────────────────────────────────────────────────


def test_credential_parsers() -> None:
    import struct

    from memtomem.server.daemon import parse_ucred, parse_xucred

    assert parse_ucred(struct.pack("3i", 10, 501, 20)) == 501
    assert parse_xucred(struct.pack("=IIh", 0, 501, 1) + b"\0" * 66) == 501


def test_peer_uid_of_a_local_pair_is_this_user() -> None:
    from memtomem.server.daemon import peer_uid

    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid(left) == os.geteuid()
    finally:
        left.close()
        right.close()


def test_a_failing_credential_query_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from memtomem.server.daemon import peer_uid

    class Broken:
        def getsockopt(self, *args: object) -> bytes:
            raise OSError("no credentials")

    assert peer_uid(Broken()) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "line, message",
    [
        (b"not json", "not JSON"),
        (b"[]", "not a JSON object"),
        (b'{"v": 2, "cwd": "/"}', "protocol version"),
        (b'{"v": 1}', "cwd is missing"),
        (b'{"v": 1, "cwd": "relative"}', "absolute"),
        (b'{"v": 1, "cwd": "/no/such/dir/here"}', "does not exist"),
        (b'{"v": 1, "cwd": "/tmp/a\\u0000b"}', "NUL"),
    ],
)
def test_preamble_refusals(line: bytes, message: str) -> None:
    from memtomem.server.daemon import PreambleError, parse_preamble

    with pytest.raises(PreambleError, match=message):
        parse_preamble(line)


def test_socket_path_limit() -> None:
    from memtomem.server.daemon import DaemonStartError, check_socket_path

    check_socket_path(Path("/tmp/memtomem-501/daemon-a.sock"))
    with pytest.raises(DaemonStartError, match="longer"):
        check_socket_path(Path("/" + "x" * 200))


def test_a_webhook_stop_failure_does_not_mark_the_runtime() -> None:
    import asyncio
    import weakref

    from memtomem.server.lifespan import _release_server_runtime, _ServerRuntimeState

    class Failing:
        async def stop(self) -> None:
            raise RuntimeError("webhook stop failed")

    class Owner:
        webhook_manager = Failing()

        async def close(self) -> None:
            return None

    anchor = Owner()
    state = _ServerRuntimeState(server_ref=weakref.ref(anchor), owner=Owner(), references=1)
    asyncio.run(_release_server_runtime(state))
    assert not state.close_failed


def test_lifetime_lock_cleanup_is_enrolled_before_its_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import atexit

    import memtomem._pid_lock as pid_lock
    from memtomem.server import daemon

    registered: list[object] = []
    monkeypatch.setattr(atexit, "register", registered.append)

    class FailingFile:
        def seek(self, _offset: int) -> None: ...
        def truncate(self) -> None: ...
        def write(self, _text: str) -> None:
            raise OSError("disk full")

        def close(self) -> None: ...

    monkeypatch.setattr(pid_lock, "lock_pid_file", lambda *a, **k: FailingFile())
    path = tmp_path / "server-daemon-t.pid"
    path.write_text("")
    with pytest.raises(OSError, match="disk full"):
        daemon._lock_lifetime(path, [], "now")
    assert len(registered) == 1
    registered[0]()  # type: ignore[operator]
    assert not path.exists()


# ── mm daemon, and the daemon in the lifecycle commands (#3381) ──────────────


def _mm(denv: DaemonEnv, monkeypatch: pytest.MonkeyPatch, *args: str):
    """Run ``mm`` in this process against the daemon's home and runtime directory."""
    """Read-only commands only: a destructive one goes through :func:`_mm_isolated`."""
    from click.testing import CliRunner

    from memtomem.cli import cli

    assert args[0] not in _DESTRUCTIVE, f"run {args[0]!r} through _mm_isolated"
    monkeypatch.setenv("HOME", str(denv.home))
    return CliRunner().invoke(cli, list(args))


_DESTRUCTIVE = frozenset({"reset", "uninstall", "purge", "gc", "embedding-reset"})

# Refuses to run unless HOME resolves inside the test's own directory: these
# commands delete state, and some of their paths are fixed at import time.
_ISOLATED_MM = """
import pathlib, sys
root, args = pathlib.Path(sys.argv[1]).resolve(), sys.argv[2:]
home = pathlib.Path.home().resolve()
if root not in home.parents and home != root:
    sys.exit(f"refusing: HOME {home} is outside {root}")
from memtomem.cli import cli
sys.argv = ["mm", *args]
cli()
"""


@dataclass
class _Ran:
    exit_code: int
    output: str


def _mm_isolated(denv: DaemonEnv, *args: str) -> _Ran:
    """A destructive ``mm`` command in its own process, with HOME inside the test.

    In process, a module imported earlier under the real HOME keeps paths it
    derived from it (``uninstall_cmd._DEFAULT_STATE_DIR``), and the command
    would act on the real state directory.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATED_MM, str(denv.root), *args],
        env=denv.env(),
        cwd=str(denv.root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return _Ran(proc.returncode, proc.stdout + proc.stderr)


def _lifetime(denv: DaemonEnv, name: str = "t") -> Path:
    return denv.runtime / f"server-daemon-{name}.pid"


def _store_pid_file(denv: DaemonEnv) -> Path:
    from memtomem._runtime_paths import server_pid_path

    return denv.runtime / server_pid_path(denv.store).name


def test_the_isolated_runner_refuses_a_home_outside_the_test(denv: DaemonEnv) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATED_MM, str(denv.root), "uninstall", "--yes"],
        env={
            **denv.env(),
            "HOME": str(denv.root.parent),
            "USERPROFILE": str(denv.root.parent),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert f"refusing: HOME {denv.root.parent.resolve()} is outside" in proc.stderr


def test_daemon_status_reports_a_running_daemon(
    denv: DaemonEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = denv.start()
    result = _mm(denv, monkeypatch, "daemon", "status")
    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"t  running  pid={proc.pid}  started=")

    result = _mm(denv, monkeypatch, "daemon", "status", "t", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    (row,) = payload["daemons"]
    assert (row["name"], row["status"], row["pid"], row["socket"]) == (
        "t",
        "running",
        proc.pid,
        True,
    )

    result = _mm(denv, monkeypatch, "daemon", "status", "other")
    assert result.exit_code == 3
    assert result.output == "no shared daemon named 'other'\n"


def test_daemon_stop_ends_its_sessions_and_releases_every_lock(
    denv: DaemonEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = denv.start()
    client = denv.connect().initialize()
    client.tool("mem_search", {"query": "anything"})  # the store is open
    assert _lifetime(denv).exists() and _store_pid_file(denv).exists()

    result = _mm(denv, monkeypatch, "daemon", "stop", "t")
    assert result.exit_code == 0, result.output
    assert result.output == f"stopped t pid={proc.pid}\n"
    assert _exit_code(proc) == 0
    assert client.at_eof()
    assert not _lifetime(denv).exists()
    assert not _store_pid_file(denv).exists()
    assert not denv.socket_path("t").exists()
    assert not _barrier_refuses()

    result = _mm(denv, monkeypatch, "daemon", "status", "t")
    assert result.exit_code == 3
    result = _mm(denv, monkeypatch, "daemon", "stop", "t")
    assert result.exit_code == 0
    assert result.output == "shared daemon 't' is not running\n"


def test_daemon_stop_without_a_name_stops_every_daemon(
    denv: DaemonEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = denv.start("a")
    second = denv.start("b")
    result = _mm(denv, monkeypatch, "daemon", "stop")
    assert result.exit_code == 0, result.output
    assert result.output == f"stopped a pid={first.pid}\nstopped b pid={second.pid}\n"
    assert _exit_code(first) == 0 and _exit_code(second) == 0


def test_a_stale_lifetime_lock_is_shown_and_left_by_stop(
    denv: DaemonEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _lifetime(denv)
    stale.write_text("999999\n\n2026-11-16T00:00:00+00:00\n")
    result = _mm(denv, monkeypatch, "daemon", "status")
    assert result.exit_code == 3
    assert result.output == f"t  stopped  (stale pid file at {stale})\n"
    result = _mm(denv, monkeypatch, "daemon", "stop")
    assert result.exit_code == 0, result.output
    assert result.output == "no shared daemon running\n"
    assert stale.exists()  # harmless; the next daemon named t takes it over


def test_a_transient_holder_of_the_lifetime_lock_delays_the_daemon_without_ending_it(
    tmp_path: Path,
) -> None:
    """A stale-file cleanup locks the file briefly to delete it; a starting daemon waits.

    In process, so the hold provably overlaps the daemon's lock attempt: a
    subprocess's start-up alone outlasts a short hold.
    """
    import threading

    import memtomem.server.daemon as daemon_mod
    from memtomem._pid_lock import lock_pid_file

    path = tmp_path / "server-daemon-t.pid"
    held = lock_pid_file(path, label="test", timeout_s=0)
    assert held is not None
    releaser = threading.Timer(0.2, held.close)
    releaser.start()
    targets: list[object] = []
    try:
        fp = daemon_mod._lock_lifetime(path, targets, "2026-11-16T00:00:00+00:00")
    finally:
        releaser.join()
    try:
        assert targets == [path]
        assert path.read_text().startswith(f"{os.getpid()}\n")
    finally:
        fp.close()


def test_reset_names_a_daemon_that_has_not_opened_its_store(denv: DaemonEnv) -> None:
    proc = denv.start()
    result = _mm_isolated(denv, "reset", "--yes")
    assert result.exit_code == 2, result.output
    assert f"Shared daemon 't' still running (pid {proc.pid})" in result.output
    assert "Stop it with `mm daemon stop t`" in result.output


def test_reset_and_uninstall_name_a_daemon_serving_a_session(denv: DaemonEnv) -> None:
    proc = denv.start()
    client = denv.connect().initialize()
    client.tool("mem_search", {"query": "anything"})  # registered as a live instance
    hint = f"Shared daemon 't' (pid {proc.pid}) is running; stop it with `mm daemon stop t`."
    result = _mm_isolated(denv, "reset", "--yes")
    assert result.exit_code == 2, result.output
    assert "A live memtomem instance" in result.output
    assert hint in result.output
    result = _mm_isolated(denv, "uninstall", "--yes")
    assert result.exit_code == 2, result.output
    assert "A live memtomem instance" in result.output
    assert hint in result.output
    assert denv.config_path.exists()


def test_uninstall_removes_dead_daemons_leftovers_and_keeps_a_starting_ones_lock(
    denv: DaemonEnv,
) -> None:
    from memtomem._pid_lock import lock_pid_file

    dead = _lifetime(denv, "dead")
    dead.write_text("999999\n\n2026-11-16T00:00:00+00:00\n")
    orphan = denv.socket_path("orphan")  # a socket whose lock file is already gone
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(orphan))
    sock.close()
    starting = _lifetime(denv, "starting")
    # A daemon that has taken its lifetime lock and waits for the barrier.
    held = lock_pid_file(starting, label="test", timeout_s=0)
    assert held is not None
    try:
        result = _mm_isolated(denv, "uninstall", "--yes")
        assert result.exit_code == 0, result.output
        assert "No memtomem state to remove" not in result.output
        assert not denv.config_path.exists()  # state was deleted, so the sweep ran
        assert not dead.exists()
        assert not orphan.exists()
        assert starting.exists()
    finally:
        held.close()


def test_doctor_lists_a_running_daemon(denv: DaemonEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = denv.start()
    result = _mm(denv, monkeypatch, "doctor", "--json")
    payload = json.loads(result.stdout)
    (check,) = [c for c in payload["checks"] if c["name"] == "shared-daemons"]
    assert check["status"] == "info"
    assert check["message"] == f"1 shared daemon(s) running: t (pid {proc.pid})"
    assert [d["name"] for d in check["data"]["daemons"]] == ["t"]


def test_mem_status_inside_a_daemon_names_it(denv: DaemonEnv) -> None:
    """Through the real module entry, where the daemon module runs as ``__main__``."""
    log = denv.root / "module-entry.log"
    with log.open("w") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "memtomem.server.daemon", "t"],
            env=denv.env(),
            cwd=str(denv.root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    proc.log = log  # type: ignore[attr-defined]
    denv.procs.append(proc)
    assert _wait(lambda: denv.socket_path("t").exists() or proc.poll() is not None)
    assert proc.poll() is None, _log(proc)
    client = denv.connect().initialize()
    text = client.tool("mem_status")
    assert f"Shared daemon:   t (pid {proc.pid}, 1 connection(s))" in text


def test_the_summary_is_published_only_while_the_daemon_serves(tmp_path: Path) -> None:
    import memtomem.server.daemon as daemon_mod
    from memtomem.server.context import shared_daemon_summary

    daemon = daemon_mod.Daemon(
        name="t",
        idle_timeout_s=0,
        frozen_store=tmp_path / "store.db",
        runtime_dir=tmp_path,
        sigterm_targets=[],
    )
    assert shared_daemon_summary() is None
    with pytest.raises(RuntimeError):
        with daemon_mod._published_summary(daemon):
            assert shared_daemon_summary() == {
                "name": "t",
                "pid": os.getpid(),
                "connections": 0,
            }
            raise RuntimeError("serve failed")
    assert shared_daemon_summary() is None


def test_mem_status_outside_a_daemon_has_no_shared_daemon_key() -> None:
    from memtomem.server.tools.status_config import _shared_daemon_runtime

    assert _shared_daemon_runtime() == {}


def test_a_terminal_failure_after_the_idle_exit_was_chosen_fails_the_exit(
    tmp_path: Path,
) -> None:
    import memtomem.server.daemon as daemon_mod

    daemon = daemon_mod.Daemon(
        name="t",
        idle_timeout_s=0,
        frozen_store=tmp_path / "store.db",
        runtime_dir=tmp_path,
        sigterm_targets=[],
    )
    daemon.exit_reason = "idle"
    assert daemon.exit_code() == daemon_mod.EXIT_OK
    daemon.notify_terminal("the store changed during teardown")
    assert daemon.exit_code() == daemon_mod.EXIT_FAILED
