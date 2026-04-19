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
