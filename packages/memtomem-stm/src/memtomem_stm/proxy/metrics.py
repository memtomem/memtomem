"""Proxy call metrics and token tracking."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem_stm.proxy.metrics_store import MetricsStore

logger = logging.getLogger(__name__)


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Compute the *p*-th percentile (0-100) from a pre-sorted list.

    Uses linear interpolation between closest ranks (same as numpy 'linear').
    """
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (p / 100) * (n - 1)
    lo = int(math.floor(k))
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


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
        self._total_original_tokens = 0
        self._total_compressed_tokens = 0
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
        # Per-call latencies for percentile computation
        self._clean_latencies: list[float] = []
        self._compress_latencies: list[float] = []
        self._surface_latencies: list[float] = []
        self._total_latencies: list[float] = []

    def record(self, metrics: CallMetrics) -> None:
        self._total_calls += 1
        self._total_original += metrics.original_chars
        self._total_compressed += metrics.compressed_chars
        self._total_surfaced += metrics.surfaced_chars
        self._total_original_tokens += metrics.original_tokens
        self._total_compressed_tokens += metrics.compressed_tokens
        self._total_clean_ms += metrics.clean_ms
        self._total_compress_ms += metrics.compress_ms
        self._total_surface_ms += metrics.surface_ms

        self._clean_latencies.append(metrics.clean_ms)
        self._compress_latencies.append(metrics.compress_ms)
        self._surface_latencies.append(metrics.surface_ms)
        self._total_latencies.append(
            metrics.clean_ms + metrics.compress_ms + metrics.surface_ms
        )

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

    def _percentiles(self, values: list[float]) -> dict[str, float]:
        """Return p50/p95/p99 for a list of latency values."""
        if not values:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        s = sorted(values)
        return {
            "p50": round(_percentile(s, 50), 2),
            "p95": round(_percentile(s, 95), 2),
            "p99": round(_percentile(s, 99), 2),
        }

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
            "total_original_tokens": self._total_original_tokens,
            "total_compressed_tokens": self._total_compressed_tokens,
            "total_token_savings_pct": (
                round((1 - self._total_compressed_tokens / self._total_original_tokens) * 100, 1)
                if self._total_original_tokens > 0
                else 0.0
            ),
            "total_savings_pct": savings,
            "avg_clean_ms": round(self._total_clean_ms / n, 2),
            "avg_compress_ms": round(self._total_compress_ms / n, 2),
            "avg_surface_ms": round(self._total_surface_ms / n, 2),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "reconnects": self._reconnects,
            "latency_percentiles": {
                "clean_ms": self._percentiles(self._clean_latencies),
                "compress_ms": self._percentiles(self._compress_latencies),
                "surface_ms": self._percentiles(self._surface_latencies),
                "total_ms": self._percentiles(self._total_latencies),
            },
            "by_server": by_server,
        }
