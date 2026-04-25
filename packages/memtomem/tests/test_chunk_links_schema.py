"""Schema + back-fill tests for the ``chunk_links`` table.

PR-1 of the ``mem_agent_share`` chunk_links series. Covers:

- Table is created by ``create_tables`` with the expected FK / PK / index
  shape (``planning/mem-agent-share-chunk-links-rfc.md`` §Storage).
- ``ON DELETE SET NULL`` on ``source_id`` keeps the destination chunk
  alive when the source is deleted (matches existing copy-on-share
  durability semantics).
- ``ON DELETE CASCADE`` on ``target_id`` drops the row.
- ``PRIMARY KEY (target_id, link_type)`` enforces one link per (target,
  type) — re-write idempotency lives in ``INSERT OR REPLACE`` (used by
  PR-2's writer) on top of the same key.
- Back-fill populates rows from pre-RFC ``shared-from=<uuid>`` audit
  tags exactly once per database; the second run is a no-op.

The writer / reader Python API ships in PR-2; these tests use raw SQL.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
import sqlite_vec

from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import create_tables


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _initialize(db: sqlite3.Connection) -> None:
    """Run ``create_tables`` with a non-meaningful but valid embedding config."""
    meta = MetaManager(lambda: db)
    create_tables(
        db,
        meta,
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )


def _insert_chunk(
    db: sqlite3.Connection,
    chunk_id: str,
    *,
    namespace: str = "default",
    tags_json: str = "[]",
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
        "tags, created_at, updated_at) "
        "VALUES (?, '', '', '', ?, ?, ?, ?)",
        (chunk_id, namespace, tags_json, now, now),
    )


class TestSchemaShape:
    def test_table_and_indexes_created(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            tbl = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_links'"
            ).fetchone()
            assert tbl is not None, "chunk_links table must exist after create_tables"

            indexes = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='chunk_links'"
                ).fetchall()
            }
            assert "idx_chunk_links_source" in indexes
            assert "idx_chunk_links_namespace" in indexes
        finally:
            db.close()

    def test_create_tables_is_idempotent(self) -> None:
        """A second ``create_tables`` on the same DB must not error."""
        db = _connect()
        try:
            _initialize(db)
            _initialize(db)  # must not raise
        finally:
            db.close()


class TestForeignKeyBehavior:
    def test_delete_source_sets_link_source_id_to_null(self) -> None:
        """``mem_agent_share`` durability: deleting the source chunk leaves
        the destination chunk and the link row, but the link's
        ``source_id`` becomes NULL."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "src-1")
            _insert_chunk(db, "tgt-1", namespace="agent-b")
            db.execute(
                "INSERT INTO chunk_links "
                "(source_id, target_id, link_type, namespace_target, created_at) "
                "VALUES ('src-1', 'tgt-1', 'shared', 'agent-b', '2026-01-01T00:00:00')"
            )
            db.commit()

            db.execute("DELETE FROM chunks WHERE id = 'src-1'")
            db.commit()

            row = db.execute(
                "SELECT source_id, target_id FROM chunk_links WHERE target_id = 'tgt-1'"
            ).fetchone()
            assert row is not None, "link row must survive source delete"
            assert row[0] is None, "source_id must be NULL after source delete"
            assert row[1] == "tgt-1"

            # Destination chunk itself untouched.
            tgt = db.execute("SELECT 1 FROM chunks WHERE id = 'tgt-1'").fetchone()
            assert tgt is not None
        finally:
            db.close()

    def test_delete_target_cascades_link_row(self) -> None:
        """Destination delete drops the link row (CASCADE)."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "src-2")
            _insert_chunk(db, "tgt-2", namespace="agent-b")
            db.execute(
                "INSERT INTO chunk_links "
                "(source_id, target_id, link_type, namespace_target, created_at) "
                "VALUES ('src-2', 'tgt-2', 'shared', 'agent-b', '2026-01-01T00:00:00')"
            )
            db.commit()

            db.execute("DELETE FROM chunks WHERE id = 'tgt-2'")
            db.commit()

            row = db.execute("SELECT 1 FROM chunk_links WHERE target_id = 'tgt-2'").fetchone()
            assert row is None, "link row must cascade-delete with target"
        finally:
            db.close()

    def test_primary_key_uniqueness_per_target_link_type(self) -> None:
        """A second link with the same (target_id, link_type) must conflict."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "src-a")
            _insert_chunk(db, "src-b")
            _insert_chunk(db, "tgt-3")
            db.execute(
                "INSERT INTO chunk_links "
                "(source_id, target_id, link_type, namespace_target, created_at) "
                "VALUES ('src-a', 'tgt-3', 'shared', 'default', '2026-01-01T00:00:00')"
            )
            db.commit()

            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO chunk_links "
                    "(source_id, target_id, link_type, namespace_target, created_at) "
                    "VALUES ('src-b', 'tgt-3', 'shared', 'default', '2026-01-01T00:00:01')"
                )
        finally:
            db.close()


class TestBackfillFromSharedFromTags:
    """One-shot back-fill of pre-RFC share copies."""

    def test_backfill_resolves_existing_source(self) -> None:
        """Source chunk still in DB → link row stores its UUID."""
        db = _connect()
        try:
            # Seed BEFORE create_tables so the back-fill picks them up on
            # the first run. ``chunks`` table is created up-front by an
            # initial ``create_tables`` pass; we then insert pre-existing
            # share copies and clear the back-fill marker so the second
            # ``create_tables`` re-runs the back-fill against seeded rows.
            _initialize(db)
            _insert_chunk(db, "src-keep")
            _insert_chunk(
                db,
                "tgt-keep",
                namespace="agent-b",
                tags_json='["pre-rfc", "shared-from=src-keep"]',
            )
            db.execute("DELETE FROM _memtomem_meta WHERE key='chunk_links_backfill_v1'")
            db.commit()

            _initialize(db)  # second pass triggers back-fill

            row = db.execute(
                "SELECT source_id, link_type, namespace_target "
                "FROM chunk_links WHERE target_id = 'tgt-keep'"
            ).fetchone()
            assert row is not None, "back-fill must populate row for shared-from= tag"
            assert row[0] == "src-keep"
            assert row[1] == "shared"
            assert row[2] == "agent-b"
        finally:
            db.close()

    def test_backfill_stores_null_when_source_unresolvable(self) -> None:
        """Source UUID not in chunks → back-fill writes ``source_id=NULL``."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(
                db,
                "tgt-orphan",
                namespace="agent-b",
                tags_json='["shared-from=src-deleted-long-ago"]',
            )
            db.execute("DELETE FROM _memtomem_meta WHERE key='chunk_links_backfill_v1'")
            db.commit()

            _initialize(db)

            row = db.execute(
                "SELECT source_id FROM chunk_links WHERE target_id = 'tgt-orphan'"
            ).fetchone()
            assert row is not None
            assert row[0] is None, "missing source UUID → NULL source_id"
        finally:
            db.close()

    def test_backfill_skips_chunks_without_shared_from_tag(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "tgt-plain", tags_json='["unrelated", "tag"]')
            db.execute("DELETE FROM _memtomem_meta WHERE key='chunk_links_backfill_v1'")
            db.commit()

            _initialize(db)

            row = db.execute("SELECT 1 FROM chunk_links WHERE target_id = 'tgt-plain'").fetchone()
            assert row is None
        finally:
            db.close()

    def test_backfill_runs_at_most_once_per_database(self) -> None:
        """Idempotency marker prevents re-scanning rows added later."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "src-once")
            _insert_chunk(
                db,
                "tgt-once",
                tags_json='["shared-from=src-once"]',
            )
            db.execute("DELETE FROM _memtomem_meta WHERE key='chunk_links_backfill_v1'")
            db.commit()
            _initialize(db)

            # Marker recorded.
            marker = db.execute(
                "SELECT value FROM _memtomem_meta WHERE key='chunk_links_backfill_v1'"
            ).fetchone()
            assert marker is not None and marker[0] == "done"

            # Add a NEW pre-RFC-shaped chunk after the back-fill ran.
            _insert_chunk(
                db,
                "tgt-late",
                tags_json='["shared-from=src-once"]',
            )
            db.commit()
            _initialize(db)  # third pass — must not re-scan

            late_row = db.execute(
                "SELECT 1 FROM chunk_links WHERE target_id = 'tgt-late'"
            ).fetchone()
            assert late_row is None, (
                "marker must short-circuit the back-fill — chunks added after "
                "the migration go through the (PR-2) writer, not a re-scan."
            )
        finally:
            db.close()

    def test_backfill_handles_corrupt_tags_json(self) -> None:
        """Malformed ``tags`` JSON must not crash the migration."""
        db = _connect()
        try:
            _initialize(db)
            # Insert with garbage in the tags column. The LIKE filter still
            # matches (the substring is present), so the back-fill walks
            # this row and must skip it gracefully.
            _insert_chunk(
                db,
                "tgt-bad",
                tags_json="this is not json shared-from=anything",
            )
            db.execute("DELETE FROM _memtomem_meta WHERE key='chunk_links_backfill_v1'")
            db.commit()

            _initialize(db)  # must not raise

            row = db.execute("SELECT 1 FROM chunk_links WHERE target_id = 'tgt-bad'").fetchone()
            assert row is None
        finally:
            db.close()
