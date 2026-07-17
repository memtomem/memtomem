"""Tests for the ``create_tables`` schema-version downgrade fence (#1614).

An older binary opening a DB written by a newer release must be detected
and refused with :class:`SchemaDowngradeError` before any migration touches
user data. Same/older/pre-versioning DBs open unchanged — the fence only
blocks the downgrade direction; additive idempotent migrations remain the
forward mechanism.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from memtomem.config import StorageConfig
from memtomem.errors import SchemaDowngradeError, StorageError
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import SCHEMA_VERSION, create_tables


def _connect_with_vec() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def _seed_schema_version(db: sqlite3.Connection, value: str) -> None:
    """Pre-create ``_memtomem_meta`` with a ``schema_version`` row, as a
    prior ``create_tables`` run of some other release would."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS _memtomem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    db.execute(
        "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES ('schema_version', ?)",
        (value,),
    )
    db.commit()


def _stored_version(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM _memtomem_meta WHERE key = 'schema_version'").fetchone()
    return row[0] if row else None


def _create_tables(db: sqlite3.Connection) -> None:
    """Run ``create_tables`` with dim=8 / provider ``none`` so the #298
    dim-mismatch gate stays out of the way."""
    create_tables(db, MetaManager(lambda: db), 8, "none", "")


class TestDowngradeFence:
    def test_fresh_db_stamps_current_version(self) -> None:
        db = _connect_with_vec()
        try:
            _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_pre_observation_query_history_gets_additive_columns(self) -> None:
        """Quality observation fields migrate without replacing legacy rows."""
        db = _connect_with_vec()
        try:
            db.execute(
                """CREATE TABLE query_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_text TEXT NOT NULL,
                    query_embedding BLOB NOT NULL,
                    result_chunk_ids TEXT NOT NULL,
                    result_scores TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "INSERT INTO query_history "
                "(query_text, query_embedding, result_chunk_ids, result_scores, created_at) "
                "VALUES ('legacy', X'', '[]', '[]', '2026-07-17T00:00:00+00:00')"
            )

            _create_tables(db)

            columns = {row[1] for row in db.execute("PRAGMA table_info(query_history)")}
            assert {"run_id", "observation_json", "result_snapshot_json"} <= columns
            row = db.execute(
                "SELECT query_text, run_id, observation_json, result_snapshot_json "
                "FROM query_history"
            ).fetchone()
            assert row == ("legacy", None, "{}", "[]")
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_pre_versioning_db_passes_and_gets_stamped(self) -> None:
        """Every existing install: meta table exists (legacy embedding keys)
        but has no ``schema_version`` row — must open and get stamped."""
        db = _connect_with_vec()
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS _memtomem_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.executemany(
                "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
                [
                    ("embedding_dimension", "8"),
                    ("embedding_provider", "none"),
                    ("embedding_model", ""),
                ],
            )
            db.commit()
            _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_newer_version_raises_typed_error(self) -> None:
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, str(SCHEMA_VERSION + 1))
            with pytest.raises(SchemaDowngradeError) as excinfo:
                _create_tables(db)
            assert isinstance(excinfo.value, StorageError)
            msg = str(excinfo.value)
            assert str(SCHEMA_VERSION + 1) in msg
            assert str(SCHEMA_VERSION) in msg
            assert "uv tool upgrade memtomem" in msg
            assert "pip install -U memtomem" in msg
        finally:
            db.close()

    def test_newer_version_raises_before_any_migration(self) -> None:
        """The fence must fire before any user-data DDL runs."""
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, str(SCHEMA_VERSION + 1))
            with pytest.raises(SchemaDowngradeError):
                _create_tables(db)
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
            ).fetchone()
            assert row is None
        finally:
            db.close()

    def test_newer_version_not_lowered_by_failed_open(self) -> None:
        """The failure path must never write — the stored (newer) version
        survives the refused open."""
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, str(SCHEMA_VERSION + 1))
            with pytest.raises(SchemaDowngradeError):
                _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION + 1)
        finally:
            db.close()

    def test_equal_version_passes(self) -> None:
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, str(SCHEMA_VERSION))
            _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_older_version_passes_and_bumps(self) -> None:
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, "0")
            _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    @pytest.mark.parametrize("garbage", ["banana", "2abc", "1.9", "+3x", "999banana"])
    def test_garbage_value_warns_migrates_and_restamps(
        self, garbage: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-integer value cannot be a legitimate newer version — warn
        loudly, treat as pre-versioning (migrations run), and restamp with
        the truth. Includes numeric-prefixed garbage, where SQLite ``CAST``
        alone would read the prefix and leave the row unrepaired."""
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, garbage)
            with caplog.at_level(logging.WARNING, logger="memtomem.storage.sqlite_schema"):
                _create_tables(db)
            assert any("schema_version" in r.message for r in caplog.records)
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
            ).fetchone()
            assert row is not None
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_noncanonical_integer_normalized(self) -> None:
        """A non-canonical but parseable integer (e.g. ``'01'``) passes the
        fence and gets rewritten to the canonical current version."""
        db = _connect_with_vec()
        try:
            _seed_schema_version(db, "01")
            _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_create_tables_idempotent_after_stamp(self) -> None:
        db = _connect_with_vec()
        try:
            _create_tables(db)
            _create_tables(db)
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()


class TestSearchFeedbackSchema:
    """Additive #1801 migration: the ``search_feedback`` table and the
    partial→non-partial rebuild of ``idx_query_history_run_id`` (an FK
    parent key must be backed by a non-partial unique index)."""

    @staticmethod
    def _run_id_index_sql(db: sqlite3.Connection) -> str:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_query_history_run_id'"
        ).fetchone()
        assert row is not None
        return row[0]

    def test_fresh_db_creates_feedback_table_and_indexes(self) -> None:
        db = _connect_with_vec()
        try:
            _create_tables(db)
            columns = {row[1] for row in db.execute("PRAGMA table_info(search_feedback)")}
            assert columns == {"id", "run_id", "chunk_id", "judgment", "created_at", "updated_at"}
            unique = db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_search_feedback_run_chunk'"
            ).fetchone()
            assert unique is not None and "UNIQUE" in unique[0]
            assert "WHERE" not in self._run_id_index_sql(db).upper()
            assert _stored_version(db) == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_partial_run_id_index_rebuilt_with_legacy_nulls(self) -> None:
        """A #1800-era DB has the partial unique index and NULL-run_id legacy
        rows; the rebuild must keep those rows and yield a working FK parent."""
        db = _connect_with_vec()
        try:
            db.execute(
                """CREATE TABLE query_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_text TEXT NOT NULL,
                    query_embedding BLOB NOT NULL,
                    result_chunk_ids TEXT NOT NULL,
                    result_scores TEXT NOT NULL,
                    run_id TEXT,
                    observation_json TEXT NOT NULL DEFAULT '{}',
                    result_snapshot_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE UNIQUE INDEX idx_query_history_run_id "
                "ON query_history(run_id) WHERE run_id IS NOT NULL"
            )
            for _ in range(2):  # two NULL run_ids must both survive the rebuild
                db.execute(
                    "INSERT INTO query_history "
                    "(query_text, query_embedding, result_chunk_ids, result_scores, created_at) "
                    "VALUES ('legacy', X'', '[]', '[]', '2026-07-17T00:00:00+00:00')"
                )
            db.execute(
                "INSERT INTO query_history "
                "(query_text, query_embedding, result_chunk_ids, result_scores, run_id, "
                "created_at) VALUES ('run', X'', '[]', '[]', 'run-1', '2026-07-17T00:00:00+00:00')"
            )

            _create_tables(db)

            assert "WHERE" not in self._run_id_index_sql(db).upper()
            assert db.execute("SELECT COUNT(*) FROM query_history").fetchone()[0] == 3

            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "INSERT INTO search_feedback "
                "(run_id, chunk_id, judgment, created_at, updated_at) "
                "VALUES ('run-1', 'c1', 'relevant', '2026-07-17T00:00:00+00:00', "
                "'2026-07-17T00:00:00+00:00')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO search_feedback "
                    "(run_id, chunk_id, judgment, created_at, updated_at) "
                    "VALUES ('no-such-run', 'c1', 'relevant', '2026-07-17T00:00:00+00:00', "
                    "'2026-07-17T00:00:00+00:00')"
                )
            db.execute("DELETE FROM query_history WHERE run_id = 'run-1'")
            assert db.execute("SELECT COUNT(*) FROM search_feedback").fetchone()[0] == 0
        finally:
            db.close()

    def test_rebuild_is_idempotent(self) -> None:
        db = _connect_with_vec()
        try:
            _create_tables(db)
            first = self._run_id_index_sql(db)
            _create_tables(db)
            assert self._run_id_index_sql(db) == first
        finally:
            db.close()


class TestBackendInitialize:
    async def test_backend_initialize_raises_and_leaves_db_untouched(self, tmp_path: Path) -> None:
        """End-to-end: ``SqliteBackend.initialize()`` refuses a newer DB with
        the typed error *before any write* — the fence runs ahead of the
        journal-mode PRAGMAs, so the refused open must not flip the journal
        mode or create WAL sidecar files."""
        cfg = StorageConfig()
        cfg.sqlite_path = tmp_path / "m.db"
        storage = SqliteBackend(cfg, dimension=8)
        await storage.initialize()
        await storage.close()

        db = sqlite3.connect(cfg.sqlite_path)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute(
                "UPDATE _memtomem_meta SET value = ? WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )
            db.commit()
        finally:
            db.close()

        reopened = SqliteBackend(cfg, dimension=8)
        with pytest.raises(SchemaDowngradeError):
            await reopened.initialize()

        assert not Path(str(cfg.sqlite_path) + "-wal").exists()
        db = sqlite3.connect(cfg.sqlite_path)
        try:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert db.execute(
                "SELECT value FROM _memtomem_meta WHERE key = 'schema_version'"
            ).fetchone()[0] == str(SCHEMA_VERSION + 1)
        finally:
            db.close()
