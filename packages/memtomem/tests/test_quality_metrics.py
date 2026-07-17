"""Unit tests for the packaged IR metrics (#1802)."""

from __future__ import annotations

import pytest

from memtomem.quality.metrics import (
    hit_rate_at_k,
    mean,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    recall_labeled_at_k,
    reciprocal_rank_at_k,
)


class TestRecall:
    def test_basic(self):
        assert recall_at_k(["a", "b", "c"], {"a", "c"}, 3) == 1.0
        assert recall_at_k(["a", "b", "c"], {"a", "z"}, 3) == 0.5

    def test_empty_relevant_is_zero(self):
        assert recall_at_k(["a"], set(), 3) == 0.0

    def test_k_zero_is_zero(self):
        assert recall_at_k(["a"], {"a"}, 0) == 0.0

    def test_duplicate_relevant_hit_bounded_at_one(self):
        # A ranking that repeats the same relevant id (hash conflation) must
        # not push recall above 1.0.
        assert recall_at_k(["a", "a", "a"], {"a"}, 3) == 1.0

    def test_labeled_alias_matches_recall(self):
        assert recall_labeled_at_k(["a", "b"], {"a"}, 2) == recall_at_k(["a", "b"], {"a"}, 2)


class TestReciprocalRank:
    def test_first_hit_position(self):
        assert reciprocal_rank_at_k(["x", "a", "b"], {"a"}, 3) == pytest.approx(0.5)

    def test_no_hit_is_zero(self):
        assert reciprocal_rank_at_k(["x", "y"], {"a"}, 2) == 0.0

    def test_respects_k_window(self):
        assert reciprocal_rank_at_k(["x", "y", "a"], {"a"}, 2) == 0.0


class TestNdcg:
    def test_perfect_ranking_is_one(self):
        assert ndcg_at_k(["a", "b"], {"a": 1.0, "b": 1.0}, 2) == pytest.approx(1.0)

    def test_empty_relevance_is_zero(self):
        assert ndcg_at_k(["a"], {}, 2) == 0.0


class TestPrecision:
    def test_fully_labeled_window(self):
        assert precision_at_k(["a", "b"], {"a"}, {"b"}, 2) == pytest.approx(0.5)

    def test_none_when_an_item_is_unlabeled(self):
        # "c" carries no judgment → precision is undefined, not a biased number.
        assert precision_at_k(["a", "b", "c"], {"a"}, {"b"}, 3) is None

    def test_none_when_no_items_retrieved(self):
        assert precision_at_k([], {"a"}, set(), 3) is None

    def test_none_when_k_zero(self):
        assert precision_at_k(["a"], {"a"}, set(), 0) is None

    def test_all_relevant_window(self):
        assert precision_at_k(["a", "b"], {"a", "b"}, set(), 2) == pytest.approx(1.0)

    def test_duplicate_labeled_hit_bounded(self):
        assert precision_at_k(["a", "a"], {"a"}, set(), 2) == pytest.approx(1.0)


class TestHitRate:
    def test_hit(self):
        assert hit_rate_at_k(["x", "a"], {"a"}, 2) == 1.0

    def test_miss(self):
        assert hit_rate_at_k(["x", "y"], {"a"}, 2) == 0.0

    def test_outside_window_is_miss(self):
        assert hit_rate_at_k(["x", "y", "a"], {"a"}, 2) == 0.0


class TestMean:
    def test_basic(self):
        assert mean([1.0, 0.0]) == pytest.approx(0.5)

    def test_empty_is_zero(self):
        assert mean([]) == 0.0

    def test_aggregate_excludes_none_precision(self):
        # An aggregate over precision must drop the undefined (None) cases,
        # not coerce them to 0.0.
        per_case = [
            precision_at_k(["a", "b"], {"a"}, {"b"}, 2),  # 0.5
            precision_at_k(["a", "c"], {"a"}, set(), 2),  # None (c unlabeled)
            precision_at_k(["a", "b"], {"a", "b"}, set(), 2),  # 1.0
        ]
        valid = [p for p in per_case if p is not None]
        assert len(valid) == 2
        assert mean(valid) == pytest.approx(0.75)


class TestShimParity:
    def test_shim_reexports_same_callables(self):
        import ir_metrics

        assert ir_metrics.recall_at_k is recall_at_k
        assert ir_metrics.reciprocal_rank_at_k is reciprocal_rank_at_k
        assert ir_metrics.ndcg_at_k is ndcg_at_k
        assert ir_metrics.mean is mean
