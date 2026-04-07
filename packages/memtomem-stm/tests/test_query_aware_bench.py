"""Benchmark: query-aware vs baseline truncation quality comparison.

Runs tasks with context_query and compares QA scores.

    uv run pytest packages/memtomem-stm/tests/test_query_aware_bench.py -v -s
"""

from __future__ import annotations

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import TruncateCompressor

from bench.datasets_expanded import CONTEXT_QUERIES, full_benchmark_suite
from bench.harness import BenchHarness
from bench.judge import RuleBasedJudge


class TestQueryAwareBench:
    def test_query_aware_vs_baseline(self, capsys):
        """Compare quality: truncate (no query) vs truncate (with query)."""
        harness = BenchHarness(
            cleaner=DefaultContentCleaner(),
            compressor=TruncateCompressor(),
            judge=RuleBasedJudge(),
        )
        tasks = full_benchmark_suite()
        # Only test tasks with context_query AND enough content to compress
        tasks = [t for t in tasks if t.context_query and len(t.content) > t.max_chars]

        baseline_scores: list[float] = []
        query_scores: list[float] = []
        improvements: list[tuple[str, float, float]] = []

        for t in tasks:
            report = harness.run_query_aware_comparison(t)
            baseline_scores.append(report.direct.quality_score)
            query_scores.append(report.stm.quality_score)
            diff = report.stm.quality_score - report.direct.quality_score
            improvements.append((t.task_id, report.direct.quality_score, report.stm.quality_score))

        # Print report
        print("\n" + "=" * 70)
        print("QUERY-AWARE vs BASELINE TRUNCATION")
        print("=" * 70)
        print(f"\nTasks evaluated: {len(tasks)}")

        avg_baseline = sum(baseline_scores) / len(baseline_scores) if baseline_scores else 0
        avg_query = sum(query_scores) / len(query_scores) if query_scores else 0
        print(f"Baseline avg:    {avg_baseline:.2f}/10")
        print(f"Query-aware avg: {avg_query:.2f}/10")
        print(f"Delta:           {avg_query - avg_baseline:+.2f}")

        improved = sum(1 for _, b, q in improvements if q > b)
        same = sum(1 for _, b, q in improvements if q == b)
        degraded = sum(1 for _, b, q in improvements if q < b)
        print(f"\nImproved: {improved}  Same: {same}  Degraded: {degraded}")

        print(f"\n{'Task':<30} {'Baseline':>8} {'Query':>8} {'Delta':>8}")
        print("-" * 58)
        for tid, b, q in sorted(improvements, key=lambda x: x[2] - x[1], reverse=True):
            delta = q - b
            marker = "+" if delta > 0 else (" " if delta == 0 else "")
            print(f"  {tid:<28} {b:>8.1f} {q:>8.1f} {marker}{delta:>7.1f}")
        print("=" * 70)

        # No regressions beyond noise
        assert avg_query >= avg_baseline - 0.5, (
            f"Query-aware avg ({avg_query:.2f}) regressed significantly vs baseline ({avg_baseline:.2f})"
        )
