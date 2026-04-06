"""Proxy call metrics and token tracking."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem_stm.proxy.metrics_store import MetricsStore

logger = logging.getLogger(__name__)


@dataclass
class CallMetrics:
    server: str
    tool: str
    original_chars: int
    compressed_chars: int
    cleaned_chars: int = 0
    original_tokens: int = 0
    compressed_tokens: int = 0
    trace_id: str | None = None
    # Per-stage timing (ms) and surfacing size
    clean_ms: float = 0.0
    compress_ms: float = 0.0
    surface_ms: float = 0.0
    surfaced_chars: int = 0


class TokenTracker:
    """Aggregate proxy call metrics (in-memory + optional persistent store)."""

    def __init__(self, metrics_store: MetricsStore | None = None) -> None:
        self._total_calls = 0
        self._total_original = 0
        self._total_compressed = 0
        self._total_surfaced = 0
        self._total_clean_ms = 0.0
        self._total_compress_ms = 0.0
        self._total_surface_ms = 0.0
        self._cache_hits = 0
        self._cache_misses = 0
        self._reconnects = 0
        self._metrics_store = metrics_store
        self._by_server: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "original_chars": 0, "compressed_chars": 0}
        )
        self._by_tool: dict[str, dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "original_chars": 0, "compressed_chars": 0}
        )

    def record(self, metrics: CallMetrics) -> None:
        self._total_calls += 1
        self._total_original += metrics.original_chars
        self._total_compressed += metrics.compressed_chars
        self._total_surfaced += metrics.surfaced_chars
        self._total_clean_ms += metrics.clean_ms
        self._total_compress_ms += metrics.compress_ms
        self._total_surface_ms += metrics.surface_ms

        s = self._by_server[metrics.server]
        s["calls"] += 1
        s["original_chars"] += metrics.original_chars
        s["compressed_chars"] += metrics.compressed_chars

        t = self._by_tool[f"{metrics.server}/{metrics.tool}"]
        t["calls"] += 1
        t["original_chars"] += metrics.original_chars
        t["compressed_chars"] += metrics.compressed_chars

        # Persist to SQLite
        if self._metrics_store is not None:
            try:
                self._metrics_store.record(metrics)
            except Exception:
                logger.debug("Failed to persist metrics", exc_info=True)

    def record_cache_hit(self) -> None:
        self._cache_hits += 1

    def record_cache_miss(self) -> None:
        self._cache_misses += 1

    def record_reconnect(self) -> None:
        self._reconnects += 1

    def get_summary(self) -> dict:
        savings = (
            round((1 - self._total_compressed / self._total_original) * 100, 1)
            if self._total_original > 0
            else 0.0
        )

        by_server = {}
        for name, s in self._by_server.items():
            pct = (
                round((1 - s["compressed_chars"] / s["original_chars"]) * 100, 1)
                if s["original_chars"] > 0
                else 0.0
            )
            by_server[name] = {**s, "savings_pct": pct}

        n = self._total_calls or 1
        return {
            "total_calls": self._total_calls,
            "total_original_chars": self._total_original,
            "total_compressed_chars": self._total_compressed,
            "total_surfaced_chars": self._total_surfaced,
            "total_savings_pct": savings,
            "avg_clean_ms": round(self._total_clean_ms / n, 2),
            "avg_compress_ms": round(self._total_compress_ms / n, 2),
            "avg_surface_ms": round(self._total_surface_ms / n, 2),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "reconnects": self._reconnects,
            "by_server": by_server,
        }
