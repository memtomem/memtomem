"""Unit tests for ir_metrics.py — edge cases matter; the metrics anchor regression tests."""

from __future__ import annotations

import math

from ir_metrics import mean, ndcg_at_k, recall_at_k, reciprocal_rank_at_k


class TestRecallAtK:
    def test_partial_hit(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"a", "d"}, k=3) == 0.5

    def test_full_hit(self) -> None:
        assert recall_at_k(["a", "b"], {"a", "b"}, k=2) == 1.0

    def test_no_hit(self) -> None:
        assert recall_at_k(["x", "y"], {"a", "b"}, k=2) == 0.0

    def test_empty_relevant_returns_zero(self) -> None:
        assert recall_at_k(["a", "b"], set(), k=2) == 0.0

    def test_empty_retrieved_returns_zero(self) -> None:
        assert recall_at_k([], {"a"}, k=3) == 0.0

    def test_k_zero_returns_zero(self) -> None:
        assert recall_at_k(["a"], {"a"}, k=0) == 0.0

    def test_k_larger_than_retrieved(self) -> None:
        # k=10 but only 2 retrieved; both are relevant, relevant set has 2 items.
        assert recall_at_k(["a", "b"], {"a", "b"}, k=10) == 1.0

    def test_frozenset_relevant_accepted(self) -> None:
        assert recall_at_k(["a", "b"], frozenset({"a"}), k=2) == 1.0


class TestReciprocalRankAtK:
    def test_first_position_is_one(self) -> None:
        assert reciprocal_rank_at_k(["a", "b"], {"a"}, k=5) == 1.0

    def test_second_position_is_half(self) -> None:
        assert reciprocal_rank_at_k(["x", "a"], {"a"}, k=5) == 0.5

    def test_third_position(self) -> None:
        assert reciprocal_rank_at_k(["x", "y", "a"], {"a"}, k=5) == 1.0 / 3.0

    def test_no_hit_returns_zero(self) -> None:
        assert reciprocal_rank_at_k(["x", "y"], {"a"}, k=5) == 0.0

    def test_hit_beyond_k_is_missed(self) -> None:
        # Relevant item is at rank 3, but k=2 cuts off.
        assert reciprocal_rank_at_k(["x", "y", "a"], {"a"}, k=2) == 0.0

    def test_k_zero_returns_zero(self) -> None:
        assert reciprocal_rank_at_k(["a"], {"a"}, k=0) == 0.0

    def test_empty_retrieved(self) -> None:
        assert reciprocal_rank_at_k([], {"a"}, k=3) == 0.0

    def test_multiple_relevant_uses_earliest(self) -> None:
        # Earliest relevant wins (rank 2), not rank 3.
        assert reciprocal_rank_at_k(["x", "a", "b"], {"a", "b"}, k=5) == 0.5


class TestNdcgAtK:
    def test_perfect_ranking_binary(self) -> None:
        # Two relevant items, both in top-2 → NDCG = 1.0
        result = ndcg_at_k(["a", "b", "x"], {"a": 1.0, "b": 1.0}, k=3)
        assert math.isclose(result, 1.0)

    def test_reversed_ranking_below_one(self) -> None:
        # Relevant items pushed to ranks 3, 4 — NDCG < 1.
        result = ndcg_at_k(["x", "y", "a", "b"], {"a": 1.0, "b": 1.0}, k=4)
        assert 0 < result < 1.0

    def test_no_hit_returns_zero(self) -> None:
        assert ndcg_at_k(["x", "y"], {"a": 1.0}, k=2) == 0.0

    def test_empty_relevance_returns_zero(self) -> None:
        assert ndcg_at_k(["a"], {}, k=3) == 0.0

    def test_all_zero_relevance(self) -> None:
        assert ndcg_at_k(["a", "b"], {"a": 0.0, "b": 0.0}, k=2) == 0.0

    def test_graded_relevance_rewards_high_gain_first(self) -> None:
        # Gain-3 at rank 1 + gain-1 at rank 2 beats the reverse.
        correct = ndcg_at_k(["a", "b"], {"a": 3.0, "b": 1.0}, k=2)
        reversed_ = ndcg_at_k(["b", "a"], {"a": 3.0, "b": 1.0}, k=2)
        assert correct > reversed_

    def test_k_zero_returns_zero(self) -> None:
        assert ndcg_at_k(["a"], {"a": 1.0}, k=0) == 0.0


class TestMean:
    def test_normal(self) -> None:
        assert mean([1.0, 2.0, 3.0]) == 2.0

    def test_empty_returns_zero(self) -> None:
        assert mean([]) == 0.0

    def test_generator_input(self) -> None:
        assert mean(x for x in (0.5, 0.5, 0.5)) == 0.5
