"""Deterministic exhaustive dense KNN for replay (#1802, split from ``record`` by #2671).

``exhaustive=True`` switches every dense leg reachable from ``search()`` — the
primary retrieval, heading expansion, and session-summary rescue — to an
exhaustive inner KNN. sqlite-vec 0.1.9 prunes to the adaptive inner ``LIMIT``
with an unstable distance-only sort, so equal-distance rows straddling that
cutoff are dropped nondeterministically; scanning every embedding removes the
cutoff so the outer stable ``ORDER BY ..., c.id`` fully determines selection.

``record=False`` alone does not ask for it: a background read keeps the
ordinary adaptive KNN, which is what keeps its dense leg alive above the KNN cap.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.search.expansion import expand_query_headings

#: ``search()`` keyword arguments → whether its dense legs must run exhaustive.
_MODES = [
    ({"record": False, "exhaustive": True}, True),
    ({"record": False}, False),
    ({"record": True}, False),
]
_MODE_IDS = ["replay", "background-read", "recorded"]


async def _drain_bg(pipeline) -> None:
    if pipeline._bg_tasks:
        await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)


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
    @pytest.mark.parametrize("kwargs,expected", _MODES, ids=_MODE_IDS)
    async def test_primary_leg_threads_exhaustive(self, components, kwargs, expected):
        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _FakeEmbedder()

        await storage.upsert_chunks(
            [_make_chunk("thread marker body", source=f"t{i}.md") for i in range(3)]
        )

        seen: list[bool] = []
        original = storage.dense_search

        async def _spy(*args, **kw):
            seen.append(kw.get("exhaustive", False))
            return await original(*args, **kw)

        storage.dense_search = _spy  # type: ignore[method-assign]

        await pipeline.search("thread", **kwargs)
        await _drain_bg(pipeline)
        assert seen, "sanity: the primary dense leg ran"
        assert set(seen) == {expected}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs,expected", _MODES, ids=_MODE_IDS)
    async def test_search_threads_exhaustive_into_heading_expansion(
        self, components, monkeypatch, kwargs, expected
    ):
        """``search()`` itself hands the flag to heading expansion.

        The helper-level test above cannot see a wrong argument at the call
        site, and expansion is off by default, so it is switched on here.
        """
        from memtomem.search import expansion

        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _FakeEmbedder()
        monkeypatch.setattr(
            pipeline,
            "_expansion_config",
            SimpleNamespace(enabled=True, strategy="headings", max_terms=3),
        )
        await storage.upsert_chunks([_make_chunk("heading marker body", heading=("Alpha",))])

        seen: list[bool] = []
        original = expansion.expand_query_headings

        async def _spy(*args, **kw):
            seen.append(kw["exhaustive"])
            return await original(*args, **kw)

        monkeypatch.setattr(expansion, "expand_query_headings", _spy)

        await pipeline.search("heading", **kwargs)
        await _drain_bg(pipeline)
        assert seen == [expected]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs,expected", _MODES, ids=_MODE_IDS)
    async def test_search_threads_exhaustive_into_rescue(
        self, components, monkeypatch, kwargs, expected
    ):
        """``search()`` itself hands the flag to the session-summary rescue leg.

        Rescue only runs with boost sources, which come from session summaries
        the fixtures do not seed — so the lookup is replaced and the real
        ``_rescue_retrieval`` runs behind a spy.
        """
        from memtomem.config import SessionSummaryConfig

        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _FakeEmbedder()
        chunk = _make_chunk("rescue marker body", source="rescue.md")
        await storage.upsert_chunks([chunk])
        monkeypatch.setattr(
            pipeline, "_session_summary_config", SessionSummaryConfig(expansion_lookup_top_k=3)
        )

        async def _boost_sources(*args, **kw):
            return {str(chunk.metadata.source_file)}

        monkeypatch.setattr(pipeline, "_session_summary_boost_sources", _boost_sources)

        seen: list[bool] = []
        original = pipeline._rescue_retrieval

        async def _spy(*args, **kw):
            seen.append(kw["exhaustive"])
            return await original(*args, **kw)

        monkeypatch.setattr(pipeline, "_rescue_retrieval", _spy)

        await pipeline.search("rescue", **kwargs)
        await _drain_bg(pipeline)
        assert seen == [expected]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["thread", ""], ids=["ranked", "filter-only"])
    async def test_exhaustive_requires_record_false(self, components, query):
        """The result-cache key does not carry ``exhaustive``.

        A recorded exhaustive search would be served from, and stored into,
        the ordinary search's cache slot — so the combination is refused on
        every path, including the empty-query one that runs no dense leg.
        """
        pipeline = components.search_pipeline
        pipeline._embedder = _FakeEmbedder()
        with pytest.raises(ValueError, match="requires record=False"):
            await pipeline.search(query, tag_filter="t", exhaustive=True)

    @pytest.mark.asyncio
    async def test_rescue_leg_threads_exhaustive(self, components):
        # The pipeline's rescue leg only fires with non-empty boost_sources
        # (from session summaries), which the fixtures don't seed — so exercise
        # ``_rescue_retrieval`` directly to prove its dense leg threads the flag.
        storage, pipeline = components.storage, components.search_pipeline
        chunk = _make_chunk("rescue marker body", source="rescue.md")
        await storage.upsert_chunks([chunk])

        seen: list[bool] = []
        original = storage.dense_search

        async def _spy(*args, **kwargs):
            seen.append(kwargs.get("exhaustive", False))
            return await original(*args, **kwargs)

        storage.dense_search = _spy  # type: ignore[method-assign]

        await pipeline._rescue_retrieval(
            "rescue",
            [0.1] * 1024,
            top_k=5,
            boost_sources={str(chunk.metadata.source_file)},
            use_bm25=False,
            use_dense=True,
            exhaustive=True,
        )
        assert seen == [True]


class TestBackgroundReadAboveTheCap:
    """#2671: ``record=False`` must not cost a large store its dense leg."""

    @pytest.mark.asyncio
    async def test_record_false_keeps_dense_where_replay_is_refused(self, components, monkeypatch):
        from memtomem.storage import sqlite_backend

        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _FakeEmbedder()
        monkeypatch.setattr(sqlite_backend, "VEC_MAX_KNN_K", 3)
        chunks = [
            _make_chunk(f"cap marker {i}", source=f"k{i}.md", embedding=[0.1] * 1024)
            for i in range(5)
        ]
        await storage.upsert_chunks(chunks)
        ids = [c.id for c in chunks]
        before = await storage.get_access_counts(ids)

        results, stats = await pipeline.search("cap marker", record=False)
        await _drain_bg(pipeline)

        assert results
        assert stats.dense_error is None and stats.dense_error_code is None
        assert stats.dense_candidates > 0, "the dense leg contributed candidates"
        # Still a background read: nothing written, nothing cached.
        assert stats.query_run_id is None
        assert await storage.get_access_counts(ids) == before
        assert await storage.get_search_runs() == []
        assert pipeline._search_cache == {}

        # The same store refuses the deterministic scan, so the pass above is
        # the flag's doing and not a store that happens to sit under the cap.
        _, replay_stats = await pipeline.search("cap marker", record=False, exhaustive=True)
        assert replay_stats.dense_error_code == "dense_exhaustive_limit"
        assert replay_stats.dense_candidates == 0
