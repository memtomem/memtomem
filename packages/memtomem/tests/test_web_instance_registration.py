"""``mm web`` registers in the instance registry (#2574).

What registering has to carry with it:

* ``web.json`` names the process's registry identity (``procid``), so the Web
  UI's registration can be told from a server's by ``(pid, procid)`` rather
  than by a pid another pid namespace may share;
* ``mm upgrade`` attributes that registration to the Web UI it already stops,
  and nothing else;
* ``mem_status`` does not count the Web UI as a concurrent MCP server;
* the #2564 migration fence, ``mm uninstall`` and ``mm reset`` now see it.

Cross-process claims use spawned children: the registry and the fence both
skip the calling process's own identity, so an in-process check proves
nothing about another process seeing the Web UI.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import memtomem._instance_registry as reg
from memtomem._instance_registry import EnumerationResult, InstanceInfo, RegistrySnapshot
from memtomem.cli import upgrade_cmd, web as web_cli
from memtomem.cli._liveness import ServerState
from memtomem.server.tools import status_config

_CTX = mp.get_context("spawn")
_PROCID = "0a1b2c3d"


def _info(pid: int, procid: str, *, ppid: int = 1) -> InstanceInfo:
    return InstanceInfo(
        pid=pid, ppid=ppid, digest="d" * 16, procid=procid, path=Path(f"/x/{pid}-{procid}")
    )


def _snapshot(*instances: InstanceInfo) -> RegistrySnapshot:
    return RegistrySnapshot(
        instances=tuple(instances),
        complete=True,
        stale_seen=0,
        unlocked_fresh_seen=0,
        unparseable_seen=0,
        roots_consulted=1,
        canonical_error=None,
        refusal=None,
        presence=(),
    )


def _web_state(tmp_path: Path, *, pid: int | None, sidecar: dict | None, **kw) -> ServerState:
    """A web liveness result whose ``web.json`` sidecar holds ``sidecar``."""
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("", encoding="utf-8")
    if sidecar is not None:
        (tmp_path / "web.json").write_text(json.dumps(sidecar), encoding="utf-8")
    fields = {"alive": True, "pid": pid, "pid_file": pid_file}
    fields.update(kw)
    return ServerState(**fields)


_DEAD = ServerState(alive=False, pid=None, pid_file=None)


# --- web.json identity -------------------------------------------------------


class TestWebMetadataIdentity:
    def test_metadata_records_this_process_registry_identity(self, tmp_path) -> None:
        pid_file = tmp_path / "web.pid"
        with open(pid_file, "a+") as fp:
            web_cli._write_web_metadata(pid_file, fp, pid=4321, port=8080, started="t")

        meta = web_cli._read_web_metadata(pid_file)
        assert meta.procid == reg.current_process_id()
        assert (meta.pid, meta.port, meta.started) == (4321, 8080, "t")

    def test_sidecar_from_an_older_release_still_parses(self, tmp_path) -> None:
        (tmp_path / "web.json").write_text(
            json.dumps({"pid": 7, "port": 8080, "started": "t"}), encoding="utf-8"
        )
        meta = web_cli._read_web_metadata(tmp_path / "web.pid")
        assert (meta.pid, meta.port, meta.procid) == (7, 8080, None)

    @pytest.mark.parametrize("bad", ["0A1B2C3D", "0a1b2c3", "0a1b2c3dd", "0a1b2c3g", 12345678])
    def test_malformed_procid_is_dropped(self, tmp_path, bad) -> None:
        (tmp_path / "web.json").write_text(json.dumps({"pid": 7, "procid": bad}), encoding="utf-8")
        assert web_cli._read_web_metadata(tmp_path / "web.pid").procid is None

    def test_identity_when_the_locked_pid_agrees(self, tmp_path) -> None:
        state = _web_state(tmp_path, pid=77, sidecar={"pid": 77, "procid": _PROCID})
        assert web_cli.verified_web_identity(state) == (77, _PROCID)

    def test_identity_from_the_sidecar_when_the_locked_pid_is_unreadable(self, tmp_path) -> None:
        """Windows cannot read a live locked ``web.pid`` (``pid=None``)."""
        state = _web_state(tmp_path, pid=None, sidecar={"pid": 77, "procid": _PROCID})
        assert web_cli.verified_web_identity(state) == (77, _PROCID)

    @pytest.mark.parametrize(
        "pid, sidecar, extra",
        [
            (77, {"pid": 78, "procid": _PROCID}, {}),  # sidecar from another process
            (77, {"pid": 77}, {}),  # older release: no procid
            (77, None, {}),  # no sidecar
            (77, {"pid": 77, "procid": _PROCID}, {"alive": False}),
            (77, {"pid": 77, "procid": _PROCID}, {"probe_error": "unreadable"}),
            # Windows shape (locked pid unreadable): only the sidecar speaks,
            # so a pid that is not a positive int must not become one.
            (None, {"pid": True, "procid": _PROCID}, {}),
            (None, {"pid": 0, "procid": _PROCID}, {}),
            (None, {"pid": -1, "procid": _PROCID}, {}),
        ],
    )
    def test_unproven_identity_is_none(self, tmp_path, pid, sidecar, extra) -> None:
        state = _web_state(tmp_path, pid=pid, sidecar=sidecar, **extra)
        assert web_cli.verified_web_identity(state) is None

    @pytest.mark.parametrize(
        ("platform", "expected"),
        [
            ("posix", ["unlink web.json", "unlink web.pid", "close"]),
            ("nt", ["unlink web.json", "close", "unlink web.pid"]),
        ],
    )
    def test_cleanup_unlinks_the_sidecar_while_the_lock_is_held(
        self, tmp_path, monkeypatch, platform: str, expected: list[str]
    ) -> None:
        """The sidecar goes first, before the lock is released on either
        platform: once released, a replacement can lock ``web.pid`` and write
        its own sidecar, which this exit must not delete (#2574, #2610).
        Windows must close before deleting ``web.pid`` (NTFS refuses to delete
        an open file); POSIX deletes it while still holding the lock."""
        pid_file = tmp_path / "web.pid"
        pid_file.write_text("", encoding="utf-8")
        (tmp_path / "web.json").write_text("{}", encoding="utf-8")
        order: list[str] = []
        real_unlink = Path.unlink

        def spy(self: Path, missing_ok: bool = False) -> None:
            order.append(f"unlink {self.name}")
            real_unlink(self, missing_ok=missing_ok)

        class FakeLock:
            def close(self) -> None:
                order.append("close")

        monkeypatch.setattr(Path, "unlink", spy)
        with monkeypatch.context() as m:
            m.setattr(web_cli.os, "name", platform)
            web_cli._cleanup_web_files(pid_file, FakeLock())
        assert order == expected


# --- mm upgrade attribution --------------------------------------------------


class TestUpgradeAttributesTheWebRegistration:
    def test_the_web_registration_is_attributed(self, tmp_path) -> None:
        web = _web_state(tmp_path, pid=77, sidecar={"pid": 77, "procid": _PROCID})
        assert (
            upgrade_cmd._registry_inventory_problems(_snapshot(_info(77, _PROCID)), [], web) == []
        )

    def test_a_same_pid_process_with_another_identity_still_refuses(self, tmp_path) -> None:
        """A server in another pid namespace can share the Web UI's pid."""
        web = _web_state(tmp_path, pid=77, sidecar={"pid": 77, "procid": _PROCID})
        problems = upgrade_cmd._registry_inventory_problems(
            _snapshot(_info(77, "ffffffff")), [], web
        )
        assert problems == [
            "server registry: live pid 77 has no authoritative pid lock (secondary or "
            "startup-only process, or a Web UI started without `mm web`); stop it manually"
        ]

    def test_a_second_identity_beside_the_web_one_refuses(self, tmp_path) -> None:
        web = _web_state(tmp_path, pid=77, sidecar={"pid": 77, "procid": _PROCID})
        problems = upgrade_cmd._registry_inventory_problems(
            _snapshot(_info(77, _PROCID), _info(77, "ffffffff")), [], web
        )
        assert problems == [
            "server registry: pid 77 identifies 2 live process identities across "
            "namespaces; automatic termination is unsafe"
        ]

    @pytest.mark.parametrize(
        "sidecar, extra",
        [
            (None, {}),
            ({"pid": 77}, {}),
            ({"pid": 78, "procid": _PROCID}, {}),
            ({"pid": 77, "procid": _PROCID}, {"alive": False}),
            ({"pid": 77, "procid": _PROCID}, {"probe_error": "unreadable"}),
        ],
    )
    def test_unproven_web_identity_refuses(self, tmp_path, sidecar, extra) -> None:
        web = _web_state(tmp_path, pid=77, sidecar=sidecar, **extra)
        problems = upgrade_cmd._registry_inventory_problems(_snapshot(_info(77, _PROCID)), [], web)
        assert len(problems) == 1 and "live pid 77 has no authoritative pid lock" in problems[0]


# --- mem_status concurrent writers -------------------------------------------


def _seed_registry(monkeypatch, *instances: InstanceInfo) -> None:
    monkeypatch.setattr(status_config, "_store_digest_for", lambda _p: "d" * 16)
    monkeypatch.setattr(
        status_config,
        "_enumerate_live_instances",
        lambda _d: EnumerationResult(tuple(instances), True),
    )


class TestStatusDoesNotCountTheWebUI:
    def test_one_server_beside_the_web_ui_is_not_a_warning(self, monkeypatch) -> None:
        _seed_registry(monkeypatch, _info(100, "aaaaaaaa"), _info(77, _PROCID))
        monkeypatch.setattr(status_config, "_web_ui_identity", lambda: (77, _PROCID))
        assert status_config._collect_concurrent_writers(Path("m.db")) is None

    def test_two_servers_beside_the_web_ui_warn_and_name_it_apart(self, monkeypatch) -> None:
        # The two servers share a parent; the Web UI has another. Only the
        # servers' parents count toward ``same_parent``.
        _seed_registry(
            monkeypatch,
            _info(100, "aaaaaaaa", ppid=7),
            _info(200, "bbbbbbbb", ppid=7),
            _info(77, _PROCID, ppid=8),
        )
        monkeypatch.setattr(status_config, "_web_ui_identity", lambda: (77, _PROCID))
        warning = status_config._collect_concurrent_writers(Path("m.db"))
        assert warning is not None
        assert warning["detail"].startswith(
            "2 live memtomem-server processes (pids 100, 200) have this store open."
        )
        assert " The Web UI (pid 77) also has it open." in warning["detail"]
        assert warning["same_parent"] == "true"

    def test_a_same_pid_server_with_another_identity_is_counted(self, monkeypatch) -> None:
        _seed_registry(monkeypatch, _info(100, "aaaaaaaa"), _info(77, "ffffffff"))
        monkeypatch.setattr(status_config, "_web_ui_identity", lambda: (77, _PROCID))
        warning = status_config._collect_concurrent_writers(Path("m.db"))
        assert warning is not None
        assert "pids 77, 100" in warning["detail"]
        assert "Web UI" not in warning["detail"]

    def test_unknown_web_identity_counts_as_before(self, monkeypatch) -> None:
        _seed_registry(monkeypatch, _info(100, "aaaaaaaa"), _info(77, _PROCID))
        monkeypatch.setattr(status_config, "_web_ui_identity", lambda: None)
        warning = status_config._collect_concurrent_writers(Path("m.db"))
        assert warning is not None and "pids 77, 100" in warning["detail"]

    def test_windows_shaped_web_state_is_matched_through_the_sidecar(
        self, tmp_path, monkeypatch
    ) -> None:
        """The real probe path, with ``pid=None`` as Windows reports a live lock."""
        import memtomem.cli._liveness as liveness

        web = _web_state(tmp_path, pid=None, sidecar={"pid": 77, "procid": _PROCID})
        monkeypatch.setattr(liveness, "check_web_liveness", lambda: web)
        _seed_registry(monkeypatch, _info(100, "aaaaaaaa"), _info(77, _PROCID))
        assert status_config._collect_concurrent_writers(Path("m.db")) is None

    def test_a_stale_sidecar_leaves_the_process_counted(self, tmp_path, monkeypatch) -> None:
        import memtomem.cli._liveness as liveness

        web = _web_state(tmp_path, pid=None, sidecar={"pid": 77, "procid": "99999999"})
        monkeypatch.setattr(liveness, "check_web_liveness", lambda: web)
        _seed_registry(monkeypatch, _info(100, "aaaaaaaa"), _info(77, _PROCID))
        assert status_config._collect_concurrent_writers(Path("m.db")) is not None

    def test_a_failing_web_probe_counts_as_before(self, monkeypatch) -> None:
        import memtomem.cli._liveness as liveness

        def boom() -> ServerState:
            raise RuntimeError("probe failed")

        monkeypatch.setattr(liveness, "check_web_liveness", boom)
        assert status_config._web_ui_identity() is None


# --- cross-process: a real ``mm web`` lock + registration in a child ----------


def _child_web(db: str, q, release) -> None:
    """Hold the real ``web.pid`` lock and the Web UI's registration, like
    ``mm web`` does (the lock in the CLI, the registration in the lifespan,
    one process). Runtime paths come from the inherited test override."""
    from memtomem.cli.web import _web_pid_lock
    from memtomem.web.app import _acquire_instance_registration_settled

    comp = SimpleNamespace(config=SimpleNamespace(storage=SimpleNamespace(sqlite_path=db)))
    with _web_pid_lock(port=0):
        registration, _ = asyncio.run(_acquire_instance_registration_settled(comp))
        q.put(("held", os.getpid(), registration is not None))
        release.wait(60)
        if registration is not None:
            registration.cleanup()


def _drain(q, tag: str, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = q.get(timeout=1.0)
        except Exception:
            continue
        if msg[0] == tag:
            return msg
    raise AssertionError(f"child never reported {tag!r}")


@pytest.fixture
def web_child(tmp_path):
    """Start a spawned child holding a Web UI lock + registration on a store.

    Yields ``(db_path, child_pid)``. The conftest runtime-dir override is in
    ``os.environ`` and so reaches the spawn child; the parent's registry is
    pointed at the same directory by the autouse isolation fixture.
    """
    started: list = []

    def start(db: Path) -> int:
        q, release = _CTX.Queue(), _CTX.Event()
        proc = _CTX.Process(target=_child_web, args=(str(db), q, release))
        proc.start()
        started.append((proc, release))
        _, pid, registered = _drain(q, "held")
        assert registered, "the child could not register"
        return pid

    yield start
    for proc, release in started:
        release.set()
        proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=30)


@pytest.mark.skipif(sys.platform == "win32", reason="upgrade refuses any live web on Windows")
def test_a_running_web_ui_is_attributed_across_processes(tmp_path, web_child) -> None:
    from memtomem.cli._liveness import check_web_liveness

    db = tmp_path / "m.db"
    db.write_bytes(b"")
    pid = web_child(db)

    web = check_web_liveness()
    identity = web_cli.verified_web_identity(web)
    snapshot = reg.snapshot_all_instances()
    assert identity is not None and identity[0] == pid
    assert identity in {(i.pid, i.procid) for i in snapshot.instances}
    assert identity[1] != reg.current_process_id()
    assert upgrade_cmd._registry_inventory_problems(snapshot, [], web) == []


def test_status_does_not_count_a_running_web_ui_across_processes(tmp_path, web_child) -> None:
    db = tmp_path / "m.db"
    db.write_bytes(b"")
    web_child(db)
    mine = reg.register_instance(db)
    assert mine is not None
    try:
        # Two live registrations on the store — this process and the Web UI —
        # which counted as two servers before #2574.
        result = reg.enumerate_live_instances(reg.store_digest_for(db))
        assert len({i.procid for i in result.instances}) == 2
        assert status_config._collect_concurrent_writers(db) is None
    finally:
        mine.cleanup()


def test_uninstall_and_reset_see_a_running_web_ui(tmp_path, web_child) -> None:
    db = tmp_path / "m.db"
    db.write_bytes(b"")
    web_child(db)
    assert reg.probe_all_for_uninstall().state == "LIVE"


async def test_the_migration_fence_sees_a_running_web_ui(tmp_path, web_child) -> None:
    from memtomem.errors import SchemaMigrationBlockedError
    from memtomem.storage.sqlite_backend import SqliteBackend
    from test_schema_live_peers import _assert_untouched, _make_old_store

    cfg = await _make_old_store(tmp_path)
    pid = web_child(Path(cfg.sqlite_path))

    storage = SqliteBackend(cfg, dimension=8)
    with pytest.raises(SchemaMigrationBlockedError) as excinfo:
        await storage.initialize()
    assert f"pid {pid})" in str(excinfo.value)
    assert "MCP servers or `mm web`" in str(excinfo.value)
    _assert_untouched(cfg)
