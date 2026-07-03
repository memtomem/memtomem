"""Tests for ``mm reset`` — liveness/DB-lock gates and ``--backup`` (#1574 item 7).

The command is destructive by intent, so the contract under test is the
gate ordering, not the wipe itself (``reset_all`` is covered in
``test_storage_extended.py``):

* server/web liveness and the ``BEGIN IMMEDIATE`` write-lock probe refuse
  BEFORE the storage backend is constructed (``-y`` never bypasses them,
  ``--force`` does);
* ``--backup`` snapshots via the sqlite3 backup API, so WAL-resident
  commits survive — the failure a plain file copy would silently cause;
* a failed backup aborts without wiping.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli import reset_cmd
from memtomem.cli._liveness import ServerState

from .helpers import set_home


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
