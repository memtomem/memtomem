"""Relevance gating — decide when to surface memories."""

from __future__ import annotations

import time
from collections import deque
from fnmatch import fnmatch

from memtomem_stm.surfacing.config import SurfacingConfig


class RelevanceGate:
    """Determine whether to run proactive surfacing for a given tool call."""

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config
        self._recent_queries: deque[tuple[float, str]] = deque(maxlen=50)
        self._surfacing_timestamps: deque[float] = deque(maxlen=200)

    def should_surface(
        self,
        server: str,
        tool: str,
        query: str | None,
    ) -> bool:
        if not self._config.enabled or query is None:
            return False

        full_name = f"{server}__{tool}"

        # Explicit exclusions
        for pattern in self._config.exclude_tools:
            if fnmatch(full_name, pattern) or fnmatch(tool, pattern):
                return False

        # Write-tool heuristic
        for pattern in self._config.write_tool_patterns:
            if fnmatch(tool, pattern):
                return False

        # Per-tool override
        tool_cfg = self._config.context_tools.get(tool)
        if tool_cfg is not None and not tool_cfg.enabled:
            return False

        # Rate limit
        now = time.monotonic()
        while self._surfacing_timestamps and now - self._surfacing_timestamps[0] > 60.0:
            self._surfacing_timestamps.popleft()
        if len(self._surfacing_timestamps) >= self._config.max_surfacings_per_minute:
            return False

        # Cooldown: skip if very similar query was recently surfaced
        for ts, prev_query in reversed(self._recent_queries):
            if now - ts >= self._config.cooldown_seconds:
                break
            if self._jaccard_similarity(query, prev_query) > 0.95:
                return False

        self._recent_queries.append((now, query))
        self._surfacing_timestamps.append(now)
        return True

    @staticmethod
    def _jaccard_similarity(a: str, b: str) -> float:
        tokens_a = set(a.lower().split())
        tokens_b = set(b.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)
