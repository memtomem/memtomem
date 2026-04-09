"""Tests for search pipeline stages: RRF fusion and MMR diversity re-ranking."""

from __future__ import annotations

import pytest

from memtomem.models import SearchResult
from memtomem.search.fusion import reciprocal_rank_fusion
from memtomem.search.mmr import apply_mmr, cosine_similarity

from helpers import make_chunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sr(content: str = "text", score: float = 1.0, rank: int = 1, source: str = "bm25",
        embedding: list[float] | None = None) -> SearchResult:
    """Create a SearchResult backed by a real Chunk."""
    chunk = make_chunk(content=content, embedding=embedding or [0.1] * 8)
    return SearchResult(chunk=chunk, score=score, rank=rank, source=source)


# ===================================================================
# RRF fusion tests
# ===================================================================

class TestReciprocalRankFusion:
    """Tests for reciprocal_rank_fusion in search/fusion.py."""

    def test_single_list_preserves_order(self):
        """A single input list should come out in the same order."""
        items = [_sr(f"doc{i}", score=10 - i, rank=i + 1) for i in range(5)]
        fused = reciprocal_rank_fusion([items], k=60, top_k=5)
        assert len(fused) == 5
        for i, r in enumerate(fused):
            assert r.chunk.content == f"doc{i}"
            assert r.rank == i + 1

    def test_two_lists_merged(self):
        """Two overlapping lists should merge and assign fused scores."""
        a = _sr("shared", score=1.0, rank=1)
        b = _sr("only_bm25", score=0.8, rank=2)
        list1 = [a, b]

        c = _sr("only_dense", score=0.9, rank=1)
        # Reuse exact same chunk for the shared doc
        shared_dense = SearchResult(chunk=a.chunk, score=0.7, rank=2, source="dense")
        list2 = [c, shared_dense]

        fused = reciprocal_rank_fusion([list1, list2], k=60, top_k=10)
        ids = [r.chunk.id for r in fused]
        # shared doc appears in both lists so it gets higher combined score
        assert ids[0] == a.chunk.id
        assert a.chunk.id in ids

    def test_fused_source_label(self):
        """A doc appearing in both lists should be labelled 'fused'."""
        shared_chunk = make_chunk("shared")
        list1 = [SearchResult(chunk=shared_chunk, score=1.0, rank=1, source="bm25")]
        list2 = [SearchResult(chunk=shared_chunk, score=0.9, rank=1, source="dense")]
        fused = reciprocal_rank_fusion([list1, list2], k=60, top_k=5)
        result = next(r for r in fused if r.chunk.id == shared_chunk.id)
        assert result.source == "fused"

    def test_single_source_label(self):
        """A doc appearing in only one list gets a source-specific label."""
        list1 = [_sr("bm25_only", rank=1)]
        list2 = [_sr("dense_only", rank=1)]
        fused = reciprocal_rank_fusion([list1, list2], k=60, top_k=5)
        labels = {r.chunk.content: r.source for r in fused}
        assert labels["bm25_only"] == "bm25"
        assert labels["dense_only"] == "dense"

    def test_weights_favor_one_list(self):
        """Higher weight on a list should boost its items."""
        doc_a = _sr("from_list1", score=1.0, rank=1)
        doc_b = _sr("from_list2", score=1.0, rank=1)
        # Weight list2 much higher
        fused = reciprocal_rank_fusion(
            [[doc_a], [doc_b]], k=60, top_k=2, weights=[1.0, 10.0]
        )
        assert fused[0].chunk.content == "from_list2"

    def test_empty_lists(self):
        """Empty result lists should produce an empty output."""
        assert reciprocal_rank_fusion([], k=60, top_k=10) == []
        assert reciprocal_rank_fusion([[], []], k=60, top_k=10) == []

    def test_top_k_limits_output(self):
        """top_k should cap the number of returned results."""
        items = [_sr(f"d{i}", rank=i + 1) for i in range(20)]
        fused = reciprocal_rank_fusion([items], k=60, top_k=3)
        assert len(fused) == 3

    def test_k_parameter_effect(self):
        """Smaller k should increase the score difference between top ranks."""
        items = [_sr(f"d{i}", rank=i + 1) for i in range(5)]
        fused_small_k = reciprocal_rank_fusion([items], k=1, top_k=5)
        fused_large_k = reciprocal_rank_fusion([items], k=200, top_k=5)
        # With smaller k, rank 1 gets 1/(1+1)=0.5 vs rank 2 1/(1+2)=0.33 -- bigger gap
        # With larger k, rank 1 gets 1/(200+1) vs rank 2 1/(200+2) -- tiny gap
        gap_small = fused_small_k[0].score - fused_small_k[1].score
        gap_large = fused_large_k[0].score - fused_large_k[1].score
        assert gap_small > gap_large

    def test_weights_padded_when_shorter_than_lists(self):
        """When fewer weights than lists, remaining lists get weight 1.0."""
        list1 = [_sr("a", rank=1)]
        list2 = [_sr("b", rank=1)]
        list3 = [_sr("c", rank=1)]
        fused = reciprocal_rank_fusion([list1, list2, list3], k=60, top_k=5, weights=[5.0])
        # list1 has weight 5.0, lists 2 and 3 get default 1.0
        assert fused[0].chunk.content == "a"


# ===================================================================
# Cosine similarity tests
# ===================================================================

class TestCosineSimilarity:
    """Tests for cosine_similarity in search/mmr.py."""

    def test_identical_vectors(self):
        assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_mismatched_lengths_returns_zero(self):
        assert cosine_similarity([1, 2], [1, 2, 3]) == 0.0

    def test_empty_vectors_returns_zero(self):
        assert cosine_similarity([], []) == 0.0

    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0


# ===================================================================
# MMR tests
# ===================================================================

class TestApplyMMR:
    """Tests for apply_mmr in search/mmr.py."""

    def test_empty_results(self):
        assert apply_mmr([], {}) == []

    def test_single_result(self):
        r = _sr("only", score=1.0, rank=1)
        result = apply_mmr([r], {r.chunk.id: [1.0, 0.0]})
        assert len(result) == 1
        assert result[0].source == "mmr"
        assert result[0].rank == 1

    def test_lambda_one_preserves_relevance_order(self):
        """lambda=1.0 means pure relevance, so order should follow score."""
        r1 = _sr("best", score=1.0, rank=1, embedding=[1, 0])
        r2 = _sr("mid", score=0.7, rank=2, embedding=[0.8, 0.2])
        r3 = _sr("low", score=0.3, rank=3, embedding=[0.5, 0.5])
        embs = {r.chunk.id: [1, 0] for r in [r1, r2, r3]}
        # All embeddings are identical, but lambda=1.0 ignores diversity penalty
        result = apply_mmr([r1, r2, r3], embs, lambda_param=1.0)
        assert [r.chunk.content for r in result] == ["best", "mid", "low"]

    def test_lambda_zero_maximizes_diversity(self):
        """lambda=0.0 means pure diversity -- dissimilar docs should be chosen."""
        r1 = _sr("a", score=1.0, rank=1)
        r2 = _sr("b", score=0.9, rank=2)
        r3 = _sr("c", score=0.8, rank=3)
        # r1 and r2 have nearly identical embeddings; r3 is very different
        embs = {
            r1.chunk.id: [1.0, 0.0, 0.0],
            r2.chunk.id: [0.99, 0.01, 0.0],
            r3.chunk.id: [0.0, 0.0, 1.0],
        }
        result = apply_mmr([r1, r2, r3], embs, lambda_param=0.0)
        # r1 is always first (highest score). Then r3 should come before r2
        # because r3 is most dissimilar to r1.
        assert result[0].chunk.content == "a"
        assert result[1].chunk.content == "c"
        assert result[2].chunk.content == "b"

    def test_top_k_limits_output(self):
        results = [_sr(f"d{i}", score=1.0 - i * 0.1, rank=i + 1) for i in range(10)]
        embs = {r.chunk.id: [float(i)] for i, r in enumerate(results)}
        out = apply_mmr(results, embs, top_k=3)
        assert len(out) == 3

    def test_all_results_labelled_mmr(self):
        results = [_sr(f"d{i}", score=1.0 - i * 0.1, rank=i + 1) for i in range(4)]
        embs = {r.chunk.id: [float(i), 0.0] for i, r in enumerate(results)}
        out = apply_mmr(results, embs)
        assert all(r.source == "mmr" for r in out)

    def test_ranks_are_sequential(self):
        results = [_sr(f"d{i}", score=1.0 - i * 0.1, rank=i + 1) for i in range(5)]
        embs = {r.chunk.id: [float(i), 0.0] for i, r in enumerate(results)}
        out = apply_mmr(results, embs)
        assert [r.rank for r in out] == list(range(1, len(out) + 1))

    def test_missing_embeddings_treated_as_zero_similarity(self):
        """Chunks without embeddings should have 0 similarity with everything."""
        r1 = _sr("a", score=1.0, rank=1)
        r2 = _sr("b", score=0.9, rank=2)
        # Only r1 has an embedding; r2 does not
        embs = {r1.chunk.id: [1.0, 0.0]}
        result = apply_mmr([r1, r2], embs, lambda_param=0.5)
        assert len(result) == 2
