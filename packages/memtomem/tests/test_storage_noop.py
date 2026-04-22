"""Tests for SqliteBackend operating in BM25-only mode (dimension=0).

Regression coverage for the bug where fresh ``mm init --provider none``
created a schema without ``chunks_vec`` (NoopEmbedder dim=0), and every
subsequent ``upsert_chunks`` / ``delete_*`` crashed with
``no such table: chunks_vec`` because the writers were unconditional.

The fix gates every chunks_vec touch on ``SqliteBackend._has_vec_table``,
which is set from a one-time ``sqlite_master`` probe in ``initialize()``
and updated by ``reset_embedding_meta`` / ``reset_all``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import make_chunk
from memtomem.config import StorageConfig
from memtomem.storage.sqlite_backend import SqliteBackend


@pytest.fixture
async def noop_backend(tmp_path):
    """A SqliteBackend initialized with ``dimension=0, provider='none'``."""
    cfg = StorageConfig(sqlite_path=tmp_path / "noop.db")
    backend = SqliteBackend(
        config=cfg,
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )
    await backend.initialize()
    yield backend
    await backend.close()


def _vec_table_exists(backend: SqliteBackend) -> bool:
    db = backend._get_db()
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        is not None
    )


class TestUpsertNoopMode:
    @pytest.mark.asyncio
    async def test_upsert_chunks_noop_embedder_no_vec_table(self, noop_backend):
        """Fresh dim=0 init: chunks_vec absent, but writes + reads still work."""
        # Sanity: the schema gate left chunks_vec unbuilt.
        assert _vec_table_exists(noop_backend) is False
        assert noop_backend._has_vec_table is False

        chunks = [
            make_chunk("hello world from python", embedding=[]),
            make_chunk("another chunk about retrieval", embedding=[]),
        ]
        await noop_backend.upsert_chunks(chunks)

        # Read paths
        for c in chunks:
            assert (await noop_backend.get_chunk(c.id)) is not None

        # BM25 search still returns the indexed content.
        results = await noop_backend.bm25_search("hello", top_k=5)
        assert len(results) >= 1
        assert any("hello world" in r.chunk.content for r in results)

        # Read-path pin: get_embeddings_for_chunks early-returns {} without
        # raising OperationalError when chunks_vec is absent.
        embeddings = await noop_backend.get_embeddings_for_chunks([str(c.id) for c in chunks])
        assert embeddings == {}

        # Dense search read path: returns [] instead of crashing the pipeline.
        dense = await noop_backend.dense_search([0.1, 0.2, 0.3], top_k=5)
        assert dense == []


class TestDeleteNoopMode:
    @pytest.mark.asyncio
    async def test_delete_paths_noop_mode(self, noop_backend):
        """delete_chunks / delete_by_source / delete_by_namespace must not crash."""
        c1 = make_chunk("first", source="a.md", namespace="ns-a", embedding=[])
        c2 = make_chunk("second", source="b.md", namespace="ns-b", embedding=[])
        c3 = make_chunk("third", source="b.md", namespace="ns-b", embedding=[])
        await noop_backend.upsert_chunks([c1, c2, c3])

        # delete by id
        n = await noop_backend.delete_chunks([c1.id])
        assert n == 1

        # delete by source (removes c2 and c3)
        n = await noop_backend.delete_by_source(Path("/tmp/b.md"))
        assert n == 2

        # delete_by_namespace: exercise both populated and empty-namespace paths.
        c4 = make_chunk("fourth", namespace="ns-c", embedding=[])
        await noop_backend.upsert_chunks([c4])
        n = await noop_backend.delete_by_namespace("ns-c")
        assert n == 1
        # No-op path: namespace with no rows must return 0 without raising.
        n = await noop_backend.delete_by_namespace("nonexistent-ns")
        assert n == 0


class TestResetFromNoopToReal:
    @pytest.mark.asyncio
    async def test_reset_embedding_meta_creates_vec_table_and_preserves_fts(self, noop_backend):
        """Recovery path: dim=0 → real provider must build chunks_vec, keep FTS,
        and live-flip the NamespaceOps callable."""
        # Seed BM25-only data
        c1 = make_chunk("alpha keyword unique", namespace="reset-ns", embedding=[])
        c2 = make_chunk("beta keyword unique", namespace="reset-ns", embedding=[])
        await noop_backend.upsert_chunks([c1, c2])
        assert noop_backend._has_vec_table is False

        # Reset to a real provider with non-zero dim
        await noop_backend.reset_embedding_meta(dimension=8, provider="onnx", model="test-model")

        # chunks_vec now exists, flag flipped True
        assert _vec_table_exists(noop_backend) is True
        assert noop_backend._has_vec_table is True

        # FTS entries from before the reset still searchable
        results = await noop_backend.bm25_search("alpha", top_k=5)
        assert any("alpha keyword unique" in r.chunk.content for r in results)

        # New chunks with real embeddings upsert without error
        c3 = make_chunk("gamma keyword unique", embedding=[0.1] * 8)
        await noop_backend.upsert_chunks([c3])
        assert (await noop_backend.get_chunk(c3.id)) is not None

        # NamespaceOps callable picks up the live flag flip — delete_by_namespace
        # actually clears chunks_vec rows for the namespace post-reset.
        deleted = await noop_backend.delete_by_namespace("reset-ns")
        assert deleted == 2
