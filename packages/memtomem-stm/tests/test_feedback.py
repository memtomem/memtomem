"""Tests for FeedbackStore, FeedbackTracker, and AutoTuner."""

from __future__ import annotations

from pathlib import Path

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback import AutoTuner, FeedbackTracker
from memtomem_stm.surfacing.feedback_store import FeedbackStore


# ---------------------------------------------------------------------------
# FeedbackStore
# ---------------------------------------------------------------------------


class TestFeedbackStore:
    def test_record_and_retrieve_surfacing(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing(
            "surf1", "server", "tool", "query", ["mem1", "mem2"], [0.9, 0.8]
        )
        stats = feedback_store.get_tool_feedback_summary("tool")
        assert stats["total_surfacings"] == 1

    def test_record_feedback_valid_id(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("surf1", "s", "t", "q", ["m1"], [0.9])
        ok = feedback_store.record_feedback("surf1", "helpful")
        assert ok is True

    def test_record_feedback_unknown_id(self, feedback_store: FeedbackStore):
        ok = feedback_store.record_feedback("nonexistent", "helpful")
        assert ok is False

    def test_feedback_summary_by_rating(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "tool_a", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s1", "not_relevant")

        stats = feedback_store.get_tool_feedback_summary("tool_a")
        assert stats["total_feedback"] == 3
        assert stats["by_rating"]["helpful"] == 2
        assert stats["by_rating"]["not_relevant"] == 1

    def test_feedback_summary_all_tools(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t1", "q", ["m1"], [0.5])
        feedback_store.record_surfacing("s2", "sv", "t2", "q", ["m2"], [0.5])
        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s2", "not_relevant")

        stats = feedback_store.get_tool_feedback_summary()
        assert stats["total_surfacings"] == 2
        assert stats["total_feedback"] == 2

    def test_not_relevant_ratio_insufficient_samples(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "helpful")
        ratio = feedback_store.get_tool_not_relevant_ratio("t", min_samples=20)
        assert ratio is None

    def test_not_relevant_ratio_computed(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        for i in range(20):
            feedback_store.record_feedback("s1", "not_relevant" if i < 12 else "helpful")

        ratio = feedback_store.get_tool_not_relevant_ratio("t", min_samples=20)
        assert ratio is not None
        assert abs(ratio - 0.6) < 0.01


# ---------------------------------------------------------------------------
# FeedbackTracker
# ---------------------------------------------------------------------------


class TestFeedbackTracker:
    def test_invalid_rating_rejected(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            result = tracker.record_feedback("s1", "invalid_rating")
            assert "Error" in result
        finally:
            tracker.close()

    def test_valid_feedback_recorded(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            tracker.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
            result = tracker.record_feedback("s1", "helpful")
            assert "recorded" in result.lower()
        finally:
            tracker.close()

    def test_get_stats(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            stats = tracker.get_stats()
            assert "total_surfacings" in stats
        finally:
            tracker.close()


# ---------------------------------------------------------------------------
# AutoTuner
# ---------------------------------------------------------------------------


class TestAutoTuner:
    def _make_tuner(
        self, feedback_store: FeedbackStore, min_score: float = 0.02
    ) -> AutoTuner:
        cfg = SurfacingConfig(
            auto_tune_enabled=True,
            auto_tune_min_samples=5,
            auto_tune_score_increment=0.005,
            min_score=min_score,
        )
        return AutoTuner(cfg, feedback_store)

    def _seed_feedback(
        self, store: FeedbackStore, tool: str, not_relevant: int, helpful: int
    ):
        store.record_surfacing("s1", "sv", tool, "q", ["m1"], [0.5])
        for _ in range(not_relevant):
            store.record_feedback("s1", "not_relevant")
        for _ in range(helpful):
            store.record_feedback("s1", "helpful")

    def test_high_not_relevant_raises_threshold(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=5, helpful=1)
        tuner = self._make_tuner(feedback_store)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result > 0.02

    def test_low_not_relevant_lowers_threshold(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=1, helpful=10)
        tuner = self._make_tuner(feedback_store)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result < 0.02

    def test_insufficient_samples_no_adjustment(self, feedback_store: FeedbackStore):
        store = feedback_store
        store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        store.record_feedback("s1", "not_relevant")
        tuner = self._make_tuner(store)
        assert tuner.maybe_adjust("t") is None

    def test_upper_bound_respected(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=10, helpful=0)
        tuner = self._make_tuner(feedback_store, min_score=0.048)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result <= 0.05

    def test_lower_bound_respected(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=0, helpful=10)
        tuner = self._make_tuner(feedback_store, min_score=0.007)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result >= 0.005

    def test_get_effective_min_score_default(self, feedback_store: FeedbackStore):
        tuner = self._make_tuner(feedback_store)
        assert tuner.get_effective_min_score("unknown_tool") == 0.02

    def test_get_effective_min_score_adjusted(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=8, helpful=0)
        tuner = self._make_tuner(feedback_store)
        tuner.maybe_adjust("t")
        score = tuner.get_effective_min_score("t")
        assert score > 0.02
