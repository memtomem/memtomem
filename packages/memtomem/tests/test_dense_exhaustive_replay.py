"""Deterministic exhaustive dense KNN under replay mode (#1802).

``record=False`` switches every dense leg reachable from ``search()`` — the
primary retrieval, heading expansion, and session-summary rescue — to an
exhaustive inner KNN. sqlite-vec 0.1.9 prunes to the adaptive inner ``LIMIT``
with an unstable distance-only sort, so equal-distance rows straddling that
cutoff are dropped nondeterministically; scanning every embedding removes the
cutoff so the outer stable ``ORDER BY ..., c.id`` fully determines selection.
"""

from __future__ import annotations

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.search.expansion import expand_query_headings


class _FakeEmbedder:
    dimension = 1024
    model_name = "fake"

    async def embed_query(self, query: str) -> list[float]:
        return [0.1] * 1024


class TestExhaustiveDeterminism:
    @pytest.mark.asyncio
    async def test_exhaustive_selects_full_set_deterministically(self, storage):
        # More tied-distance chunks than the non-exhaustive first cutoff
        # (max(top_k*5, 100)) so a boundary genuinely exists.
        vec = [0.2] * 1024
        chunks = [
            _make_chunk(f"exhaustive body {i}", source=f"e{i}.md", embedding=vec)
            for i in range(120)
        ]
        await storage.upsert_chunks(chunks)

        results = await storage.dense_search([0.2] * 1024, top_k=5, exhaustive=True)
        got = [r.chunk.id for r in results]

        # With every row fetched, the outer stable sort returns the 5 lowest ids.
        expected = sorted((c.id for c in chunks), key=str)[:5]
        assert got == expected

        # Stable across a repeat call.
        again = await storage.dense_search([0.2] * 1024, top_k=5, exhaustive=True)
        assert [r.chunk.id for r in again] == expected


class TestExhaustiveThreading:
    @pytest.mark.asyncio
    async def test_heading_expansion_threads_exhaustive(self, storage):
        await storage.upsert_chunks([_make_chunk("body", heading=("Alpha Topic",))])
        seen: list[bool] = []
        original = storage.dense_search

        async def _spy(*args, **kwargs):
            seen.append(kwargs.get("exhaustive", False))
            return await original(*args, **kwargs)

        storage.dense_search = _spy  # type: ignore[method-assign]
        await expand_query_headings("query", storage, _FakeEmbedder(), exhaustive=True)
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_primary_and_rescue_legs_thread_exhaustive(self, components):
        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _FakeEmbedder()

        await storage.upsert_chunks(
            [_make_chunk("thread marker body", source=f"t{i}.md") for i in range(3)]
        )

        seen: list[bool] = []
        original = storage.dense_search

        async def _spy(*args, **kwargs):
            seen.append(kwargs.get("exhaustive", False))
            return await original(*args, **kwargs)

        storage.dense_search = _spy  # type: ignore[method-assign]

        await pipeline.search("thread", record=False)
        assert seen and all(seen), "every dense leg must run exhaustive under replay"

        seen.clear()
        await pipeline.search("thread", record=True)
        if pipeline._bg_tasks:
            import asyncio

            await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)
        assert seen and not any(seen), "normal search must not run exhaustive"
