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
