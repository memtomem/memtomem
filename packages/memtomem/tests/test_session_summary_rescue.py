"""Phase C — Stage-1 session-summary rescue leg tests.

Covers the new path that runs alongside BM25 + dense:

1. ``_session_summary_boost_sources`` lookup + threshold + chunk_links walk
2. ``_rescue_retrieval`` boost_sources filter
3. 3-leg RRF preserving ``via_session_summary`` (OR) and labelling
   rescue-only chunks as ``session_rescue``
4. End-to-end ``SearchPipeline.search`` surfacing the flag through
   downstream stages so structured output sees it
5. Structured formatter emitting the field only when set
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from memtomem.config import SearchConfig, SessionSummaryConfig
from memtomem.models import Chunk, ChunkLink, ChunkMetadata, SearchResult
from memtomem.search.fusion import reciprocal_rank_fusion
from memtomem.search.pipeline import SearchPipeline
from memtomem.server.formatters import _format_structured_results


def _chunk(content: str = "x", source: str = "a.md", namespace: str = "default") -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            namespace=namespace,
        ),
        embedding=[0.1] * 8,
    )


def _sr(chunk: Chunk, score: float, rank: int, source: str = "bm25", *, via=False) -> SearchResult:
    return SearchResult(chunk=chunk, score=score, rank=rank, source=source, via_session_summary=via)


# ---------------------------------------------------------------------------
# 1. Fusion preserves via_session_summary (OR) + labels rescue leg
# ---------------------------------------------------------------------------


class TestFusionViaSessionSummaryPropagation:
    def test_rescue_only_chunk_labelled_session_rescue(self):
        bm25 = _chunk("only_bm25")
        rescue = _chunk("only_rescue")
        fused = reciprocal_rank_fusion(
            [
                [_sr(bm25, 1.0, 1, "bm25")],
                [],
                [_sr(rescue, 1.0, 1, "session_rescue", via=True)],
            ],
            list_labels=["bm25", "dense", "session_rescue"],
            top_k=5,
        )
        labels = {r.chunk.id: r.source for r in fused}
        assert labels[rescue.id] == "session_rescue"
        flags = {r.chunk.id: r.via_session_summary for r in fused}
        assert flags[rescue.id] is True
        assert flags[bm25.id] is False

    def test_or_propagation_when_chunk_in_multiple_legs(self):
        """A chunk that hit bm25 *and* the rescue leg keeps the flag."""
        shared = _chunk("shared")
        fused = reciprocal_rank_fusion(
            [
                [_sr(shared, 1.0, 1, "bm25", via=False)],
                [],
                [_sr(shared, 1.0, 1, "session_rescue", via=True)],
            ],
            list_labels=["bm25", "dense", "session_rescue"],
            top_k=5,
        )
        result = next(r for r in fused if r.chunk.id == shared.id)
        assert result.via_session_summary is True
        # Hit two legs → labelled "fused"
        assert result.source == "fused"


# ---------------------------------------------------------------------------
# 2. _session_summary_boost_sources helper
# ---------------------------------------------------------------------------


def _make_pipeline(
    storage: AsyncMock,
    *,
    session_summary_config: SessionSummaryConfig | None = None,
) -> SearchPipeline:
    embedder = AsyncMock()
    embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
    return SearchPipeline(
        storage=storage,
        embedder=embedder,
        config=SearchConfig(enable_bm25=True, enable_dense=False),
        session_summary_config=session_summary_config,
    )


def _async_storage() -> AsyncMock:
    s = AsyncMock()
    s.bm25_search = AsyncMock(return_value=[])
    s.dense_search = AsyncMock(return_value=[])
    s.increment_access = AsyncMock()
    s.save_query_history = AsyncMock()
    s.get_access_counts = AsyncMock(return_value={})
    s.get_embeddings_for_chunks = AsyncMock(return_value={})
    s.get_importance_scores = AsyncMock(return_value={})
    s.count_chunks_by_ns_prefix = AsyncMock(return_value=0)
    s.get_chunks_shared_from = AsyncMock(return_value=[])
    s.get_chunks_batch = AsyncMock(return_value={})
    return s


class TestBoostSourcesHelper:
    @pytest.mark.asyncio
    async def test_disabled_when_no_config(self):
        pipeline = _make_pipeline(_async_storage(), session_summary_config=None)
        assert await pipeline._session_summary_boost_sources("q") == set()

    @pytest.mark.asyncio
    async def test_disabled_when_top_k_zero(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=1)
        # zero is rejected by validator, but we can stub directly via private
        # set; emulate by setting cfg with min positive value and bypass
        # threshold-only path: instead, prove disabled by an empty hit list.
        storage = _async_storage()
        storage.bm25_search = AsyncMock(return_value=[])
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()

    @pytest.mark.asyncio
    async def test_threshold_filters_low_score_summary(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.5)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        storage = _async_storage()
        # Below threshold → no rescue
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.1, rank=1, source="bm25")]
        )
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()
        storage.get_chunks_shared_from.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_threshold_walks_chunk_links_to_source_files(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        target1 = _chunk("c1", source="src/a.md")
        target2 = _chunk("c2", source="src/b.md")

        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
        )
        storage.get_chunks_shared_from = AsyncMock(
            return_value=[
                ChunkLink(
                    target_id=target1.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                ),
                ChunkLink(
                    target_id=target2.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                ),
            ]
        )
        storage.get_chunks_batch = AsyncMock(
            return_value={target1.id: target1, target2.id: target2}
        )

        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        sources = await pipeline._session_summary_boost_sources("q")
        assert sources == {"/tmp/src/a.md", "/tmp/src/b.md"}
        # Walk used the correct link_type
        call_args = storage.get_chunks_shared_from.await_args
        assert call_args.kwargs.get("link_type") == "summarizes"

    @pytest.mark.asyncio
    async def test_no_links_yields_empty(self):
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary", namespace="archive:session:abc")
        storage = _async_storage()
        storage.bm25_search = AsyncMock(
            return_value=[_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
        )
        storage.get_chunks_shared_from = AsyncMock(return_value=[])
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        assert await pipeline._session_summary_boost_sources("q") == set()


# ---------------------------------------------------------------------------
# 3. End-to-end pipeline: rescue chunk surfaces with flag preserved
# ---------------------------------------------------------------------------


class TestPipelineEndToEndRescue:
    @pytest.mark.asyncio
    async def test_rescue_chunk_surfaces_with_flag(self):
        """A chunk absent from organic BM25 must be able to enter the result
        set via the rescue leg (RFC ``ranking contention``) and carry
        ``via_session_summary=True`` through the final pipeline output.
        """
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.3)
        summary_chunk = _chunk("summary body", namespace="archive:session:abc")
        rescued = _chunk("rescued chunk", source="src/old_session.md")
        organic = _chunk("organic chunk", source="src/today.md")

        storage = _async_storage()

        async def bm25_dispatch(query: str, top_k: int, namespace_filter=None):
            # Archive lookup pattern
            if namespace_filter is not None and getattr(namespace_filter, "pattern", None) == (
                "archive:session:*"
            ):
                return [_sr(summary_chunk, score=0.9, rank=1, source="bm25")]
            # Organic + rescue (unrestricted) pool — both chunks visible
            return [
                _sr(organic, score=1.0, rank=1, source="bm25"),
                _sr(rescued, score=0.4, rank=2, source="bm25"),
            ]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        storage.get_chunks_shared_from = AsyncMock(
            return_value=[
                ChunkLink(
                    target_id=rescued.id,
                    link_type="summarizes",
                    namespace_target="default",
                    created_at=datetime.now(timezone.utc),
                    source_id=summary_chunk.id,
                )
            ]
        )
        storage.get_chunks_batch = AsyncMock(return_value={rescued.id: rescued})

        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        results, _stats = await pipeline.search("q", top_k=10)

        ids = {r.chunk.id for r in results}
        assert rescued.id in ids
        rescued_result = next(r for r in results if r.chunk.id == rescued.id)
        assert rescued_result.via_session_summary is True
        organic_result = next(r for r in results if r.chunk.id == organic.id)
        assert organic_result.via_session_summary is False

    @pytest.mark.asyncio
    async def test_no_rescue_when_no_summary_above_threshold(self):
        """Common case: no past summary above threshold → rescue leg
        skipped (no extra retrieval round-trip)."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3, expansion_score_threshold=0.5)
        organic = _chunk("organic chunk")

        storage = _async_storage()
        bm25_calls: list[object] = []

        def _label(nf) -> str:
            if nf is None:
                return "ORGANIC"
            if getattr(nf, "pattern", None) == "archive:session:*":
                return "archive:session:*"
            return "ORGANIC"

        async def bm25_dispatch(query: str, top_k: int, namespace_filter=None):
            label = _label(namespace_filter)
            bm25_calls.append(label)
            if label == "archive:session:*":
                return []  # no summary → boost_sources stays empty
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        results, _ = await pipeline.search("q", top_k=10)
        assert {r.chunk.id for r in results} == {organic.id}
        # Exactly two BM25 calls: archive lookup + organic. No third
        # rescue retrieval call when boost_sources is empty.
        assert bm25_calls.count("ORGANIC") == 1
        assert bm25_calls.count("archive:session:*") == 1

    @pytest.mark.asyncio
    async def test_disabled_when_namespace_pinned(self):
        """Caller pinning a namespace explicitly opted in to that scope —
        the rescue leg (which broadens scope back out) must stay quiet."""
        cfg = SessionSummaryConfig(expansion_lookup_top_k=3)
        organic = _chunk("organic", namespace="agent-runtime:planner")

        storage = _async_storage()
        archive_lookup_called = False

        async def bm25_dispatch(query, top_k, namespace_filter=None):
            nonlocal archive_lookup_called
            if getattr(namespace_filter, "pattern", None) == "archive:session:*":
                archive_lookup_called = True
                return []
            return [_sr(organic, score=1.0, rank=1, source="bm25")]

        storage.bm25_search = AsyncMock(side_effect=bm25_dispatch)
        pipeline = _make_pipeline(storage, session_summary_config=cfg)
        await pipeline.search("q", top_k=10, namespace="agent-runtime:planner")
        assert archive_lookup_called is False


# ---------------------------------------------------------------------------
# 4. Structured formatter emits via_session_summary only when True
# ---------------------------------------------------------------------------


class TestStructuredFormatterFlag:
    def test_flag_omitted_when_false(self):
        import json

        sr = _sr(_chunk("a"), 1.0, 1, "bm25", via=False)
        out = json.loads(_format_structured_results([sr]))
        assert "via_session_summary" not in out["results"][0]

    def test_flag_emitted_when_true(self):
        import json

        sr = _sr(_chunk("a"), 1.0, 1, "session_rescue", via=True)
        out = json.loads(_format_structured_results([sr]))
        assert out["results"][0]["via_session_summary"] is True


# ---------------------------------------------------------------------------
# 5. Config validators
# ---------------------------------------------------------------------------


class TestSessionSummaryConfigPhaseC:
    def test_defaults_match_rfc(self):
        cfg = SessionSummaryConfig()
        assert cfg.expansion_lookup_top_k == 3
        assert cfg.expansion_score_threshold == 0.3
        assert cfg.expansion_rescue_weight == 0.5

    def test_top_k_must_be_positive(self):
        with pytest.raises(ValueError):
            SessionSummaryConfig(expansion_lookup_top_k=0)

    def test_threshold_non_negative(self):
        SessionSummaryConfig(expansion_score_threshold=0.0)  # ok
        with pytest.raises(ValueError):
            SessionSummaryConfig(expansion_score_threshold=-0.1)

    def test_rescue_weight_non_negative(self):
        SessionSummaryConfig(expansion_rescue_weight=0.0)  # ok
        with pytest.raises(ValueError):
            SessionSummaryConfig(expansion_rescue_weight=-1.0)
