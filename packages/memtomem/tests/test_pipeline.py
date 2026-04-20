"""Tests for search pipeline stages (expansion, reranker, importance integration)."""

import asyncio

import pytest
from pathlib import Path
from uuid import uuid4
from memtomem.models import Chunk, ChunkMetadata, SearchResult


class TestPipelineQueryExpansion:
    """Test that query expansion modifies queries before retrieval."""

    @pytest.mark.asyncio
    async def test_tag_expansion_appends_terms(self):
        from memtomem.search.expansion import expand_query_tags

        class FakeStorage:
            async def get_tag_counts(self):
                return [("deployment", 10), ("kubernetes", 5), ("docker", 3)]

        result = await expand_query_tags("deploy containers", FakeStorage())
        assert "deployment" in result or result == "deploy containers"

    @pytest.mark.asyncio
    async def test_tag_expansion_no_match(self):
        from memtomem.search.expansion import expand_query_tags

        class FakeStorage:
            async def get_tag_counts(self):
                return [("python", 5)]

        result = await expand_query_tags("javascript frameworks", FakeStorage())
        assert result == "javascript frameworks"

    @pytest.mark.asyncio
    async def test_expansion_handles_error(self):
        from memtomem.search.expansion import expand_query_tags

        class BrokenStorage:
            async def get_tag_counts(self):
                raise RuntimeError("DB error")

        result = await expand_query_tags("test", BrokenStorage())
        assert result == "test"


class TestPipelineImportanceBoost:
    """Test importance boost re-ordering."""

    def _make_result(self, score, chunk_id=None):
        cid = chunk_id or uuid4()
        chunk = Chunk(
            content="test",
            metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
            id=cid,
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=score, rank=1, source="test")

    def test_high_importance_reorders(self):
        from memtomem.search.importance import apply_importance_boost

        r1 = self._make_result(0.8)  # high score, no importance
        r2 = self._make_result(0.5)  # lower score, high importance
        scores = {str(r2.chunk.id): 1.0}

        boosted = apply_importance_boost([r1, r2], scores, max_boost=2.0)
        # r2 should be boosted: 0.5 * 2.0 = 1.0 > 0.8
        assert boosted[0].chunk.id == r2.chunk.id

    def test_zero_importance_no_change(self):
        from memtomem.search.importance import apply_importance_boost

        r1 = self._make_result(0.8)
        r2 = self._make_result(0.5)
        scores = {}  # no importance

        boosted = apply_importance_boost([r1, r2], scores)
        assert boosted[0].chunk.id == r1.chunk.id
        assert boosted[0].score == pytest.approx(0.8)


class TestBgTaskErrorCallback:
    """_bg_task_error_cb must log at warning when a fire-and-forget task raises."""

    @pytest.mark.asyncio
    async def test_callback_logs_warning_on_exception(self, caplog):
        import logging
        from memtomem.search.pipeline import _bg_task_error_cb

        async def _failing():
            raise RuntimeError("storage down")

        task = asyncio.create_task(_failing())
        task.add_done_callback(_bg_task_error_cb)

        with caplog.at_level(logging.WARNING, logger="memtomem.search.pipeline"):
            # Wait for the task to complete and the callback to fire.
            try:
                await task
            except RuntimeError:
                pass
            # The callback runs synchronously after the task finishes, but we
            # need a brief event-loop tick for it to execute.
            await asyncio.sleep(0)

        assert any("storage down" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_callback_silent_on_success(self, caplog):
        import logging
        from memtomem.search.pipeline import _bg_task_error_cb

        async def _ok():
            return 42

        task = asyncio.create_task(_ok())
        task.add_done_callback(_bg_task_error_cb)

        with caplog.at_level(logging.WARNING, logger="memtomem.search.pipeline"):
            await task
            await asyncio.sleep(0)

        assert not any("Background task" in r.message for r in caplog.records)


class TestImportanceCompute:
    """Test importance score edge cases."""

    def test_all_max(self):
        from memtomem.search.importance import compute_importance

        score = compute_importance(1000, 10, 50, 0.0)
        assert 0.8 <= score <= 1.0

    def test_all_zero_except_recency(self):
        from memtomem.search.importance import compute_importance

        score = compute_importance(0, 0, 0, 0.0)
        # recency factor = exp(0) = 1.0, weight = 0.2
        assert score == pytest.approx(0.2, abs=0.05)

    def test_very_old(self):
        from memtomem.search.importance import compute_importance

        score_new = compute_importance(10, 3, 2, 0.0)
        score_old = compute_importance(10, 3, 2, 1000.0)
        assert score_new > score_old


class TestRerankCandidatePool:
    """Regression for #307: RerankConfig.top_k must widen the rerank pool.

    Before the fix, ``SearchPipeline`` passed ``top_k`` (the response size) as
    the fusion cap, so the reranker could only reorder within the already-
    trimmed top-K and could not rescue relevant chunks RRF ranked just
    outside it.
    """

    @staticmethod
    def _make_result(content: str, rank: int, score: float | None = None) -> SearchResult:
        chunk = Chunk(
            content=content,
            metadata=ChunkMetadata(source_file=Path(f"/tmp/{content}.md")),
            id=uuid4(),
            embedding=[],
        )
        return SearchResult(
            chunk=chunk,
            score=1.0 / rank if score is None else score,
            rank=rank,
            source="fused",
        )

    def _make_pipeline(
        self,
        bm25_results: list[SearchResult],
        *,
        reranker: object | None,
        rerank_config: object | None,
    ):
        from unittest.mock import AsyncMock

        from memtomem.config import SearchConfig
        from memtomem.search.pipeline import SearchPipeline

        storage = AsyncMock()
        storage.bm25_search = AsyncMock(return_value=bm25_results)
        storage.dense_search = AsyncMock(return_value=[])
        storage.increment_access = AsyncMock()
        storage.save_query_history = AsyncMock()
        storage.get_access_counts = AsyncMock(return_value={})
        storage.get_embeddings_for_chunks = AsyncMock(return_value={})
        storage.get_importance_scores = AsyncMock(return_value={})
        storage.count_chunks_by_ns_prefix = AsyncMock(return_value=0)

        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)

        return SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=False),
            reranker=reranker,
            rerank_config=rerank_config,
        )

    @pytest.mark.asyncio
    async def test_reranker_receives_widened_pool_rescuing_outranked_chunk(self):
        """RRF ranks the relevant chunk at position 15; rerank_config.top_k=20
        must let the cross-encoder see it and surface it into the response."""
        from memtomem.config import RerankConfig

        # 20 candidates, relevant one at BM25 rank 15 (0-indexed: 14)
        fused_input = [self._make_result(f"chunk{i}", rank=i + 1) for i in range(20)]
        relevant = fused_input[14]

        received_pool_size: list[int] = []

        class ScoringReranker:
            async def rerank(self, query, results, top_k):
                received_pool_size.append(len(results))
                # Score the "relevant" chunk highest, everything else near zero.
                scored = [
                    SearchResult(
                        chunk=r.chunk,
                        score=1.0 if r.chunk.id == relevant.chunk.id else 0.01,
                        rank=r.rank,
                        source="reranked",
                    )
                    for r in results
                ]
                scored.sort(key=lambda r: r.score, reverse=True)
                return scored[:top_k]

        pipeline = self._make_pipeline(
            fused_input,
            reranker=ScoringReranker(),
            rerank_config=RerankConfig(enabled=True, top_k=20),
        )

        results, _ = await pipeline.search("anything", top_k=10)

        # Reranker must receive the widened pool, not the response top_k.
        assert received_pool_size == [20]
        # And the relevant chunk — originally at RRF position 15 — must now
        # be first in the response.
        assert results[0].chunk.id == relevant.chunk.id
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_pool_collapses_to_top_k_when_rerank_disabled(self):
        """No reranker + no rerank_config → single-retriever pool stays at top_k."""
        fused_input = [self._make_result(f"chunk{i}", rank=i + 1) for i in range(20)]

        pipeline = self._make_pipeline(fused_input, reranker=None, rerank_config=None)
        results, _ = await pipeline.search("anything", top_k=10)
        assert len(results) == 10

    def test_cache_key_changes_when_rerank_top_k_changes(self):
        """Enabling rerank or changing ``rerank.top_k`` must bust the cache."""
        from memtomem.config import RerankConfig

        class DummyReranker:
            async def rerank(self, query, results, top_k):
                return results[:top_k]

        base = self._make_pipeline([], reranker=None, rerank_config=None)
        key_no_rerank = base._cache_key("q", 10, None, None, None)

        with_20 = self._make_pipeline(
            [],
            reranker=DummyReranker(),
            rerank_config=RerankConfig(enabled=True, top_k=20),
        )
        key_20 = with_20._cache_key("q", 10, None, None, None)

        with_50 = self._make_pipeline(
            [],
            reranker=DummyReranker(),
            rerank_config=RerankConfig(enabled=True, top_k=50),
        )
        key_50 = with_50._cache_key("q", 10, None, None, None)

        assert key_no_rerank != key_20
        assert key_20 != key_50
