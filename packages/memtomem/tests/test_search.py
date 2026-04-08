"""Tests for search pipeline and scoring."""

import pytest
from memtomem.search.access import access_boost, apply_access_boost
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from pathlib import Path
from uuid import uuid4


class TestAccessBoost:
    def test_zero_access(self):
        assert access_boost(0) == 1.0

    def test_positive_access(self):
        b10 = access_boost(10)
        b100 = access_boost(100)
        assert 1.0 < b10 < b100
        assert b100 == pytest.approx(1.5)

    def test_max_boost_configurable(self):
        b = access_boost(100, max_boost=2.0)
        assert b == pytest.approx(2.0)

    def test_negative_access(self):
        assert access_boost(-1) == 1.0


class TestApplyAccessBoost:
    def _make_result(self, score, chunk_id=None):
        cid = chunk_id or uuid4()
        chunk = Chunk(
            content="test",
            metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
            id=cid,
            embedding=[],
        )
        return SearchResult(chunk=chunk, score=score, rank=1, source="test")

    def test_reorders_by_boosted_score(self):
        r1 = self._make_result(0.5)
        r2 = self._make_result(0.4)
        # r2 has high access, should be boosted above r1
        counts = {str(r2.chunk.id): 100}
        boosted = apply_access_boost([r1, r2], counts)
        assert boosted[0].chunk.id == r2.chunk.id

    def test_empty_results(self):
        assert apply_access_boost([], {}) == []

    def test_no_access_counts(self):
        r = self._make_result(0.5)
        boosted = apply_access_boost([r], {})
        assert boosted[0].score == pytest.approx(0.5)
