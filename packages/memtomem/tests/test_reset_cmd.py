"""Tests for ``mm reset`` — liveness/DB-lock gates and ``--backup`` (#1574 item 7).

The command is destructive by intent, so the contract under test is the
gate ordering, not the wipe itself (``reset_all`` is covered in
``test_storage_extended.py``):

* server/web liveness and the ``BEGIN IMMEDIATE`` write-lock probe refuse
  BEFORE the storage backend is constructed (``-y`` never bypasses them,
  ``--force`` does);
* the instance-registry gate (#1935, #1945) refuses on LIVE/UNKNOWN/
  UNTRUSTED evidence, is never ``--force``-overridable, and is
  deliberately user-wide (an unrelated-store server also refuses);
* the lifecycle barrier (#1936, #1945) is taken exclusive around both
  write boundaries — ``initialize()`` and backup + ``reset_all()`` — is
  never ``--force``-overridable, and is released after a confirmed (or
  never-opened) storage close but *retained* on an unconfirmed one
  (proven from a separate process: the autouse
  ``_isolated_instance_registry`` sweep and Windows same-process
  reacquire both make in-process checks false evidence);
* the store identity counted in Phase A is revalidated under the Phase B
  barrier, so a database removed or swapped during the prompt is refused
  rather than wiped or resurrected on stale consent;
* ``--backup`` snapshots via the sqlite3 backup API, so WAL-resident
  commits survive — the failure a plain file copy would silently cause;
* a failed backup aborts without wiping.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing as mp
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem._instance_registry import UninstallProbeResult
from memtomem.cli import cli
from memtomem.cli import reset_cmd
from memtomem.cli._liveness import ServerState

from .helpers import set_home
from .test_uninstall_cmd import (
    _child_hold_shared_barrier,
    _child_try_exclusive_barrier,
    _hold_pid_lock,
    _seed_sentinel,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Tmp HOME + stripped ``MEMTOMEM_*`` env, mirroring
    ``test_uninstall_cmd.home`` (module-bound ``_bootstrap._CONFIG_PATH``
    and ``XDG_RUNTIME_DIR`` isolation included)."""
    import tempfile

    from memtomem.cli import _bootstrap

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.chdir(h)
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    fake_tempdir = tmp_path / "tempdir"
    fake_tempdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(fake_tempdir))
    set_home(monkeypatch, h)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", h / ".memtomem" / "config.json")
    return h


_DEAD = ServerState(alive=False, pid=None, pid_file=None)


def _patch_liveness(monkeypatch, server: ServerState = _DEAD, web: ServerState = _DEAD) -> None:
    monkeypatch.setattr(reset_cmd, "check_server_liveness", lambda: server)
    monkeypatch.setattr(reset_cmd, "check_web_liveness", lambda: web)


def _init_and_index(home: Path, runner: CliRunner) -> Path:
    """Fresh ``--provider none`` install with one indexed chunk; returns db path."""
    mem_dir = home / "memories"
    mem_dir.mkdir(exist_ok=True)
    (mem_dir / "note.md").write_text("# memo\n\nhello reset test\n", encoding="utf-8")
    r = runner.invoke(
        cli,
        ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
    )
    assert r.exit_code == 0, f"init failed: {r.output}"
    r = runner.invoke(cli, ["index", str(mem_dir)])
    assert r.exit_code == 0, f"index failed: {r.output}"
    return home / ".memtomem" / "memtomem.db"


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- gates


class TestResetGates:
    def test_refuses_when_server_alive(self, home, monkeypatch):
        _patch_liveness(
            monkeypatch, server=ServerState(alive=True, pid=4242, pid_file=home / "pid")
        )
        result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code == 2, result.output
        assert "MCP server still running (pid 4242)" in result.output
        assert "--force" in result.output

    def test_refuses_when_web_alive_naming_pid_and_port(self, home, monkeypatch):
        _patch_liveness(
            monkeypatch,
            web=ServerState(alive=True, pid=777, pid_file=home / "web.pid", port=8080),
        )
        result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code == 2, result.output
        assert "mm web still running (pid 777, port 8080)" in result.output

    def test_yes_does_not_bypass_gates(self, home, monkeypatch):
        """``-y`` skips only the confirmation prompt — both refusal tests
        above already pass ``-y``; this pins the intent explicitly against
        a future refactor that folds the gates into the prompt branch."""
        _patch_liveness(monkeypatch, server=ServerState(alive=True, pid=1, pid_file=home / "pid"))
        assert CliRunner().invoke(cli, ["reset", "-y"]).exit_code == 2

    def test_refuses_on_real_db_writer(self, home, monkeypatch):
        """An active writer with no pid file (mm web / watchdog / ad-hoc
        connection) is caught by the ``BEGIN IMMEDIATE`` probe — the #384
        gap, now closed for reset too."""
        _patch_liveness(monkeypatch)
        state = home / ".memtomem"
        state.mkdir(parents=True)
        db_path = state / "memtomem.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE _probe (id INTEGER)")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            result = CliRunner().invoke(cli, ["reset", "-y"])
            assert result.exit_code == 2, result.output
            assert "holds a write lock" in result.output
            assert str(db_path) in result.output
            # Nothing touched — the probe fired before the backend opened.
            assert db_path.exists()
        finally:
            conn.rollback()
            conn.close()

    def test_force_bypasses_gates(self, home, monkeypatch):
        """``--force`` is the stale-pid recovery hatch (uninstall parity):
        a supposedly-alive server must not make reset permanently refuse."""
        _patch_liveness(
            monkeypatch, server=ServerState(alive=True, pid=9999, pid_file=home / "pid")
        )
        result = CliRunner().invoke(cli, ["reset", "-y", "--force"])
        assert result.exit_code == 0, result.output
        # Proceeded past the gates to the empty-DB early exit.
        assert "already empty" in result.output


# ---------------------------------------------------------------- --json acks


class TestResetJson:
    """``--json`` write acks (#1615) — CONTRIBUTING write-command shape:
    success and no-op exit 0; handled failures retain their JSON body and exit
    1. Text-path behavior is untouched."""

    def test_wipe_json_ack(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        result = runner.invoke(cli, ["reset", "-y", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["backup"] is None
        assert data["deleted"].get("chunks", 0) >= 1
        assert _count(db_path, "chunks") == 0

    def test_already_empty_json_is_ok_noop(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        mem_dir = home / "memories"
        mem_dir.mkdir(exist_ok=True)
        r = runner.invoke(
            cli,
            ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
        )
        assert r.exit_code == 0, r.output

        result = runner.invoke(cli, ["reset", "-y", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"ok": True, "deleted": {}, "backup": None}

    def test_gate_refusal_json_exit_one(self, home, monkeypatch):
        _patch_liveness(
            monkeypatch, server=ServerState(alive=True, pid=4242, pid_file=home / "pid")
        )

        result = CliRunner().invoke(cli, ["reset", "-y", "--json"])

        # Text path exits 2; under --json the refusal is a handled failure
        # carried in the body with the standard automation exit code.
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "MCP server still running" in data["reason"]
        assert "--force" in data["reason"]

    def test_backup_json_ack_carries_path(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)

        result = runner.invoke(cli, ["reset", "-y", "--backup", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        (bak,) = (home / ".memtomem").glob("memtomem.db.pre-reset-*.bak")
        assert data["backup"] == str(bak)

    def test_backup_failure_json_exit_one_without_wiping(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        def _boom(_db_path: Path) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(reset_cmd, "_backup_db", _boom)
        result = runner.invoke(cli, ["reset", "-y", "--backup", "--json"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["ok"] is False
        assert "backup failed" in data["reason"]
        assert _count(db_path, "chunks") >= 1, "DB was wiped despite backup failure"

    def test_cancelled_confirm_json(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        result = runner.invoke(cli, ["reset", "--json"], input="n\n")

        assert result.exit_code == 1, result.output
        # stdout must be a single JSON document — the prompt rides stderr
        # under --json so `mm reset --json | jq` works on the cancel path.
        # (result.output merges the streams; assert on stdout alone.)
        data = json.loads(result.stdout)
        assert data == {"ok": False, "reason": "cancelled at confirmation prompt"}
        assert "Continue?" in result.stderr
        assert _count(db_path, "chunks") >= 1

    def test_cancelled_confirm_json_win_prompt_branch(self, home, monkeypatch):
        """#1640: click's WIN prompt branch leaked the CliRunner reply echo
        into stdout (`' n\\n' + JSON`), failing this flow on windows-latest.
        ``_prompts.confirm`` bypasses that branch, so stdout must stay a
        single JSON document even with the branch forced."""
        import click.termui

        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        monkeypatch.setattr(click.termui, "WIN", True)

        result = runner.invoke(cli, ["reset", "--json"], input="n\n")

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data == {"ok": False, "reason": "cancelled at confirmation prompt"}


# ---------------------------------------------------------------- backup


class TestResetBackup:
    def test_backup_is_standalone_and_contains_wal_resident_rows(self, home, monkeypatch):
        """The snapshot must go through the sqlite3 backup API: a row
        committed by a still-open connection lives in the ``-wal`` file,
        and a plain copy of the main DB file would silently drop it."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        # WAL-resident commit: keep the connection open so nothing
        # checkpoints before the backup runs.
        wal_writer = sqlite3.connect(db_path)
        try:
            wal_writer.execute("CREATE TABLE wal_probe (x INTEGER)")
            wal_writer.execute("INSERT INTO wal_probe VALUES (42)")
            wal_writer.commit()

            result = runner.invoke(cli, ["reset", "-y", "--backup"])
            assert result.exit_code == 0, result.output
            assert "Backup written to" in result.output
        finally:
            wal_writer.close()

        baks = sorted((home / ".memtomem").glob("memtomem.db.pre-reset-*.bak"))
        assert len(baks) == 1, f"expected one backup, found {baks}"
        # Standalone: a consistent DB with no -wal/-shm siblings needed.
        assert not baks[0].with_name(baks[0].name + "-wal").exists()
        assert _count(baks[0], "chunks") >= 1, "pre-wipe chunk missing from backup"
        assert _count(baks[0], "wal_probe") == 1, (
            "WAL-resident commit missing from backup — snapshot must use the "
            "sqlite3 backup API, not a file copy"
        )
        # The live DB was wiped after the snapshot.
        assert _count(db_path, "chunks") == 0

    def test_backup_failure_aborts_without_wiping(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        def _boom(_db_path: Path) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(reset_cmd, "_backup_db", _boom)
        result = runner.invoke(cli, ["reset", "-y", "--backup"])
        assert result.exit_code == 1, result.output
        assert "aborting without wiping" in result.output
        assert _count(db_path, "chunks") >= 1, "DB was wiped despite backup failure"

    def test_backup_lands_next_to_custom_path_db(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        custom_dir = home / "custom-storage"
        custom_dir.mkdir()
        custom_db = custom_dir / "elsewhere.db"
        monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(custom_db))

        runner = CliRunner()
        _init_and_index(home, runner)
        assert custom_db.exists(), "env-pinned custom storage path not honored"

        result = runner.invoke(cli, ["reset", "-y", "--backup"])
        assert result.exit_code == 0, result.output
        assert list(custom_dir.glob("elsewhere.db.pre-reset-*.bak")), (
            "backup must live next to the actual DB, not the default state dir"
        )
        assert not list((home / ".memtomem").glob("*.bak"))

    def test_cancelled_confirm_leaves_db_intact(self, home, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)
        result = runner.invoke(cli, ["reset"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        assert _count(db_path, "chunks") >= 1

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_backup_is_owner_only(self, home, monkeypatch):
        """The DB holds private memory content and the backend keeps it
        0600 — the backup must not be created with the process umask
        (0644 under the common 022), which would leak it (Codex review)."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        result = runner.invoke(cli, ["reset", "-y", "--backup"])
        assert result.exit_code == 0, result.output
        (bak,) = (home / ".memtomem").glob("memtomem.db.pre-reset-*.bak")
        assert (bak.stat().st_mode & 0o777) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="'?' is not a legal NTFS filename char")
    def test_uri_delimiter_chars_in_db_name(self, home, monkeypatch):
        """A DB path containing ``?``/``#`` must be percent-encoded in the
        sqlite URI — raw interpolation opened a different, truncated path,
        produced an empty 'successful' backup, and let the probe miss a
        live writer on the real file (Codex review)."""
        from memtomem.cli._db_lock import check_db_lock

        state = home / ".memtomem"
        state.mkdir(parents=True)
        db_path = state / "we?ird#1.db"
        seed = sqlite3.connect(db_path)
        try:
            seed.execute("CREATE TABLE marker (x INTEGER)")
            seed.execute("INSERT INTO marker VALUES (7)")
            seed.commit()

            # Probe sees the real file: a held writer is detected...
            seed.execute("BEGIN IMMEDIATE")
            assert check_db_lock(db_path).locked is True
            seed.rollback()
        finally:
            seed.close()
        # ...and the backup reads the real file, not a truncated sibling.
        assert check_db_lock(db_path).locked is False
        bak = reset_cmd._backup_db(db_path)
        assert _count(bak, "marker") == 1
        stray = [p for p in state.iterdir() if "?" not in p.name and p.suffix == ".db"]
        assert not stray, f"URI mis-parse created a truncated sibling: {stray}"

    def test_backup_never_overwrites_existing_snapshot(self, home, monkeypatch):
        """A timestamp collision must refuse, not silently replace the
        earlier snapshot — sqlite3.connect alone would open and overwrite
        the existing backup (Codex review). Clock frozen so every attempt
        collides."""
        from datetime import datetime as real_datetime

        state = home / ".memtomem"
        state.mkdir(parents=True)
        db_path = state / "memtomem.db"
        seed = sqlite3.connect(db_path)
        seed.execute("CREATE TABLE marker (x INTEGER)")
        seed.execute("INSERT INTO marker VALUES (1)")
        seed.commit()
        seed.close()

        class _FrozenDatetime:
            @staticmethod
            def now() -> real_datetime:
                return real_datetime(2026, 7, 3, 12, 0, 0, 123456)

        monkeypatch.setattr(reset_cmd, "datetime", _FrozenDatetime)
        first = reset_cmd._backup_db(db_path)
        assert _count(first, "marker") == 1
        original_bytes = first.read_bytes()

        with pytest.raises(OSError, match="could not reserve"):
            reset_cmd._backup_db(db_path)
        assert first.read_bytes() == original_bytes, "existing snapshot was clobbered"


# ------------------------------------------------- registry gate (#1945)


@pytest.fixture
def reg():
    """The instance-registry module, isolated per test by the autouse
    ``_isolated_instance_registry`` conftest fixture — reset never stages
    the runtime dir, so the default tmp anchor is the right one here
    (unlike uninstall's staging tests, which re-anchor it)."""
    import memtomem._instance_registry as reg_module

    return reg_module


def _assert_barrier_free(reg) -> None:
    """Prove the barrier is free from another *process*.

    Same-process re-acquisition is the weaker check (Windows can grant a
    second handle in the owning process), and the autouse fixture sweeps
    leaked holds at teardown — so this must run inside the test, via a
    spawned child.
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    child = ctx.Process(target=_child_try_exclusive_barrier, args=(str(reg.runtime_dir()), q))
    child.start()
    try:
        outcome, detail = q.get(timeout=30)
    finally:
        child.join(timeout=30)
        if child.is_alive():
            child.kill()
            child.join(timeout=30)
    assert outcome == "acquired", f"reset left the barrier held ({detail})"


def _assert_barrier_held(reg) -> None:
    """Prove the barrier is still held, from another *process*."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    child = ctx.Process(target=_child_try_exclusive_barrier, args=(str(reg.runtime_dir()), q))
    child.start()
    try:
        outcome, _detail = q.get(timeout=30)
    finally:
        child.join(timeout=30)
        if child.is_alive():
            child.kill()
            child.join(timeout=30)
    assert outcome == "refused", "an unconfirmed close must retain the barrier"


@contextlib.contextmanager
def _shared_barrier_holder(reg):
    """A spawned process standing in for a live server (shared hold)."""
    ctx = mp.get_context("spawn")
    q, release = ctx.Queue(), ctx.Event()
    holder = ctx.Process(
        target=_child_hold_shared_barrier, args=(str(reg.runtime_dir()), q, release)
    )
    holder.start()
    try:
        assert q.get(timeout=30)[0] == "held"
        yield
    finally:
        release.set()
        holder.join(timeout=30)
        if holder.is_alive():
            holder.kill()
            holder.join(timeout=30)


class TestResetRegistryGate:
    """LIVE/UNKNOWN/UNTRUSTED registry evidence refuses unconditionally
    (#1935, #1945) — the same blind-spot closure as uninstall's gate: a
    *secondary* server owns no ``server.pid`` and an *idle* server holds
    no SQLite write lock, so only the sentinel flock proves it is alive.
    """

    def test_live_sentinel_refuses(self, home, reg):
        entry = _seed_sentinel(reg)
        with _hold_pid_lock(entry):
            result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code == 2, result.output
        assert "live memtomem-server instance is registered" in result.output

    def test_live_sentinel_refuses_despite_force(self, home, reg):
        """``--force`` covers the stale-pid heuristics, not positive
        liveness — and the refusal must not advertise an override that
        does not apply."""
        entry = _seed_sentinel(reg)
        with _hold_pid_lock(entry):
            result = CliRunner().invoke(cli, ["reset", "-y", "--force"])
        assert result.exit_code == 2, result.output
        assert "--force does not override" in result.output
        assert "pass --force" not in result.output

    def test_unrelated_store_registration_refuses(self, home, reg):
        """The accepted user-wide scope (#1945 design gate): the probe is
        per-user, not per-store, so a live server on a store reset would
        never touch still refuses. Fail-closed is the deliberate
        trade-off — there is no store-scoped fail-closed probe."""
        other_store = home / "some-unrelated-store.db"
        other_store.write_bytes(b"")  # the identity digest needs a regular file
        inst = reg.register_instance(other_store)
        assert inst is not None, "in-process registration should succeed"
        try:
            result = CliRunner().invoke(cli, ["reset", "-y"])
        finally:
            inst.cleanup()
        assert result.exit_code == 2, result.output
        assert "live memtomem-server instance is registered" in result.output

    def test_unknown_refuses_with_retry_advice(self, home, monkeypatch):
        """Transient cause → retry wording, and the persistent cause's
        advice must not leak in (#1942 split, mirrored for reset)."""
        monkeypatch.setattr(
            reset_cmd, "_probe_registry_liveness", lambda: UninstallProbeResult("UNKNOWN")
        )
        result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code == 2, result.output
        assert "Retry in a moment" in result.output
        assert "Remove or repair" not in result.output

    def test_untrusted_refuses_naming_path(self, home, reg):
        """Persistent cause → name the offending path and prescribe the
        repair; retrying provably cannot succeed, so the transient
        advice must not appear (#1942 split, mirrored for reset)."""
        reg.ensure_runtime_dir()
        try:
            reg.instances_dir().symlink_to(home / "nowhere")
        except OSError:
            pytest.skip("symlinks unavailable")
        result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code == 2, result.output
        assert str(reg.instances_dir()) in result.output, "refusal must name the path"
        assert "Remove or repair" in result.output
        assert "Retry in a moment" not in result.output

    def test_registry_refusal_json_shape(self, home, monkeypatch):
        """Registry refusals keep the write-command JSON contract: exit 1
        with ``ok: false`` on stdout, not the text path's exit 2."""
        monkeypatch.setattr(
            reset_cmd, "_probe_registry_liveness", lambda: UninstallProbeResult("LIVE")
        )
        result = CliRunner().invoke(cli, ["reset", "-y", "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "--force does not override" in data["reason"]


# ---------------------------------------------- lifecycle barrier (#1945)


class TestResetLifecycleBarrier:
    """A server holding the barrier blocks reset even when nothing else
    can see it (#1936, #1945).

    Deliberately seeds **no sentinel**: that isolates the barrier from
    the registry gate, and it is the real-world case the lifetime hold
    exists for — a server whose ``register_instance`` failed has an open
    store that nothing advertises.
    """

    def test_shared_holder_refuses(self, home, reg, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        with _shared_barrier_holder(reg):
            assert reg.probe_all_for_uninstall().state == "NONE", (
                "no sentinel — registry sees nothing"
            )
            result = runner.invoke(cli, ["reset", "-y"])

        assert result.exit_code == 2, result.output
        assert "lifecycle barrier" in result.output
        assert _count(db_path, "chunks") >= 1, "refusal must leave the DB untouched"

    def test_force_does_not_override_the_barrier(self, home, reg, monkeypatch):
        """A held flock is never stale — the kernel releases it when its
        holder dies — so there is nothing for ``--force`` to override,
        and the output must not suggest otherwise."""
        _patch_liveness(monkeypatch)
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        with _shared_barrier_holder(reg):
            result = CliRunner().invoke(cli, ["reset", "-y", "--force"])

        assert result.exit_code == 2, result.output
        assert "--force" not in result.output

    def test_barrier_refusal_json_shape(self, home, reg, monkeypatch):
        _patch_liveness(monkeypatch)
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        with _shared_barrier_holder(reg):
            result = CliRunner().invoke(cli, ["reset", "-y", "--json"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "lifecycle barrier" in data["reason"]

    def test_server_start_during_prompt_refuses(self, home, reg, monkeypatch):
        """The #1945 headline race: the confirmation prompt can be sat on
        for minutes, and a server that starts meanwhile holds the barrier
        shared for its lifetime — Phase B must surface it as a refusal,
        not wipe the store it just opened."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        ctx = mp.get_context("spawn")
        q, release = ctx.Queue(), ctx.Event()
        holder = ctx.Process(
            target=_child_hold_shared_barrier, args=(str(reg.runtime_dir()), q, release)
        )

        def confirm_and_start_server(*_a, **_k) -> bool:
            holder.start()
            assert q.get(timeout=30)[0] == "held"
            return True

        monkeypatch.setattr(reset_cmd, "_confirm", confirm_and_start_server)
        try:
            result = runner.invoke(cli, ["reset"])
        finally:
            release.set()
            if holder.pid is not None:
                holder.join(timeout=30)
                if holder.is_alive():
                    holder.kill()
                    holder.join(timeout=30)

        assert result.exit_code == 2, result.output
        assert "lifecycle barrier" in result.output
        assert _count(db_path, "chunks") >= 1, "the prompt-window server's store was wiped"

    def test_wipe_happens_under_the_barrier(self, home, reg, monkeypatch):
        """The positive half: a *real* reset parked inside ``reset_all``
        must still be holding the barrier — a run that dropped it after
        the re-probe would pass every release assertion and reopen the
        race."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)
        rt = str(reg.ensure_runtime_dir())

        ctx = mp.get_context("spawn")
        q, release = ctx.Queue(), ctx.Event()
        worker = ctx.Process(
            target=_child_reset_parked_inside, args=(str(home), rt, "reset_all", q, release)
        )
        worker.start()
        try:
            assert q.get(timeout=60)[0] == "parked", "reset never reached reset_all"
            # Parked inside the wipe, still holding: a server starting
            # now must be refused before it can open the store.
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
        finally:
            release.set()
            worker.join(timeout=60)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=30)
        assert q.get(timeout=30) == ("done", 0)
        assert _count(db_path, "chunks") == 0

    def test_phase_a_hold_spans_the_backend_lifetime(self, home, reg, monkeypatch):
        """Codex round-2 pin: releasing right after the Phase A re-probe
        would leave every other test green — prove the hold covers the
        open backend by parking a real reset inside ``get_stats`` and
        failing a cross-process shared acquire."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        rt = str(reg.ensure_runtime_dir())

        ctx = mp.get_context("spawn")
        q, release = ctx.Queue(), ctx.Event()
        worker = ctx.Process(
            target=_child_reset_parked_inside, args=(str(home), rt, "get_stats", q, release)
        )
        worker.start()
        try:
            assert q.get(timeout=60)[0] == "parked", "reset never reached get_stats"
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
        finally:
            release.set()
            worker.join(timeout=60)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=30)
        assert q.get(timeout=30) == ("done", 0)

    def test_backend_closed_before_prompt(self, home, reg, monkeypatch):
        """The design-gate blocker made concrete: an open handle carried
        across the prompt would let an uninstall stage the DB out from
        under it — Phase A must confirm its close before ``_confirm``
        runs."""
        from memtomem.storage.sqlite_backend import SqliteBackend

        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        events: list[str] = []
        real_close = SqliteBackend.close

        async def spying_close(self):
            events.append("close")
            return await real_close(self)

        monkeypatch.setattr(SqliteBackend, "close", spying_close)

        def confirm_probe(*_a, **_k) -> bool:
            events.append("confirm")
            return False

        monkeypatch.setattr(reset_cmd, "_confirm", confirm_probe)
        result = runner.invoke(cli, ["reset"])
        assert result.exit_code == 0, result.output
        assert events == ["close", "confirm"], events

    def test_barrier_free_during_prompt(self, home, reg, monkeypatch):
        """Nothing may be held while the user sits on the prompt (#1936's
        rejected shape) — proven by a spawned exclusive acquire from
        inside ``_confirm``."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        seen: dict[str, bool] = {}

        def confirm_probe(*_a, **_k) -> bool:
            try:
                _assert_barrier_free(reg)
                seen["free"] = True
            except AssertionError:
                seen["free"] = False
            return False

        monkeypatch.setattr(reset_cmd, "_confirm", confirm_probe)
        result = runner.invoke(cli, ["reset"])
        assert result.exit_code == 0, result.output
        assert seen.get("free") is True, "barrier still held while prompting"

    def test_infrastructure_oserror_prescribes_repair(self, home, monkeypatch):
        """A direct ``OSError`` from acquisition is infrastructure
        (unusable runtime dir, barrier-file permissions), not contention
        — "stop it and re-run" would send the user hunting for a process
        that does not exist (#1870)."""
        _patch_liveness(monkeypatch)

        def broken_acquire(timeout_s: float | None = None):
            raise PermissionError("lifecycle.lock: permission denied")

        monkeypatch.setattr(reset_cmd, "_acquire_lifecycle_barrier", broken_acquire)
        result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code == 2, result.output
        assert "Repair the reported path" in result.output
        assert "Stop it and re-run" not in result.output
        assert "--force" not in result.output


class TestResetRevalidatesStoreIdentity:
    """Consent is for the file counted in Phase A (#1945, Codex round 3).
    A database removed or swapped at the same path during the prompt must
    be refused under the Phase B barrier — not wiped on stale consent,
    and not resurrected after a racing uninstall removed it."""

    def _prompt_mutates_db(self, monkeypatch, action) -> None:
        """Run ``action()`` from inside ``_confirm``, then confirm."""

        def confirm_and_mutate(*_a, **_k) -> bool:
            action()
            return True

        monkeypatch.setattr(reset_cmd, "_confirm", confirm_and_mutate)

    def test_removed_during_prompt_refuses(self, home, reg, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        self._prompt_mutates_db(monkeypatch, lambda: db_path.unlink())
        result = runner.invoke(cli, ["reset"])

        assert result.exit_code == 2, result.output
        assert "removed" in result.output
        assert "different database" in result.output
        # initialize() must not have resurrected it.
        assert not db_path.exists(), "reset recreated a database removed during the prompt"

    def test_replaced_during_prompt_refuses(self, home, reg, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        def swap() -> None:
            # A *different* file at the same path — new inode, new digest.
            db_path.unlink()
            replacement = sqlite3.connect(db_path)
            replacement.execute("CREATE TABLE chunks (id INTEGER)")
            replacement.execute("INSERT INTO chunks VALUES (1), (2), (3)")
            replacement.commit()
            replacement.close()

        self._prompt_mutates_db(monkeypatch, swap)
        result = runner.invoke(cli, ["reset"])

        assert result.exit_code == 2, result.output
        assert "replaced" in result.output
        # The swapped-in database was NOT wiped on the old consent.
        assert _count(db_path, "chunks") == 3, "reset wiped a replacement DB on stale consent"

    def test_unchanged_db_proceeds(self, home, reg, monkeypatch):
        """The revalidation must not false-refuse the ordinary path: an
        untouched DB has the same identity and resets normally."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        result = runner.invoke(cli, ["reset", "-y"])

        assert result.exit_code == 0, result.output
        assert _count(db_path, "chunks") == 0

    def test_identity_refusal_json_shape(self, home, reg, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)

        self._prompt_mutates_db(monkeypatch, lambda: db_path.unlink())
        result = runner.invoke(cli, ["reset", "--json"])

        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "different database" in data["reason"]

    def test_none_fingerprints_fail_closed(self, home, reg, monkeypatch):
        """A ``None`` fingerprint on either side means 'cannot confirm
        same file' — it must refuse, never match by absence
        (``None == None``), which would silently disable the gate."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        monkeypatch.setattr(reset_cmd, "_store_fingerprint", lambda _p: None)

        result = runner.invoke(cli, ["reset", "-y"])

        assert result.exit_code == 2, result.output
        assert "different database" in result.output

    def test_inode_reuse_replacement_refuses(self, home, reg, monkeypatch):
        """The #1945 round-4 hardening at the wiring level: a fingerprint
        change between Phase A and Phase B refuses even when it shares the
        inode. Fabricated same-dev/ino tuples differing in size stand in
        for an inode-reuse swap the comparison must still catch."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)
        seq = iter([(7, 42, 100, 1000), (7, 42, 200, 2000)])  # same dev+ino
        monkeypatch.setattr(reset_cmd, "_store_fingerprint", lambda _p: next(seq))

        result = runner.invoke(cli, ["reset", "-y"])

        assert result.exit_code == 2, result.output
        assert "replaced" in result.output
        assert _count(db_path, "chunks") >= 1, "an inode-reuse swap was wiped"

    def test_fingerprint_carries_size_and_mtime(self, home):
        """The fingerprint FUNCTION must include more than (dev, ino):
        an in-place rewrite keeps the inode but changes size/mtime, and
        that has to move the fingerprint or an inode-reuse swap slips
        through. Exercises the real helper, so dropping the extra fields
        (round-4 mutation) is caught here rather than behind a mock."""
        state = home / ".memtomem"
        state.mkdir(parents=True)
        f = state / "fp.db"
        f.write_bytes(b"a" * 10)
        first = reset_cmd._store_fingerprint(f)
        assert first is not None
        st = os.stat(f)
        # In-place rewrite: same path, same inode, larger + newer.
        with open(f, "wb") as fh:
            fh.write(b"b" * 4096)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        second = reset_cmd._store_fingerprint(f)
        assert second is not None
        assert os.stat(f).st_ino == st.st_ino, "test premise: inode unchanged"
        assert first != second, "fingerprint ignores size/mtime — inode reuse slips through"


class TestResetAlwaysReleasesTheBarrier:
    """Every exit path frees the barrier — proven via a spawned child's
    exclusive acquire *inside the test* (see ``_assert_barrier_free``)."""

    def test_released_after_successful_wipe(self, home, reg, monkeypatch):
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)

        result = runner.invoke(cli, ["reset", "-y"])

        assert result.exit_code == 0, result.output
        _assert_barrier_free(reg)

    def test_released_after_empty_db_early_return(self, home, reg, monkeypatch):
        """The Phase A hold spans initialize + stats + close; the
        already-empty early return must not skip its ``finally``."""
        _patch_liveness(monkeypatch)

        result = CliRunner().invoke(cli, ["reset", "-y"])

        assert result.exit_code == 0, result.output
        assert "already empty" in result.output
        _assert_barrier_free(reg)

    def test_released_after_boundary_refusal(self, home, reg, monkeypatch):
        """The under-barrier re-probe exits with ``SystemExit`` from
        inside the held region — the ``finally`` must still run."""
        _patch_liveness(monkeypatch)
        calls: list[int] = []

        def flapping_probe() -> UninstallProbeResult:
            calls.append(1)
            return UninstallProbeResult("NONE" if len(calls) == 1 else "LIVE")

        monkeypatch.setattr(reset_cmd, "_probe_registry_liveness", flapping_probe)

        result = CliRunner().invoke(cli, ["reset", "-y"])

        assert result.exit_code == 2, result.output
        assert len(calls) == 2, "the Phase A hold must re-probe, not reuse the snapshot"
        _assert_barrier_free(reg)

    def test_phase_a_reprobe_fires_before_initialize(self, home, reg, monkeypatch):
        """Registry evidence appearing between the un-barriered pass and
        the Phase A hold must refuse *before* ``initialize()`` writes —
        a refusal that only fired at Phase B would already have created
        (and possibly migrated) the DB."""
        _patch_liveness(monkeypatch)
        calls: list[int] = []

        def flapping_probe() -> UninstallProbeResult:
            calls.append(1)
            return UninstallProbeResult("NONE" if len(calls) == 1 else "LIVE")

        monkeypatch.setattr(reset_cmd, "_probe_registry_liveness", flapping_probe)

        result = CliRunner().invoke(cli, ["reset", "-y"])

        assert result.exit_code == 2, result.output
        assert len(calls) == 2
        assert not (home / ".memtomem" / "memtomem.db").exists(), (
            "initialize() ran despite LIVE evidence at the Phase A boundary"
        )

    def test_phase_b_reprobe_fires_before_wipe(self, home, reg, monkeypatch):
        """Registry evidence appearing after Phase A (e.g. while a ``-y``
        run raced a registrar, or during the prompt) must be re-checked
        under the Phase B hold — the wipe may not trust the Phase A
        snapshot."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        db_path = _init_and_index(home, runner)
        calls: list[int] = []

        def flapping_probe() -> UninstallProbeResult:
            calls.append(1)
            return UninstallProbeResult("NONE" if len(calls) <= 2 else "LIVE")

        monkeypatch.setattr(reset_cmd, "_probe_registry_liveness", flapping_probe)

        result = runner.invoke(cli, ["reset", "-y"])

        assert result.exit_code == 2, result.output
        assert len(calls) == 3
        assert _count(db_path, "chunks") >= 1, "the wipe outran the Phase B re-probe"

    def test_released_after_backup_failure(self, home, reg, monkeypatch):
        """A backup abort exits from inside the Phase B hold."""
        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)

        def _boom(_db_path: Path) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(reset_cmd, "_backup_db", _boom)
        result = runner.invoke(cli, ["reset", "-y", "--backup"])

        assert result.exit_code == 1, result.output
        _assert_barrier_free(reg)

    def test_cancelled_acquisition_releases_late_handle(self, reg, monkeypatch):
        """A cancellation that lands while the acquire worker is still
        blocked must not leak the handle the worker returns afterwards —
        the ``settle_shielded_value`` release branch (#1936 contract,
        wired into reset by #1945)."""
        go = threading.Event()
        started = threading.Event()
        real_acquire = reg.acquire_uninstall_lifecycle_barrier

        def slow_acquire(timeout_s: float | None = None):
            started.set()
            go.wait(10)
            return real_acquire(timeout_s=1.0)

        monkeypatch.setattr(reset_cmd, "_acquire_lifecycle_barrier", slow_acquire)

        async def scenario() -> None:
            task = asyncio.ensure_future(reset_cmd._acquire_barrier_settled())
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel("test cancellation")
            go.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        _assert_barrier_free(reg)


class TestResetRetainsBarrierOnUncleanClose:
    """#1936 polarity (Codex round 2): an unconfirmed close leaves a
    possibly-open store — the barrier must keep blocking until process
    exit frees the flock, never be released on faith."""

    def test_phase_a_close_failure_retains(self, home, reg, monkeypatch):
        from memtomem.storage.sqlite_backend import SqliteBackend

        _patch_liveness(monkeypatch)

        async def broken_close(self) -> None:
            raise OSError("close failed")

        monkeypatch.setattr(SqliteBackend, "close", broken_close)
        result = CliRunner().invoke(cli, ["reset", "-y"])
        assert result.exit_code != 0
        _assert_barrier_held(reg)

    def test_phase_b_close_failure_retains(self, home, reg, monkeypatch):
        from memtomem.storage.sqlite_backend import SqliteBackend

        _patch_liveness(monkeypatch)
        runner = CliRunner()
        _init_and_index(home, runner)
        calls: list[int] = []
        real_close = SqliteBackend.close

        async def second_close_fails(self) -> None:
            calls.append(1)
            if len(calls) == 2:
                raise OSError("close failed")
            return await real_close(self)

        monkeypatch.setattr(SqliteBackend, "close", second_close_fails)
        result = runner.invoke(cli, ["reset", "-y"])
        assert result.exit_code != 0
        assert len(calls) == 2, "the failure must land on the Phase B close"
        _assert_barrier_held(reg)


def _child_reset_parked_inside(home_str: str, rt_str: str, method: str, q, release) -> None:
    """Run a *real* ``mm reset -y`` parked inside ``SqliteBackend.<method>``.

    The barrier is only proven useful if reset keeps holding it across
    its write boundaries — a child that merely grabbed the lock would
    pass the same assertions even if production dropped it right after
    the re-probe. ``method`` picks the parking spot: ``get_stats`` sits
    inside the Phase A hold, ``reset_all`` inside Phase B's. The runtime
    dir is injected as the parent's already-resolved path, never
    re-derived from the environment: on Windows ``runtime_dir()``
    ignores ``$XDG_RUNTIME_DIR`` entirely and the two processes would
    land on different barrier files.
    """
    import os

    os.environ["HOME"] = home_str
    os.environ["USERPROFILE"] = home_str  # ``Path.home()`` on Windows

    from click.testing import CliRunner

    import memtomem._instance_registry as _reg
    from memtomem.cli import _bootstrap
    from memtomem.cli import cli as _cli
    from memtomem.storage.sqlite_backend import SqliteBackend

    rt = Path(rt_str)

    def _rt() -> Path:
        return rt

    def _ensure_rt() -> Path:
        rt.mkdir(mode=0o700, parents=True, exist_ok=True)
        return rt

    _reg.runtime_dir = _rt
    _reg.ensure_runtime_dir = _ensure_rt
    _bootstrap._CONFIG_PATH = Path(home_str) / ".memtomem" / "config.json"

    real_method = getattr(SqliteBackend, method)

    async def parked(self, *args, **kwargs):
        q.put(("parked",))
        release.wait(60)
        return await real_method(self, *args, **kwargs)

    setattr(SqliteBackend, method, parked)
    result = CliRunner().invoke(_cli, ["reset", "-y"])
    q.put(("done", result.exit_code))
