"""Tests for ``storage.sqlite_schema.create_tables`` startup invariants.

Focused on issue #298: a DB with ``embedding_dimension=0`` but a non-``none``
configured embedding provider indicates a legacy NoopEmbedder → real-provider
switch without ``mm embedding-reset``. Without a fail-fast gate, startup
would silently proceed and every subsequent ``upsert_chunks`` would crash
with ``no such table: chunks_vec``.
"""

from __future__ import annotations

import sqlite3

import pytest
import sqlite_vec

from memtomem.errors import EmbeddingDimensionMismatchError
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import create_tables


def _connect_with_vec() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def _seed_legacy_meta(
    db: sqlite3.Connection,
    *,
    stored_dim: int,
    stored_provider: str,
    stored_model: str,
) -> None:
    """Pre-create ``_memtomem_meta`` as a prior ``create_tables`` run would."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS _memtomem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    db.executemany(
        "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
        [
            ("embedding_dimension", str(stored_dim)),
            ("embedding_provider", stored_provider),
            ("embedding_model", stored_model),
        ],
    )
    db.commit()


class TestDim0ProviderMismatch:
    """Startup gate for the dim=0 / real-provider contradiction (#298)."""

    def test_mismatch_raises_by_default(self) -> None:
        """Legacy DB left at dim=0 + configured real provider must fail fast."""
        db = _connect_with_vec()
        try:
            _seed_legacy_meta(db, stored_dim=0, stored_provider="onnx", stored_model="bge-m3")
            meta = MetaManager(lambda: db)
            with pytest.raises(EmbeddingDimensionMismatchError) as excinfo:
                create_tables(
                    db,
                    meta,
                    dimension=1024,
                    embedding_provider="onnx",
                    embedding_model="bge-m3",
                )
            msg = str(excinfo.value)
            assert "embedding_dimension=0" in msg
            assert "mm embedding-reset --mode apply-current" in msg
        finally:
            db.close()

    def test_relaxed_check_allows_mismatch_for_recovery(self) -> None:
        """``mm embedding-reset`` passes strict_dim_check=False so it can
        observe and fix the broken state instead of tripping the gate."""
        db = _connect_with_vec()
        try:
            _seed_legacy_meta(db, stored_dim=0, stored_provider="onnx", stored_model="bge-m3")
            meta = MetaManager(lambda: db)
            effective_dim, _, _ = create_tables(
                db,
                meta,
                dimension=1024,
                embedding_provider="onnx",
                embedding_model="bge-m3",
                strict_dim_check=False,
            )
            assert effective_dim == 0
            vec_row = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
            ).fetchone()
            assert vec_row is None, "chunks_vec must not be created at dim=0"
        finally:
            db.close()

    def test_bm25_only_install_ok(self) -> None:
        """The legit pure-BM25 case (dim=0 + provider=none) must initialize
        cleanly — stored provider is ``none``, so the gate does not trip."""
        db = _connect_with_vec()
        try:
            _seed_legacy_meta(db, stored_dim=0, stored_provider="none", stored_model="")
            meta = MetaManager(lambda: db)
            effective_dim, _, _ = create_tables(
                db,
                meta,
                dimension=0,
                embedding_provider="none",
                embedding_model="",
            )
            assert effective_dim == 0
        finally:
            db.close()

    def test_bm25_upgrade_attempt_without_reset_raises(self) -> None:
        """DB initialized with provider=none, config upgraded to onnx without
        running embedding-reset: stored provider=none, stored dim=0, but
        configured provider=onnx — gate still fires because the configured
        runtime provider is non-``none`` and the stored dim is 0."""
        db = _connect_with_vec()
        try:
            _seed_legacy_meta(db, stored_dim=0, stored_provider="none", stored_model="")
            meta = MetaManager(lambda: db)
            with pytest.raises(EmbeddingDimensionMismatchError):
                create_tables(
                    db,
                    meta,
                    dimension=1024,
                    embedding_provider="onnx",
                    embedding_model="bge-m3",
                )
        finally:
            db.close()

    def test_fresh_install_real_provider_ok(self) -> None:
        """Fresh DB + real provider: no prior meta rows → stored_dim is None,
        falls through to ``meta.store_dimension(configured)``, chunks_vec is
        created with the configured dimension."""
        db = _connect_with_vec()
        try:
            meta = MetaManager(lambda: db)
            effective_dim, dim_mismatch, model_mismatch = create_tables(
                db,
                meta,
                dimension=1024,
                embedding_provider="onnx",
                embedding_model="bge-m3",
            )
            assert effective_dim == 1024
            assert dim_mismatch is None
            assert model_mismatch is None
            vec_row = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
            ).fetchone()
            assert vec_row is not None
        finally:
            db.close()


class TestDuplicateChunksMigration:
    """One-time cleanup of pre-#691 duplicate chunk rows on startup.

    Real-world DBs that ran ``mm web`` watcher + ``mm`` MCP / CLI on the same
    files accumulated rows that share
    ``(namespace, source_file, content_hash, start_line)`` but differ only in
    ``id``. ``create_tables`` must collapse those groups exactly once, then
    install the UNIQUE index so future ``INSERT OR IGNORE`` writes block any
    new ones at the storage layer.
    """

    @staticmethod
    def _seed_dup_rows(db: sqlite3.Connection) -> dict[str, int]:
        # Two identical-hash rows differing only in id, created_at, and access
        # stats. The keeper must be the row with the higher
        # ``access_count + use_count`` — that is the row the differ has been
        # actively reusing across re-indexes; the loser is the silent ghost.
        # Returns ``{chunk_id: rowid}`` so callers can pin sidecar cleanup
        # against the loser's rowid (chunks_fts entries here; no chunks_vec
        # because this test path runs at dimension=0).
        db.executemany(
            """INSERT INTO chunks
               (id, content, content_hash, source_file, namespace,
                start_line, end_line, created_at, updated_at,
                access_count, use_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    "00000000-0000-0000-0000-000000000001",
                    "duplicate body",
                    "hash-X",
                    "/tmp/dup.md",
                    "default",
                    10,
                    20,
                    "2026-04-29T00:00:00+00:00",
                    "2026-04-29T00:00:00+00:00",
                    7,
                    3,
                ),
                (
                    "00000000-0000-0000-0000-000000000002",
                    "duplicate body",
                    "hash-X",
                    "/tmp/dup.md",
                    "default",
                    10,
                    20,
                    "2026-04-30T00:00:00+00:00",
                    "2026-04-30T00:00:00+00:00",
                    0,
                    0,
                ),
            ],
        )
        rowid_by_id = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT id, rowid FROM chunks WHERE content_hash='hash-X'"
            ).fetchall()
        }
        # Mirror the prod write path: every chunks row has a matching
        # chunks_fts row at the same rowid. Without these the negative pin
        # below couldn't tell whether the migration cleaned up the sidecar
        # or whether it was simply never seeded.
        db.executemany(
            "INSERT INTO chunks_fts(rowid, content, source_file) VALUES (?, ?, ?)",
            [(rowid, "duplicate body", "/tmp/dup.md") for rowid in rowid_by_id.values()],
        )
        db.commit()
        return rowid_by_id

    def test_collapses_existing_dup_rows_and_installs_unique_index(self) -> None:
        db = _connect_with_vec()
        try:
            meta = MetaManager(lambda: db)
            # First call sets up schema; on a fresh DB the cleanup loop has
            # nothing to do but the UNIQUE index is created.
            create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")

            # Drop the index so we can simulate an upgrade from a pre-#691
            # DB that already accumulated duplicates.
            db.execute("DROP INDEX idx_chunks_unique_content")
            rowid_by_id = self._seed_dup_rows(db)
            loser_rowid = rowid_by_id["00000000-0000-0000-0000-000000000002"]

            # Second call re-runs create_tables, which now sees the UNIQUE
            # index missing and triggers the cleanup migration.
            create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")

            rows = db.execute(
                "SELECT id, access_count FROM chunks WHERE content_hash='hash-X'"
            ).fetchall()
            assert len(rows) == 1, f"expected 1 row after cleanup, got {len(rows)}"
            kept_id, kept_access = rows[0]
            # Keeper rule: highest (access_count + use_count) wins so we
            # preserve the actively-reused row, not the older ghost.
            assert kept_id == "00000000-0000-0000-0000-000000000001"
            assert kept_access == 7

            # Negative pin (per ``feedback_pin_invert_symmetric_assertion``):
            # asserting the keeper survived isn't enough — the loser must
            # also be gone, in both ``chunks`` and the ``chunks_fts`` sidecar
            # that production keeps in lockstep.
            loser_present = db.execute(
                "SELECT 1 FROM chunks WHERE id='00000000-0000-0000-0000-000000000002'"
            ).fetchone()
            assert loser_present is None, "loser chunk row must be deleted"
            loser_fts = db.execute(
                "SELECT 1 FROM chunks_fts WHERE rowid=?", (loser_rowid,)
            ).fetchone()
            assert loser_fts is None, "loser chunks_fts sidecar row must be deleted"

            idx_row = db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='index' AND name='idx_chunks_unique_content'"
            ).fetchone()
            assert idx_row is not None, "UNIQUE index must be present after migration"
        finally:
            db.close()

    def test_migration_is_idempotent_after_first_run(self) -> None:
        db = _connect_with_vec()
        try:
            meta = MetaManager(lambda: db)
            create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
            # No dups exist; second call must be a no-op (no errors, index stays).
            create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
            idx_row = db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='index' AND name='idx_chunks_unique_content'"
            ).fetchone()
            assert idx_row is not None
        finally:
            db.close()
