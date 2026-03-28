"""Tests for importance scoring."""

import pytest
from pathlib import Path
from uuid import uuid4
from memtomem.search.importance import compute_importance, apply_importance_boost
from memtomem.models import Chunk, ChunkMetadata, SearchResult


class TestComputeImportance:
    def test_zero_everything(self):
        score = compute_importance(0, 0, 0, 0.0)
        # Only recency contributes (exp(0) = 1.0 * w3=0.2)
        assert 0.0 < score < 0.3

    def test_high_access(self):
        s_low = compute_importance(0, 0, 0, 0.0)
        s_high = compute_importance(100, 0, 0, 0.0)
        assert s_high > s_low

    def test_tags_increase_score(self):
        s0 = compute_importance(0, 0, 0, 0.0)
        s5 = compute_importance(0, 5, 0, 0.0)
        assert s5 > s0

    def test_relations_increase_score(self):
        s0 = compute_importance(0, 0, 0, 0.0)
        s10 = compute_importance(0, 0, 10, 0.0)
        assert s10 > s0

    def test_old_content_lower_score(self):
        s_new = compute_importance(10, 2, 1, 0.0)
        s_old = compute_importance(10, 2, 1, 365.0)
        assert s_new > s_old

    def test_score_bounded(self):
        score = compute_importance(1000, 100, 100, 0.0)
        assert 0.0 <= score <= 1.0

    def test_custom_weights(self):
        # All weight on access
        s = compute_importance(100, 0, 0, 0.0, weights=(1.0, 0.0, 0.0, 0.0))
        assert s > 0.5


class TestApplyImportanceBoost:
    def _make_result(self, score, chunk_id=None):
        cid = chunk_id or uuid4()
        chunk = Chunk(
            content="test",
            metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
            id=cid,
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=score, rank=1, source="test")

    def test_boost_reorders(self):
        r1 = self._make_result(0.5)
        r2 = self._make_result(0.4)
        # r2 has high importance, should be boosted above r1
        scores = {str(r2.chunk.id): 1.0}  # max importance
        boosted = apply_importance_boost([r1, r2], scores, max_boost=2.0)
        assert boosted[0].chunk.id == r2.chunk.id

    def test_empty_results(self):
        assert apply_importance_boost([], {}) == []

    def test_no_importance_no_change(self):
        r = self._make_result(0.5)
        boosted = apply_importance_boost([r], {})
        assert boosted[0].score == pytest.approx(0.5)

    def test_max_boost_applied(self):
        r = self._make_result(1.0)
        scores = {str(r.chunk.id): 1.0}
        boosted = apply_importance_boost([r], scores, max_boost=2.0)
        assert boosted[0].score == pytest.approx(2.0)
