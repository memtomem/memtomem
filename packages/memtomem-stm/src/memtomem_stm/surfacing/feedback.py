"""Feedback tracking and auto-tuning for surfacing."""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback_store import FeedbackStore

logger = logging.getLogger(__name__)

_VALID_RATINGS = {"helpful", "not_relevant", "already_known"}


class FeedbackTracker:
    """Track surfacing feedback and optionally auto-tune min_score."""

    def __init__(self, config: SurfacingConfig, db_path: Path | None = None) -> None:
        self._config = config
        self._store = FeedbackStore(db_path or Path("~/.memtomem/stm_feedback.db").expanduser())
        self._store.initialize()

    @property
    def store(self) -> FeedbackStore:
        return self._store

    def close(self) -> None:
        self._store.close()

    def record_surfacing(
        self,
        surfacing_id: str,
        server: str,
        tool: str,
        query: str,
        memory_ids: list[str],
        scores: list[float],
    ) -> None:
        self._store.record_surfacing(
            surfacing_id,
            server,
            tool,
            query,
            memory_ids,
            scores,
        )

    def record_feedback(
        self,
        surfacing_id: str,
        rating: str,
        memory_id: str | None = None,
    ) -> str:
        if rating not in _VALID_RATINGS:
            return f"Error: rating must be one of {_VALID_RATINGS}"

        ok = self._store.record_feedback(surfacing_id, rating, memory_id)
        if not ok:
            return f"Error: surfacing event '{surfacing_id}' not found"

        return f"Feedback recorded: {rating}"

    def get_stats(self, tool: str | None = None) -> dict:
        return self._store.get_tool_feedback_summary(tool)


class AutoTuner:
    """Auto-adjust min_score based on feedback ratios.

    Integrated into SurfacingEngine — when `auto_tune_enabled=True` and
    FeedbackTracker is available, the engine calls maybe_adjust(tool)
    and get_effective_min_score(tool) before filtering results.
    """

    def __init__(self, config: SurfacingConfig, store: FeedbackStore) -> None:
        self._config = config
        self._store = store
        self._adjustments: dict[str, float] = {}

    def maybe_adjust(self, tool: str) -> float | None:
        """Check feedback ratio and adjust min_score for a tool.

        When the tool has insufficient samples, falls back to the global
        ratio across all tools (cold-start mitigation).

        Returns new min_score if adjusted, None otherwise.
        """
        if not self._config.auto_tune_enabled:
            return None

        ratio = self._store.get_tool_not_relevant_ratio(
            tool,
            min_samples=self._config.auto_tune_min_samples,
        )
        if ratio is None:
            # Cold start: use global ratio as fallback
            ratio = self._store.get_tool_not_relevant_ratio(
                None,  # None = all tools
                min_samples=self._config.auto_tune_min_samples,
            )
            if ratio is None:
                return None

        current = self._adjustments.get(tool, self._config.min_score)
        increment = self._config.auto_tune_score_increment

        if ratio > 0.6:
            # Too many not_relevant → raise threshold
            new_score = min(current + increment, 0.05)
            if new_score != current:
                self._adjustments[tool] = new_score
                logger.info(
                    "AutoTune: %s min_score %.2f → %.2f (not_relevant ratio: %.0f%%)",
                    tool,
                    current,
                    new_score,
                    ratio * 100,
                )
                return new_score
        elif ratio < 0.2:
            # Mostly helpful → lower threshold (surface more)
            new_score = max(current - increment, 0.005)
            if new_score != current:
                self._adjustments[tool] = new_score
                logger.info(
                    "AutoTune: %s min_score %.2f → %.2f (not_relevant ratio: %.0f%%)",
                    tool,
                    current,
                    new_score,
                    ratio * 100,
                )
                return new_score

        return None

    def get_effective_min_score(self, tool: str) -> float:
        """Return the auto-tuned min_score for a tool, or the default."""
        return self._adjustments.get(tool, self._config.min_score)
