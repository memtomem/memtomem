"""Storage scope-column tests for ADR-0011 PR-B.

Three pins:

1. **Schema migration is idempotent.** Adding the columns twice (or
   running ``create_tables`` against a DB that already has them) does
   not raise — mirrors the precedent at ``sqlite_schema.py:80`` for the
   ``namespace`` migration.
2. **Round-trip via ``_row_to_chunk`` preserves scope + project_root.**
   Insert a chunk with ``scope='project_shared'`` + a project_root path
   → fetch it back → ``ChunkMetadata.scope`` and ``project_root`` match.
3. **Legacy DB (no scope columns) still decodes.** A row with the old
   21-column layout decodes to ``scope='user'`` / ``project_root=None``
   without raising — the conditional unpacking in ``_row_to_chunk`` is
   the backward-compatibility boundary.

Schema rollback (ADR-0011 §1) — SQLite ≥3.35 supports ``ALTER TABLE
DROP COLUMN``. Verified inline so a future SQLite floor change surfaces
loudly rather than silently breaking the rollback path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata


@pytest.mark.asyncio
async def test_scope_columns_present_on_fresh_db(storage):
    """Fresh DB CREATE TABLE includes scope + project_root."""
    cols = {row[1] for row in storage._get_db().execute("PRAGMA table_info(chunks)").fetchall()}
    assert "scope" in cols
    assert "project_root" in cols


@pytest.mark.asyncio
async def test_idx_chunks_scope_index_exists(storage):
    """The composite (scope, project_root) index is created."""
    indexes = {
        row[1]
        for row in storage._get_db()
        .execute("SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='chunks'")
        .fetchall()
    }
    assert "idx_chunks_scope" in indexes


@pytest.mark.asyncio
async def test_default_scope_is_user(storage):
    """A chunk written without explicit scope defaults to 'user' / None."""
    chunk = Chunk(
        content="hello",
        metadata=ChunkMetadata(source_file=Path("/tmp/x.md")),
        embedding=[0.1] * 1024,
    )
    await storage.upsert_chunks([chunk])
    fetched = await storage.get_chunk(chunk.id)
    assert fetched is not None
    assert fetched.metadata.scope == "user"
    assert fetched.metadata.project_root is None


@pytest.mark.asyncio
async def test_round_trip_project_shared(storage, tmp_path):
    """project_shared scope + project_root persist through upsert/fetch."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    chunk = Chunk(
        content="team rule X",
        metadata=ChunkMetadata(
            source_file=tmp_path / "myproj" / ".memtomem" / "memories" / "rule.md",
            scope="project_shared",
            project_root=proj,
        ),
        embedding=[0.1] * 1024,
    )
    await storage.upsert_chunks([chunk])
    fetched = await storage.get_chunk(chunk.id)
    assert fetched is not None
    assert fetched.metadata.scope == "project_shared"
    assert fetched.metadata.project_root == proj


@pytest.mark.asyncio
async def test_round_trip_project_local(storage, tmp_path):
    """project_local scope persists; project_root is the project root, not the .local dir."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    chunk = Chunk(
        content="draft note",
        metadata=ChunkMetadata(
            source_file=proj / ".memtomem" / "memories.local" / "draft.md",
            scope="project_local",
            project_root=proj,
        ),
        embedding=[0.1] * 1024,
    )
    await storage.upsert_chunks([chunk])
    fetched = await storage.get_chunk(chunk.id)
    assert fetched is not None
    assert fetched.metadata.scope == "project_local"
    assert fetched.metadata.project_root == proj


@pytest.mark.asyncio
async def test_legacy_row_without_scope_columns_decodes(storage):
    """A row missing scope/project_root (legacy 21-col layout) decodes safely.

    Simulates a DB that was migrated up to validity columns (21 cols) but
    not yet to scope (23 cols) — the ``_row_to_chunk`` conditional
    unpacking must default to user / None instead of crashing.
    """
    chunk_id = str(uuid4())
    db = storage._get_db()
    # Insert directly without scope/project_root values; SQLite supplies
    # defaults from the CREATE TABLE/ALTER TABLE clauses (scope='user',
    # project_root=NULL). Then SELECT only the legacy 21 columns to
    # simulate a fetch from a pre-ALTER schema row.
    db.execute(
        """INSERT INTO chunks
           (id, content, content_hash, source_file, heading_hierarchy, chunk_type,
            start_line, end_line, language, tags, namespace, created_at, updated_at,
            valid_from_unix, valid_to_unix)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            chunk_id,
            "legacy",
            "deadbeef",
            "/tmp/legacy.md",
            "[]",
            "raw_text",
            0,
            10,
            "en",
            "[]",
            "default",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            None,
            None,
        ),
    )
    db.commit()
    # Simulate a legacy SELECT that only fetches 21 columns. ``_row_to_chunk``
    # must default to scope='user' / project_root=None.
    legacy_row = db.execute(
        """SELECT id, content, content_hash, source_file, heading_hierarchy,
                  chunk_type, start_line, end_line, language, tags, namespace,
                  created_at, updated_at,
                  access_count, use_count, last_accessed_at,
                  overlap_before, overlap_after,
                  importance_score,
                  valid_from_unix, valid_to_unix
           FROM chunks WHERE id=?""",
        (chunk_id,),
    ).fetchone()
    assert len(legacy_row) == 21
    chunk = storage._row_to_chunk(legacy_row)
    assert chunk.metadata.scope == "user"
    assert chunk.metadata.project_root is None


def test_alter_drop_column_supported_on_min_sqlite():
    """SQLite floor (Python 3.12 ships ≥3.45) supports ALTER DROP COLUMN.

    Schema rollback path requires this; the test fails loudly if a future
    Python downgrade breaks the contract.
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (a INT, b INT, c INT)")
    db.execute("ALTER TABLE t DROP COLUMN c")  # MUST not raise
    cols = {row[1] for row in db.execute("PRAGMA table_info(t)").fetchall()}
    assert "c" not in cols


def test_schema_migration_is_idempotent(tmp_path):
    """Running create_tables twice on the same DB does not raise.

    Pin for the ``ALTER TABLE ... ADD COLUMN scope`` block — the
    ``duplicate column`` check must catch the second run, mirroring the
    precedent at ``sqlite_schema.py:80`` for the ``namespace`` migration.
    """
    from memtomem.storage.sqlite_meta import MetaManager
    from memtomem.storage.sqlite_schema import create_tables

    db = sqlite3.connect(str(tmp_path / "idem.db"))
    meta = MetaManager(lambda: db)
    # Pass dimension=0 (NoopEmbedder mode) so the test does not require
    # the vec0 extension to be loaded.
    create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
    # Second invocation MUST be a no-op — namespace, validity, scope
    # migrations are all guarded by ``duplicate column`` catch.
    create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
    cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "scope" in cols
    assert "project_root" in cols
