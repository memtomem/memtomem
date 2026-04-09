"""Tests for latency percentile computation in TokenTracker."""

from __future__ import annotations


from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker, _percentile


# ── _percentile helper ───────────────────────────────────────────────────


class TestPercentileHelper:
    def test_empty_list(self):
        assert _percentile([], 50) == 0.0

    def test_single_value(self):
        assert _percentile([5.0], 50) == 5.0
        assert _percentile([5.0], 99) == 5.0

    def test_two_values(self):
        # p50 of [1, 3] → 1 + 0.5*(3-1) = 2.0
        assert _percentile([1.0, 3.0], 50) == 2.0

    def test_p0_returns_min(self):
        assert _percentile([1.0, 2.0, 3.0], 0) == 1.0

    def test_p100_returns_max(self):
        assert _percentile([1.0, 2.0, 3.0], 100) == 3.0

    def test_p50_median_odd(self):
        # [1, 2, 3, 4, 5] → median = 3
        assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0

    def test_p50_median_even(self):
        # [1, 2, 3, 4] → k = 1.5 → 2 + 0.5*(3-2) = 2.5
        assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    def test_p95_large_list(self):
        # 100 values: 1..100
        vals = [float(i) for i in range(1, 101)]
        p95 = _percentile(vals, 95)
        # k = 0.95 * 99 = 94.05 → vals[94] + 0.05*(vals[95]-vals[94]) = 95 + 0.05 = 95.05
        assert abs(p95 - 95.05) < 0.001

    def test_p99_large_list(self):
        vals = [float(i) for i in range(1, 101)]
        p99 = _percentile(vals, 99)
        # k = 0.99 * 99 = 98.01 → vals[98] + 0.01*(vals[99]-vals[98]) = 99 + 0.01 = 99.01
        assert abs(p99 - 99.01) < 0.001

    def test_identical_values(self):
        assert _percentile([7.0, 7.0, 7.0, 7.0], 50) == 7.0
        assert _percentile([7.0, 7.0, 7.0, 7.0], 99) == 7.0


# ── TokenTracker percentile integration ──────────────────────────────────


def _make_metrics(
    clean_ms: float = 0.0,
    compress_ms: float = 0.0,
    surface_ms: float = 0.0,
) -> CallMetrics:
    return CallMetrics(
        server="srv",
        tool="tool",
        original_chars=100,
        compressed_chars=50,
        clean_ms=clean_ms,
        compress_ms=compress_ms,
        surface_ms=surface_ms,
    )


class TestTokenTrackerPercentiles:
    def test_no_calls_returns_zeros(self):
        tracker = TokenTracker()
        s = tracker.get_summary()
        lp = s["latency_percentiles"]
        for stage in ("clean_ms", "compress_ms", "surface_ms", "total_ms"):
            assert lp[stage] == {"p50": 0.0, "p95": 0.0, "p99": 0.0}

    def test_single_call(self):
        tracker = TokenTracker()
        tracker.record(_make_metrics(clean_ms=1.0, compress_ms=2.0, surface_ms=3.0))
        lp = tracker.get_summary()["latency_percentiles"]
        assert lp["clean_ms"]["p50"] == 1.0
        assert lp["compress_ms"]["p50"] == 2.0
        assert lp["surface_ms"]["p50"] == 3.0
        assert lp["total_ms"]["p50"] == 6.0

    def test_multiple_calls_median(self):
        tracker = TokenTracker()
        for ms in [10.0, 20.0, 30.0, 40.0, 50.0]:
            tracker.record(_make_metrics(compress_ms=ms))

        lp = tracker.get_summary()["latency_percentiles"]
        assert lp["compress_ms"]["p50"] == 30.0

    def test_p95_p99_spread(self):
        tracker = TokenTracker()
        # 100 calls: compress_ms = 1..100
        for i in range(1, 101):
            tracker.record(_make_metrics(compress_ms=float(i)))

        lp = tracker.get_summary()["latency_percentiles"]
        assert lp["compress_ms"]["p50"] == 50.5  # median of 1..100
        assert lp["compress_ms"]["p95"] > 90
        assert lp["compress_ms"]["p99"] > 98

    def test_total_ms_is_sum_of_stages(self):
        tracker = TokenTracker()
        tracker.record(_make_metrics(clean_ms=5.0, compress_ms=10.0, surface_ms=15.0))
        tracker.record(_make_metrics(clean_ms=1.0, compress_ms=2.0, surface_ms=3.0))

        lp = tracker.get_summary()["latency_percentiles"]
        # totals: [30.0, 6.0] → sorted: [6.0, 30.0] → p50 = 18.0
        assert lp["total_ms"]["p50"] == 18.0

    def test_percentiles_not_affected_by_insertion_order(self):
        """Percentiles are computed on sorted values, not insertion order."""
        tracker1 = TokenTracker()
        tracker2 = TokenTracker()

        ascending = [1.0, 2.0, 3.0, 4.0, 5.0]
        descending = [5.0, 4.0, 3.0, 2.0, 1.0]

        for v in ascending:
            tracker1.record(_make_metrics(compress_ms=v))
        for v in descending:
            tracker2.record(_make_metrics(compress_ms=v))

        lp1 = tracker1.get_summary()["latency_percentiles"]["compress_ms"]
        lp2 = tracker2.get_summary()["latency_percentiles"]["compress_ms"]
        assert lp1 == lp2

    def test_outlier_raises_p99(self):
        """A single slow call raises p99 above the fast-path value."""
        tracker = TokenTracker()
        # 99 fast calls + 1 slow
        for _ in range(99):
            tracker.record(_make_metrics(compress_ms=1.0))
        tracker.record(_make_metrics(compress_ms=1000.0))

        lp = tracker.get_summary()["latency_percentiles"]
        assert lp["compress_ms"]["p50"] == 1.0
        # p99: k=98.01 → 1.0 + 0.01*(1000-1) ≈ 10.99 — outlier pulls p99 above baseline
        assert lp["compress_ms"]["p99"] > 10.0
        assert lp["compress_ms"]["p99"] < lp["compress_ms"]["p50"] + 1000

    def test_all_stages_tracked_independently(self):
        tracker = TokenTracker()
        tracker.record(_make_metrics(clean_ms=100.0, compress_ms=1.0, surface_ms=50.0))
        tracker.record(_make_metrics(clean_ms=200.0, compress_ms=2.0, surface_ms=60.0))

        lp = tracker.get_summary()["latency_percentiles"]
        assert lp["clean_ms"]["p50"] == 150.0
        assert lp["compress_ms"]["p50"] == 1.5
        assert lp["surface_ms"]["p50"] == 55.0
