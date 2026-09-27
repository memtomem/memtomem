"""A migration must not strand servers that opened the DB under an older schema (#2564).

The open-time downgrade fence never runs again for a long-lived process, so:

* the migrating side refuses while the instance registry shows another live
  server on the store (those opened it before the stamp moved);
* a running process stops using the DB once another one migrates it past the
  schema this process knows;
* the stamp move is traced in ``_memtomem_meta``.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import click
import pytest

import memtomem._instance_registry as reg
from memtomem import __version__
from memtomem._instance_registry import EnumerationResult, InstanceInfo
from memtomem.cli._errors import raise_cli_error
from memtomem.config import StorageConfig
from memtomem.errors import SchemaDowngradeError, SchemaMigrationBlockedError
from memtomem.storage import schema_peers
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_schema import (
    SCHEMA_MIGRATED_AT_KEY,
    SCHEMA_MIGRATED_BY_KEY,
    SCHEMA_MIGRATED_FROM_KEY,
    SCHEMA_VERSION,
)

_CTX = mp.get_context("spawn")
_OLD = SCHEMA_VERSION - 1


def _config(tmp_path: Path) -> StorageConfig:
    cfg = StorageConfig()
    cfg.sqlite_path = tmp_path / "m.db"
    return cfg


async def _make_old_store(tmp_path: Path) -> StorageConfig:
    """A store as the previous schema left it: older stamp, no newer table."""
    cfg = _config(tmp_path)
    storage = SqliteBackend(cfg, dimension=8)
    await storage.initialize()
    await storage.close()
    db = sqlite3.connect(cfg.sqlite_path)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("DROP TABLE held_sources")
        db.execute("UPDATE _memtomem_meta SET value = ? WHERE key = 'schema_version'", (str(_OLD),))
        db.execute(
            "DELETE FROM _memtomem_meta WHERE key IN (?, ?, ?)",
            (SCHEMA_MIGRATED_AT_KEY, SCHEMA_MIGRATED_FROM_KEY, SCHEMA_MIGRATED_BY_KEY),
        )
        db.commit()
    finally:
        db.close()
    return cfg


def _meta(path: Path) -> dict[str, str]:
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT key, value FROM _memtomem_meta").fetchall())
    finally:
        db.close()


def _assert_untouched(cfg: StorageConfig) -> None:
    """The refused open migrated nothing and did not switch the journal mode."""
    assert not Path(str(cfg.sqlite_path) + "-wal").exists()
    db = sqlite3.connect(cfg.sqlite_path)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute(
            "SELECT value FROM _memtomem_meta WHERE key = 'schema_version'"
        ).fetchone() == (str(_OLD),)
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='held_sources'"
            ).fetchone()
            is None
        )
    finally:
        db.close()


def _peer(pid: int, procid: str = "deadbeef") -> InstanceInfo:
    return InstanceInfo(pid=pid, ppid=1, digest="0" * 16, procid=procid, path=Path("x.lock"))


def _fake_enumeration(monkeypatch, result: EnumerationResult, calls: list | None = None):
    def fake(digest: str) -> EnumerationResult:
        if calls is not None:
            calls.append((digest, threading.get_ident()))
        return result

    monkeypatch.setattr(schema_peers, "enumerate_live_instances", fake)


def _no_enumeration(monkeypatch) -> None:
    def fail(digest: str) -> EnumerationResult:
        raise AssertionError("the registry must not be read when nothing migrates")

    monkeypatch.setattr(schema_peers, "enumerate_live_instances", fail)


class TestMigratingSideRefuses:
    async def test_live_foreign_server_blocks_migration(self, tmp_path, monkeypatch) -> None:
        cfg = await _make_old_store(tmp_path)
        _fake_enumeration(monkeypatch, EnumerationResult((_peer(4242),), True))

        storage = SqliteBackend(cfg, dimension=8)
        with pytest.raises(SchemaMigrationBlockedError) as excinfo:
            await storage.initialize()

        message = str(excinfo.value)
        assert "pid 4242" in message
        assert f"schema version {_OLD}" in message
        assert f"to schema version {SCHEMA_VERSION}" in message
        _assert_untouched(cfg)

    async def test_incomplete_registry_read_blocks_migration(self, tmp_path, monkeypatch) -> None:
        cfg = await _make_old_store(tmp_path)
        _fake_enumeration(monkeypatch, EnumerationResult((), False))

        storage = SqliteBackend(cfg, dimension=8)
        with pytest.raises(SchemaMigrationBlockedError, match="could not be fully read"):
            await storage.initialize()
        _assert_untouched(cfg)

    async def test_own_registration_does_not_block(self, tmp_path, monkeypatch) -> None:
        cfg = await _make_old_store(tmp_path)
        own = _peer(os.getpid(), procid=reg.current_process_id())
        _fake_enumeration(monkeypatch, EnumerationResult((own,), True))

        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()
        assert _meta(cfg.sqlite_path)["schema_version"] == str(SCHEMA_VERSION)

    async def test_no_peers_migrates(self, tmp_path, monkeypatch) -> None:
        cfg = await _make_old_store(tmp_path)
        calls: list = []
        _fake_enumeration(monkeypatch, EnumerationResult((), True), calls)

        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()
        assert _meta(cfg.sqlite_path)["schema_version"] == str(SCHEMA_VERSION)
        # The registry walk takes a bounded cross-process lock: never on the loop.
        assert len(calls) == 1
        assert calls[0][1] != threading.get_ident()

    async def test_current_stamp_never_reads_registry(self, tmp_path, monkeypatch) -> None:
        cfg = _config(tmp_path)
        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()

        _no_enumeration(monkeypatch)
        reopened = SqliteBackend(cfg, dimension=8)
        await reopened.initialize()
        await reopened.close()

    async def test_fresh_db_never_reads_registry(self, tmp_path, monkeypatch) -> None:
        _no_enumeration(monkeypatch)
        storage = SqliteBackend(_config(tmp_path), dimension=8)
        await storage.initialize()
        await storage.close()

    async def test_peer_that_migrated_meanwhile_is_not_older(self, tmp_path, monkeypatch) -> None:
        """A same-version peer stamps, then registers, while we enumerate.

        Its registration is visible, but the stamp re-read after enumerating is
        already current, so it must not be mistaken for an older server.
        """
        cfg = await _make_old_store(tmp_path)

        def migrate_then_register(digest: str) -> EnumerationResult:
            db = sqlite3.connect(cfg.sqlite_path)
            try:
                db.execute(
                    "UPDATE _memtomem_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                db.commit()
            finally:
                db.close()
            return EnumerationResult((_peer(4242),), True)

        monkeypatch.setattr(schema_peers, "enumerate_live_instances", migrate_then_register)
        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()

    async def test_real_registration_in_another_process_blocks(self, tmp_path) -> None:
        cfg = await _make_old_store(tmp_path)
        q = _CTX.Queue()
        release = _CTX.Event()
        child = _CTX.Process(
            target=_child_register_hold,
            args=(str(reg.runtime_dir()), str(cfg.sqlite_path), q, release),
        )
        child.start()
        try:
            _, registered, child_pid = _drain_until(q, "registered")
            assert registered

            storage = SqliteBackend(cfg, dimension=8)
            with pytest.raises(SchemaMigrationBlockedError) as excinfo:
                await storage.initialize()
            assert f"pid {child_pid}" in str(excinfo.value)
            _assert_untouched(cfg)
        finally:
            release.set()
            child.join(timeout=30)
            if child.is_alive():
                child.kill()
                child.join(timeout=30)

    def test_cli_hint_names_the_older_server_not_a_newer_release(self) -> None:
        with pytest.raises(click.ClickException) as excinfo:
            raise_cli_error(SchemaMigrationBlockedError("blocked"))
        hint = excinfo.value.message
        assert "older memtomem server" in hint
        assert "written by a newer memtomem" not in hint


def _child_register_hold(rt_str: str, db_str: str, q, release) -> None:
    import memtomem._instance_registry as _reg

    target = Path(rt_str)
    _reg.runtime_dir = lambda: target  # type: ignore[assignment]

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    _reg.ensure_runtime_dir = _ensure  # type: ignore[assignment]
    inst = _reg.register_instance(Path(db_str))
    q.put(("registered", inst is not None, os.getpid()))
    release.wait(60)
    if inst is not None:
        inst.cleanup()


def _drain_until(q, tag: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = q.get(timeout=1.0)
        except Exception:
            continue
        if msg[0] == tag:
            return msg
    raise AssertionError(f"child never reported {tag!r}")


def _migrate_past(path: Path) -> None:
    """Another process moves the stamp beyond what this binary knows."""
    db = sqlite3.connect(path)
    try:
        db.execute(
            "UPDATE _memtomem_meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        db.commit()
    finally:
        db.close()


def _write_lock_is_free(path: Path) -> bool:
    db = sqlite3.connect(path, timeout=0)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        db.close()


class TestRunningProcessStops:
    @pytest.fixture
    async def storage(self, tmp_path):
        backend = SqliteBackend(_config(tmp_path), dimension=8)
        await backend.initialize()
        yield backend
        await backend.close()

    async def test_writer_and_reader_refuse_after_migration(self, storage) -> None:
        storage._get_db()
        storage._get_read_db()
        _migrate_past(storage._config.sqlite_path)

        with pytest.raises(SchemaDowngradeError, match="restart it on the newer release"):
            storage._get_db()
        with pytest.raises(SchemaDowngradeError):
            await storage.get_pending_source_checks()

    async def test_read_pool_refuses_on_its_own(self, storage) -> None:
        _migrate_past(storage._config.sqlite_path)
        with pytest.raises(SchemaDowngradeError):
            storage._get_read_db()

    async def test_refusal_is_sticky(self, storage) -> None:
        path = storage._config.sqlite_path
        _migrate_past(path)
        with pytest.raises(SchemaDowngradeError):
            storage._get_db()

        db = sqlite3.connect(path)
        try:
            db.execute(
                "UPDATE _memtomem_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION),),
            )
            db.commit()
        finally:
            db.close()
        with pytest.raises(SchemaDowngradeError):
            storage._get_db()

    async def test_transaction_rechecks_under_the_write_lock(self, storage) -> None:
        """The migration lands after the handout check but before the lock.

        The handout memo is forced to the post-migration ``data_version`` so
        only the check inside ``transaction()`` can see it; its refusal must
        not strand the write lock.
        """
        db = storage._get_db()
        path = storage._config.sqlite_path
        _migrate_past(path)
        storage._schema_checked_at[id(db)] = db.execute("PRAGMA data_version").fetchone()[0]

        with pytest.raises(SchemaDowngradeError):
            async with storage.transaction():
                pytest.fail("the block must not run on a superseded schema")
        assert not db.in_transaction
        assert _write_lock_is_free(path)

    async def test_watcher_journal_connection_refuses(self, storage, tmp_path) -> None:
        _migrate_past(storage._config.sqlite_path)
        with pytest.raises(SchemaDowngradeError):
            storage.queue_source_check_sync(tmp_path / "gone.md")
        assert _write_lock_is_free(storage._config.sqlite_path)

    async def test_fts_rebuild_connection_refuses(self, storage) -> None:
        _migrate_past(storage._config.sqlite_path)
        # The handout memo would refuse first; clear it to reach the worker's own check.
        db = storage._db
        storage._schema_checked_at[id(db)] = db.execute("PRAGMA data_version").fetchone()[0]
        with pytest.raises(SchemaDowngradeError):
            await storage.rebuild_fts()

    async def test_unchanged_stamp_keeps_working(self, storage) -> None:
        """Another process's ordinary commit re-reads the stamp and passes."""
        path = storage._config.sqlite_path
        storage._get_db()
        db = sqlite3.connect(path)
        try:
            db.execute("INSERT INTO _memtomem_meta(key, value) VALUES ('probe', '1')")
            db.commit()
        finally:
            db.close()
        storage._get_db()
        storage._get_read_db()


class TestMigrationTrace:
    async def test_migration_records_when_from_and_by(self, tmp_path, monkeypatch) -> None:
        cfg = await _make_old_store(tmp_path)
        _fake_enumeration(monkeypatch, EnumerationResult((), True))
        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()

        meta = _meta(cfg.sqlite_path)
        assert meta[SCHEMA_MIGRATED_FROM_KEY] == str(_OLD)
        assert meta[SCHEMA_MIGRATED_BY_KEY] == __version__
        assert datetime.fromisoformat(meta[SCHEMA_MIGRATED_AT_KEY]).tzinfo is not None

        reopened = SqliteBackend(cfg, dimension=8)
        await reopened.initialize()
        await reopened.close()
        assert _meta(cfg.sqlite_path) == meta

    async def test_new_store_records_creation(self, tmp_path) -> None:
        cfg = _config(tmp_path)
        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()
        assert _meta(cfg.sqlite_path)[SCHEMA_MIGRATED_FROM_KEY] == "none"
