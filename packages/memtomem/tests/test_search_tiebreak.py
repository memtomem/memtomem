"""Deterministic tie-break in the BM25 and dense search legs (#516).

Equal ``fts.rank`` (BM25) or equal ``distance`` (dense) rows within the same
scope priority previously took arbitrary SQLite order, so repeated queries or
reopened connections could reorder them — destabilizing replay diffs. Both legs
now append the unique ``c.id`` as the final ``ORDER BY`` key, so tied rows come
back in a fixed, id-sorted order.
"""

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.storage.sqlite_backend import SqliteBackend


def _ids_in_sql_order(chunk_ids):
    """SQLite orders the TEXT ``id`` column lexicographically; mirror that."""
    return sorted(chunk_ids, key=str)


class TestSearchLegTieBreak:
    @pytest.mark.asyncio
    async def test_bm25_tied_rank_orders_by_id(self, storage):
        # Identical content -> identical BM25 rank; identical scope -> the only
        # thing left to order by is the trailing ``c.id``.
        chunks = [_make_chunk("tiebreak marker body", source=f"s{i}.md") for i in range(6)]
        await storage.upsert_chunks(chunks)

        results = await storage.bm25_search("tiebreak", top_k=6)

        got = [r.chunk.id for r in results]
        assert got == _ids_in_sql_order(c.id for c in chunks)

    @pytest.mark.asyncio
    async def test_bm25_tied_rank_stable_across_repeated_queries(self, storage):
        chunks = [_make_chunk("stable marker body", source=f"s{i}.md") for i in range(6)]
        await storage.upsert_chunks(chunks)

        first = [r.chunk.id for r in await storage.bm25_search("stable", top_k=6)]
        second = [r.chunk.id for r in await storage.bm25_search("stable", top_k=6)]
        assert first == second

    @pytest.mark.asyncio
    async def test_dense_tied_distance_orders_by_id(self, storage):
        # Identical embeddings -> identical distance to any query vector.
        vec = [0.2] * 1024
        chunks = [_make_chunk("dense tie body", source=f"d{i}.md", embedding=vec) for i in range(6)]
        await storage.upsert_chunks(chunks)

        results = await storage.dense_search([0.2] * 1024, top_k=6)

        got = [r.chunk.id for r in results]
        assert got == _ids_in_sql_order(c.id for c in chunks)

    @pytest.mark.asyncio
    async def test_tied_order_stable_across_connection_reopen(self, storage, components):
        chunks = [_make_chunk("reopen marker body", source=f"r{i}.md") for i in range(6)]
        await storage.upsert_chunks(chunks)

        first = [r.chunk.id for r in await storage.bm25_search("reopen", top_k=6)]

        # Reopen a fresh backend against the same DB file: a new connection
        # pool must not reorder tied rows.
        cfg = components.config
        reopened = SqliteBackend(
            cfg.storage,
            dimension=cfg.embedding.dimension,
            strict_dim_check=False,
        )
        await reopened.initialize()
        try:
            second = [r.chunk.id for r in await reopened.bm25_search("reopen", top_k=6)]
        finally:
            await reopened.close()
        assert first == second
