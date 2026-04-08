"""Extended storage tests covering methods not exercised by existing test suite.

Tests vector search (dense_search), FTS rebuild, chunk hashes, embedding
retrieval, access counting, size distribution, and embedding meta reset.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from helpers import make_chunk
from memtomem.models import NamespaceFilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _varied_embedding(seed: float = 0.1, dim: int = 1024) -> list[float]:
    """Return a deterministic but varied embedding vector."""
    return [seed + i * 0.0001 for i in range(dim)]


def _similar_embedding(base: list[float], delta: float = 0.001) -> list[float]:
    """Return an embedding close to *base* (high cosine similarity)."""
    return [v + delta for v in base]


def _distant_embedding(dim: int = 1024) -> list[float]:
    """Return a vector far from the default embeddings."""
    return [0.9 - i * 0.0001 for i in range(dim)]


class TestStorageExtended:
    """Storage backend methods that need additional coverage."""

    # ---- dense_search --------------------------------------------------------

    async def test_dense_search_returns_results(self, components):
        """Insert chunks with known embeddings, search with a similar vector."""
        storage = components.storage
        emb = _varied_embedding(0.2)
        chunk = make_chunk(content="dense search target", embedding=emb)
        await storage.upsert_chunks([chunk])

        results = await storage.dense_search(emb, top_k=5)
        assert len(results) >= 1
        assert results[0].chunk.content == "dense search target"
        assert results[0].source == "dense"

    async def test_dense_search_similar_vector_ranks_higher(self, components):
        """A query embedding close to a chunk should rank it above distant chunks."""
        storage = components.storage
        emb_a = _varied_embedding(0.1)
        emb_b = _distant_embedding()
        chunk_a = make_chunk(content="nearby chunk", embedding=emb_a, source="a.md")
        chunk_b = make_chunk(content="distant chunk", embedding=emb_b, source="b.md")
        await storage.upsert_chunks([chunk_a, chunk_b])

        query = _similar_embedding(emb_a, delta=0.0005)
        results = await storage.dense_search(query, top_k=5)
        assert len(results) == 2
        assert results[0].chunk.content == "nearby chunk"

    async def test_dense_search_respects_top_k(self, components):
        storage = components.storage
        chunks = [
            make_chunk(content=f"chunk {i}", source=f"f{i}.md", embedding=_varied_embedding(0.1 + i * 0.01))
            for i in range(5)
        ]
        await storage.upsert_chunks(chunks)

        results = await storage.dense_search(_varied_embedding(0.1), top_k=2)
        assert len(results) == 2

    async def test_dense_search_namespace_filter(self, components):
        storage = components.storage
        emb = _varied_embedding(0.3)
        chunk_a = make_chunk(content="ns-work", namespace="work", embedding=emb, source="w.md")
        chunk_b = make_chunk(content="ns-personal", namespace="personal",
                            embedding=_similar_embedding(emb), source="p.md")
        await storage.upsert_chunks([chunk_a, chunk_b])

        ns_filter = NamespaceFilter.parse("work")
        results = await storage.dense_search(emb, top_k=10, namespace_filter=ns_filter)
        namespaces = {r.chunk.metadata.namespace for r in results}
        assert "personal" not in namespaces
        assert len(results) >= 1

    async def test_dense_search_empty_db_returns_empty(self, components):
        storage = components.storage
        results = await storage.dense_search([0.1] * 1024, top_k=5)
        assert results == []

    async def test_dense_search_dimension_mismatch_raises(self, components):
        storage = components.storage
        chunk = make_chunk(content="dim check", embedding=[0.1] * 1024)
        await storage.upsert_chunks([chunk])
        with pytest.raises((ValueError, Exception)):
            await storage.dense_search([0.1] * 512, top_k=5)

    # ---- rebuild_fts ---------------------------------------------------------

    async def test_rebuild_fts_preserves_searchability(self, components):
        """After rebuild_fts, BM25 search should still find indexed content."""
        storage = components.storage
        chunk = make_chunk(content="unique giraffe content for rebuild test")
        await storage.upsert_chunks([chunk])

        rebuilt = await storage.rebuild_fts()
        assert rebuilt >= 1

        results = await storage.bm25_search("giraffe", top_k=5)
        assert len(results) >= 1
        assert "giraffe" in results[0].chunk.content

    async def test_rebuild_fts_empty_db(self, components):
        storage = components.storage
        rebuilt = await storage.rebuild_fts()
        assert rebuilt == 0

    async def test_rebuild_fts_returns_correct_count(self, components):
        storage = components.storage
        chunks = [make_chunk(content=f"rebuild content {i}", source=f"rb{i}.md") for i in range(4)]
        await storage.upsert_chunks(chunks)

        count = await storage.rebuild_fts()
        assert count == 4

    # ---- get_chunk_hashes ----------------------------------------------------

    async def test_get_chunk_hashes_returns_mapping(self, components):
        storage = components.storage
        chunk = make_chunk(content="hash test content", source="hashed.md")
        await storage.upsert_chunks([chunk])

        hashes = await storage.get_chunk_hashes(Path("/tmp/hashed.md"))
        assert len(hashes) == 1
        values = list(hashes.values())
        assert values[0] == chunk.content_hash

    async def test_get_chunk_hashes_unknown_source(self, components):
        storage = components.storage
        hashes = await storage.get_chunk_hashes(Path("/tmp/nonexistent.md"))
        assert hashes == {}

    async def test_get_chunk_hashes_multiple_chunks_same_source(self, components):
        storage = components.storage
        c1 = make_chunk(content="first section", source="multi.md")
        c2 = make_chunk(content="second section", source="multi.md")
        await storage.upsert_chunks([c1, c2])

        hashes = await storage.get_chunk_hashes(Path("/tmp/multi.md"))
        assert len(hashes) == 2
        hash_values = set(hashes.values())
        assert c1.content_hash in hash_values
        assert c2.content_hash in hash_values

    # ---- get_embeddings_for_chunks -------------------------------------------

    async def test_get_embeddings_for_chunks_returns_vectors(self, components):
        storage = components.storage
        emb = _varied_embedding(0.5)
        chunk = make_chunk(content="embedding fetch test", embedding=emb)
        await storage.upsert_chunks([chunk])

        result = await storage.get_embeddings_for_chunks([str(chunk.id)])
        assert str(chunk.id) in result
        retrieved = result[str(chunk.id)]
        # Vectors should be close to original (f32 serialization may lose tiny precision)
        assert len(retrieved) == 1024
        assert abs(retrieved[0] - emb[0]) < 0.01

    async def test_get_embeddings_for_chunks_empty_list(self, components):
        storage = components.storage
        result = await storage.get_embeddings_for_chunks([])
        assert result == {}

    async def test_get_embeddings_for_chunks_missing_id(self, components):
        storage = components.storage
        fake_id = str(uuid.uuid4())
        result = await storage.get_embeddings_for_chunks([fake_id])
        assert fake_id not in result

    # ---- increment_access / get_access_counts --------------------------------

    async def test_increment_access_and_get(self, components):
        storage = components.storage
        chunk = make_chunk(content="access test content")
        await storage.upsert_chunks([chunk])

        counts_before = await storage.get_access_counts([chunk.id])
        assert counts_before.get(str(chunk.id), 0) == 0

        await storage.increment_access([chunk.id])
        counts_after = await storage.get_access_counts([chunk.id])
        assert counts_after[str(chunk.id)] == 1

    async def test_increment_access_multiple_times(self, components):
        storage = components.storage
        chunk = make_chunk(content="multi access test")
        await storage.upsert_chunks([chunk])

        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])

        counts = await storage.get_access_counts([chunk.id])
        assert counts[str(chunk.id)] == 3

    async def test_increment_access_empty_list(self, components):
        """Should not raise on empty input."""
        storage = components.storage
        await storage.increment_access([])

    async def test_get_access_counts_empty_list(self, components):
        storage = components.storage
        result = await storage.get_access_counts([])
        assert result == {}

    # ---- get_chunk_size_distribution -----------------------------------------

    async def test_chunk_size_distribution_returns_buckets(self, components):
        storage = components.storage
        chunk = make_chunk(content="x" * 300)  # ~100 estimated tokens
        await storage.upsert_chunks([chunk])

        dist = await storage.get_chunk_size_distribution()
        assert isinstance(dist, list)
        bucket_names = {d["bucket"] for d in dist}
        assert "0-32" in bucket_names
        assert "1024+" in bucket_names

        # The 300-char chunk has ~100 tokens -> "64-128" bucket
        target = next(d for d in dist if d["bucket"] == "64-128")
        assert target["count"] >= 1

    async def test_chunk_size_distribution_empty_db(self, components):
        storage = components.storage
        dist = await storage.get_chunk_size_distribution()
        assert isinstance(dist, list)
        total = sum(d["count"] for d in dist)
        assert total == 0

    async def test_chunk_size_distribution_with_source_filter(self, components):
        storage = components.storage
        c1 = make_chunk(content="a" * 150, source="filtered.md")
        c2 = make_chunk(content="b" * 150, source="other.md")
        await storage.upsert_chunks([c1, c2])

        dist = await storage.get_chunk_size_distribution(source_file=Path("/tmp/filtered.md"))
        total = sum(d["count"] for d in dist)
        assert total == 1

    # ---- reset_embedding_meta ------------------------------------------------

    async def test_reset_embedding_meta_changes_dimension(self, components):
        storage = components.storage
        chunk = make_chunk(content="before reset", embedding=[0.1] * 1024)
        await storage.upsert_chunks([chunk])

        await storage.reset_embedding_meta(dimension=768, provider="openai", model="text-embedding-3-small")

        # Old vector data is gone; DB should accept 768-dim vectors now
        new_chunk = make_chunk(content="after reset", embedding=[0.2] * 768, source="new.md")
        await storage.upsert_chunks([new_chunk])

        results = await storage.dense_search([0.2] * 768, top_k=5)
        assert len(results) >= 1
