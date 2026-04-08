"""Run full 35-task benchmark and print quality report.

Usage:
    uv run pytest packages/memtomem-stm/tests/test_bench_report.py -v -s
"""

from __future__ import annotations

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import TruncateCompressor

from bench.datasets_expanded import full_benchmark_suite, full_category_map
from bench.harness import BenchHarness, ComparisonReport
from bench.judge import RuleBasedJudge
from bench.stats import compute_summary


@pytest.fixture
def harness():
    return BenchHarness(
        cleaner=DefaultContentCleaner(),
        compressor=TruncateCompressor(),
        judge=RuleBasedJudge(),
    )


class TestBenchReport:
    def test_full_quality_report(self, harness: BenchHarness, capsys):
        """Run full 35-task suite with AUTO strategy and print report."""
        tasks = full_benchmark_suite()
        tasks = [t for t in tasks if len(t.content) > 0]

        # Run with AUTO strategy
        comparisons = [harness.run_auto_strategy(t) for t in tasks]
        cat_map = full_category_map()
        summary = compute_summary(comparisons, category_map=cat_map)

        # Separate compressed vs passthrough
        compressed = [c for c in comparisons if c.stm.text != c.direct.text]
        passthrough = [c for c in comparisons if c.stm.text == c.direct.text]

        compressed_scores = [c.stm.quality_score for c in compressed]
        all_scores = [c.stm.quality_score for c in comparisons]

        overall_avg = sum(all_scores) / len(all_scores) if all_scores else 0
        compressed_avg = sum(compressed_scores) / len(compressed_scores) if compressed_scores else 0

        # Compression savings
        total_original = sum(len(c.direct.text) for c in compressed)
        total_compressed = sum(len(c.stm.text) for c in compressed)
        savings_pct = (1 - total_compressed / total_original) * 100 if total_original else 0

        # Low-scoring tasks
        low = [(c.task_id, c.stm.quality_score) for c in comparisons if c.stm.quality_score < 6.0]

        # Per-task detail
        print("\n" + "=" * 70)
        print("BENCHMARK REPORT — v0.1.0")
        print("=" * 70)
        print(f"\nTasks: {len(comparisons)} ({len(compressed)} compressed, {len(passthrough)} passthrough)")
        print(f"Overall quality:      {overall_avg:.2f}/10")
        print(f"Compressed-only:      {compressed_avg:.2f}/10")
        print(f"Compression savings:  {savings_pct:.1f}%")
        print(f"Low-scoring (<6.0):   {len(low)}")

        if low:
            for tid, score in low:
                print(f"  - {tid}: {score:.1f}")

        print(f"\n{'Task':<35} {'Score':>6} {'Ratio':>8} {'Strategy':>12}")
        print("-" * 65)
        for c in sorted(comparisons, key=lambda x: x.stm.quality_score):
            ratio = len(c.stm.text) / len(c.direct.text) if c.direct.text else 1.0
            is_pass = "pass" if c.stm.text == c.direct.text else ""
            strategy_label = is_pass or f"{ratio:.1%}"
            print(f"  {c.task_id:<33} {c.stm.quality_score:>6.1f} {strategy_label:>8}")

        # Category breakdown
        print(f"\n{'Category':<20} {'N':>4} {'Mean':>6} {'Std':>6}")
        print("-" * 40)
        for cat, stats in sorted(summary.by_category.items()):
            print(f"  {cat:<18} {stats.n_tasks:>4} {stats.mean_quality:>6.2f} {stats.std_quality:>6.2f}")
        print(f"  {'OVERALL':<18} {summary.overall.n_tasks:>4} {summary.overall.mean_quality:>6.2f} {summary.overall.std_quality:>6.2f}")
        print("=" * 70)

        # Assertions
        assert summary.overall.mean_quality >= 8.0, f"Overall quality {summary.overall.mean_quality:.2f} < 8.0"
        assert len(low) == 0, f"{len(low)} tasks below 6.0: {low}"
        assert savings_pct > 50, f"Compression savings {savings_pct:.1f}% < 50%"
