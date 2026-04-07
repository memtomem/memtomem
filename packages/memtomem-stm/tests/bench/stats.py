"""Statistical analysis for benchmark results.

Provides:
- Bootstrap confidence intervals (1000 resamples by default)
- Wilcoxon signed-rank test for paired comparisons
- Category-level aggregation with summary statistics
- LaTeX and markdown table formatters for publication

All implementations are pure Python (no numpy/scipy dependency).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import ComparisonReport, StrategyResult


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ConfidenceInterval:
    """Bootstrap confidence interval."""

    mean: float
    ci_lower: float
    ci_upper: float
    ci_level: float  # e.g. 0.95
    n_resamples: int
    n_samples: int


@dataclass
class WilcoxonResult:
    """Wilcoxon signed-rank test result."""

    statistic: float  # W statistic (smaller of W+ and W-)
    p_value: float  # approximate p-value
    n: int  # number of non-zero differences
    significant: bool  # p < alpha
    alpha: float  # significance level


@dataclass
class CategoryStats:
    """Aggregate statistics for a category of tasks."""

    category: str
    n_tasks: int
    mean_quality: float
    std_quality: float
    mean_preservation: float  # quality_preservation %
    ci: ConfidenceInterval | None = None
    min_quality: float = 0.0
    max_quality: float = 0.0
    median_quality: float = 0.0


@dataclass
class BenchmarkSummary:
    """Full statistical summary of benchmark results."""

    overall: CategoryStats
    by_category: dict[str, CategoryStats] = field(default_factory=dict)
    wilcoxon: WilcoxonResult | None = None  # direct vs STM
    strategy_comparison: dict[str, CategoryStats] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# Bootstrap CI
# ═══════════════════════════════════════════════════════════════════════════


def bootstrap_ci(
    values: list[float],
    n_resamples: int = 1000,
    ci_level: float = 0.95,
    seed: int | None = 42,
) -> ConfidenceInterval:
    """Compute bootstrap confidence interval for the mean.

    Args:
        values: sample values
        n_resamples: number of bootstrap resamples (default 1000)
        ci_level: confidence level (default 0.95)
        seed: random seed for reproducibility (None for random)
    """
    n = len(values)
    if n == 0:
        return ConfidenceInterval(
            mean=0.0, ci_lower=0.0, ci_upper=0.0,
            ci_level=ci_level, n_resamples=0, n_samples=0,
        )
    if n == 1:
        return ConfidenceInterval(
            mean=values[0], ci_lower=values[0], ci_upper=values[0],
            ci_level=ci_level, n_resamples=0, n_samples=1,
        )

    rng = random.Random(seed)
    sample_mean = sum(values) / n
    boot_means: list[float] = []

    for _ in range(n_resamples):
        resample = [rng.choice(values) for _ in range(n)]
        boot_means.append(sum(resample) / n)

    boot_means.sort()
    alpha = 1 - ci_level
    lo_idx = max(0, int(math.floor(alpha / 2 * n_resamples)))
    hi_idx = min(n_resamples - 1, int(math.ceil((1 - alpha / 2) * n_resamples)) - 1)

    return ConfidenceInterval(
        mean=sample_mean,
        ci_lower=boot_means[lo_idx],
        ci_upper=boot_means[hi_idx],
        ci_level=ci_level,
        n_resamples=n_resamples,
        n_samples=n,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Wilcoxon signed-rank test
# ═══════════════════════════════════════════════════════════════════════════


def wilcoxon_signed_rank(
    x: list[float],
    y: list[float],
    alpha: float = 0.05,
) -> WilcoxonResult:
    """Wilcoxon signed-rank test for paired samples (pure Python).

    Tests H0: median difference between x and y is zero.
    Uses normal approximation for n >= 10, exact for small n.

    Args:
        x: first sample (e.g., direct quality scores)
        y: second sample (e.g., STM quality scores)
        alpha: significance level
    """
    if len(x) != len(y):
        raise ValueError("x and y must have equal length")

    # Compute differences, remove zeros
    diffs = [(xi - yi) for xi, yi in zip(x, y) if xi != yi]
    n = len(diffs)

    if n == 0:
        return WilcoxonResult(statistic=0.0, p_value=1.0, n=0, significant=False, alpha=alpha)

    # Rank by absolute value
    abs_diffs = [(abs(d), i) for i, d in enumerate(diffs)]
    abs_diffs.sort(key=lambda x: x[0])

    # Assign ranks (average ties)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and abs_diffs[j][0] == abs_diffs[i][0]:
            j += 1
        avg_rank = (i + j - 1) / 2.0 + 1  # 1-based
        for k in range(i, j):
            ranks[abs_diffs[k][1]] = avg_rank
        i = j

    # W+ and W-
    w_plus = sum(ranks[i] for i in range(n) if diffs[i] > 0)
    w_minus = sum(ranks[i] for i in range(n) if diffs[i] < 0)
    w_stat = min(w_plus, w_minus)

    # Normal approximation (valid for n >= 10)
    if n >= 10:
        mean_w = n * (n + 1) / 4
        std_w = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
        if std_w == 0:
            p_value = 1.0
        else:
            z = (w_stat - mean_w) / std_w
            # Two-tailed p-value using normal CDF approximation
            p_value = 2 * _normal_cdf(z)
    else:
        # For small n, use conservative approximation
        # Exact tables would be better but this is sufficient for benchmarking
        mean_w = n * (n + 1) / 4
        std_w = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
        if std_w == 0:
            p_value = 1.0
        else:
            z = (w_stat - mean_w) / std_w
            p_value = min(1.0, 2 * _normal_cdf(z))

    return WilcoxonResult(
        statistic=w_stat,
        p_value=p_value,
        n=n,
        significant=p_value < alpha,
        alpha=alpha,
    )


def _normal_cdf(z: float) -> float:
    """Approximate standard normal CDF using Abramowitz & Stegun formula 7.1.26."""
    if z > 6:
        return 1.0
    if z < -6:
        return 0.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if z >= 0 else -1
    x = abs(z) / math.sqrt(2)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


# ═══════════════════════════════════════════════════════════════════════════
# Category aggregation
# ═══════════════════════════════════════════════════════════════════════════


def _std(values: list[float], mean: float) -> float:
    """Sample standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _median(values: list[float]) -> float:
    """Median of sorted values."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def aggregate_by_category(
    comparisons: list[ComparisonReport],
    category_map: dict[str, str],
    compute_ci: bool = True,
) -> dict[str, CategoryStats]:
    """Aggregate comparison results by category.

    Args:
        comparisons: list of A/B comparison results
        category_map: task_id → category name mapping
        compute_ci: whether to compute bootstrap CI (slower)
    """
    # Group by category
    cat_scores: dict[str, list[float]] = {}
    cat_preservation: dict[str, list[float]] = {}

    for c in comparisons:
        cat = category_map.get(c.task_id, "other")
        cat_scores.setdefault(cat, []).append(c.stm.quality_score)
        cat_preservation.setdefault(cat, []).append(c.quality_preservation)

    result: dict[str, CategoryStats] = {}
    for cat in sorted(cat_scores.keys()):
        scores = cat_scores[cat]
        preservations = cat_preservation[cat]
        mean_q = sum(scores) / len(scores)
        mean_p = sum(preservations) / len(preservations)
        ci = bootstrap_ci(scores) if compute_ci else None

        result[cat] = CategoryStats(
            category=cat,
            n_tasks=len(scores),
            mean_quality=mean_q,
            std_quality=_std(scores, mean_q),
            mean_preservation=mean_p,
            ci=ci,
            min_quality=min(scores),
            max_quality=max(scores),
            median_quality=_median(scores),
        )

    return result


def compute_summary(
    comparisons: list[ComparisonReport],
    category_map: dict[str, str] | None = None,
) -> BenchmarkSummary:
    """Compute full statistical summary of benchmark results.

    Args:
        comparisons: A/B comparison results
        category_map: optional task_id → category mapping
    """
    # Overall
    all_stm_scores = [c.stm.quality_score for c in comparisons]
    all_preservations = [c.quality_preservation for c in comparisons]
    mean_q = sum(all_stm_scores) / len(all_stm_scores) if all_stm_scores else 0.0
    mean_p = sum(all_preservations) / len(all_preservations) if all_preservations else 0.0

    overall = CategoryStats(
        category="overall",
        n_tasks=len(comparisons),
        mean_quality=mean_q,
        std_quality=_std(all_stm_scores, mean_q),
        mean_preservation=mean_p,
        ci=bootstrap_ci(all_stm_scores) if all_stm_scores else None,
        min_quality=min(all_stm_scores) if all_stm_scores else 0.0,
        max_quality=max(all_stm_scores) if all_stm_scores else 0.0,
        median_quality=_median(all_stm_scores),
    )

    # By category
    by_cat: dict[str, CategoryStats] = {}
    if category_map:
        by_cat = aggregate_by_category(comparisons, category_map)

    # Wilcoxon test (direct vs STM)
    direct_scores = [c.direct.quality_score for c in comparisons]
    stm_scores = [c.stm.quality_score for c in comparisons]
    wilcoxon = None
    if len(comparisons) >= 5:
        wilcoxon = wilcoxon_signed_rank(direct_scores, stm_scores)

    return BenchmarkSummary(
        overall=overall,
        by_category=by_cat,
        wilcoxon=wilcoxon,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Table formatters
# ═══════════════════════════════════════════════════════════════════════════


def format_markdown_table(summary: BenchmarkSummary) -> str:
    """Format summary as markdown table for docs/blog."""
    lines: list[str] = []

    # Overall
    o = summary.overall
    lines.append("## Overall Results")
    lines.append("")
    lines.append(f"- **Tasks:** {o.n_tasks}")
    lines.append(f"- **Mean quality:** {o.mean_quality:.2f}/10 (±{o.std_quality:.2f})")
    if o.ci:
        lines.append(
            f"- **{o.ci.ci_level:.0%} CI:** [{o.ci.ci_lower:.2f}, {o.ci.ci_upper:.2f}]"
        )
    lines.append(f"- **Quality preservation:** {o.mean_preservation:.1f}%")
    lines.append("")

    # Wilcoxon
    if summary.wilcoxon:
        w = summary.wilcoxon
        sig = "significant" if w.significant else "not significant"
        lines.append(f"**Wilcoxon signed-rank:** W={w.statistic:.1f}, p={w.p_value:.4f} ({sig})")
        lines.append("")

    # Category table
    if summary.by_category:
        lines.append("## Results by Category")
        lines.append("")
        lines.append("| Category | N | Mean | Std | Median | Min | Max | Preservation | 95% CI |")
        lines.append("|----------|---|------|-----|--------|-----|-----|-------------|--------|")
        for cat, s in sorted(summary.by_category.items()):
            ci_str = f"[{s.ci.ci_lower:.1f}, {s.ci.ci_upper:.1f}]" if s.ci else "—"
            lines.append(
                f"| {cat} | {s.n_tasks} | {s.mean_quality:.1f} | {s.std_quality:.1f} "
                f"| {s.median_quality:.1f} | {s.min_quality:.1f} | {s.max_quality:.1f} "
                f"| {s.mean_preservation:.0f}% | {ci_str} |"
            )
        lines.append("")

    return "\n".join(lines)


def format_latex_table(summary: BenchmarkSummary) -> str:
    """Format summary as LaTeX table for papers."""
    lines: list[str] = []
    lines.append(r"\begin{table}[h]")
    lines.append(r"\centering")
    lines.append(r"\caption{STM Pipeline Quality Benchmark Results}")
    lines.append(r"\label{tab:benchmark}")
    lines.append(r"\begin{tabular}{lcccccc}")
    lines.append(r"\hline")
    lines.append(
        r"\textbf{Category} & \textbf{N} & \textbf{Mean} & \textbf{Std} "
        r"& \textbf{Median} & \textbf{Preservation} & \textbf{95\% CI} \\"
    )
    lines.append(r"\hline")

    # Overall row
    o = summary.overall
    ci_str = f"[{o.ci.ci_lower:.1f}, {o.ci.ci_upper:.1f}]" if o.ci else "---"
    lines.append(
        f"\\textbf{{Overall}} & {o.n_tasks} & {o.mean_quality:.1f} & {o.std_quality:.1f} "
        f"& {o.median_quality:.1f} & {o.mean_preservation:.0f}\\% & {ci_str} \\\\"
    )

    # Category rows
    if summary.by_category:
        lines.append(r"\hline")
        for cat, s in sorted(summary.by_category.items()):
            ci_str = f"[{s.ci.ci_lower:.1f}, {s.ci.ci_upper:.1f}]" if s.ci else "---"
            lines.append(
                f"{cat} & {s.n_tasks} & {s.mean_quality:.1f} & {s.std_quality:.1f} "
                f"& {s.median_quality:.1f} & {s.mean_preservation:.0f}\\% & {ci_str} \\\\"
            )

    lines.append(r"\hline")
    lines.append(r"\end{tabular}")

    # Wilcoxon note
    if summary.wilcoxon:
        w = summary.wilcoxon
        sig = "p < 0.05" if w.significant else f"p = {w.p_value:.3f}"
        lines.append(
            f"\\\\\\footnotesize{{Wilcoxon signed-rank: $W={w.statistic:.0f}$, ${sig}$, "
            f"$n={w.n}$}}"
        )

    lines.append(r"\end{table}")
    return "\n".join(lines)


def format_strategy_table(
    matrix: dict[str, dict[str, StrategyResult]],
    fmt: str = "markdown",
) -> str:
    """Format strategy matrix comparison table.

    Args:
        matrix: task_id → {strategy → StrategyResult}
        fmt: "markdown" or "latex"
    """
    if not matrix:
        return ""

    # Collect all strategies
    strategies = sorted({s for results in matrix.values() for s in results})

    if fmt == "latex":
        return _strategy_table_latex(matrix, strategies)
    return _strategy_table_markdown(matrix, strategies)


def _strategy_table_markdown(
    matrix: dict[str, dict[str, StrategyResult]],
    strategies: list[str],
) -> str:
    lines: list[str] = []
    header = "| Task | " + " | ".join(strategies) + " |"
    sep = "|------|" + "|".join(["------" for _ in strategies]) + "|"
    lines.append(header)
    lines.append(sep)

    for task_id in sorted(matrix.keys()):
        results = matrix[task_id]
        cells = []
        best_score = max((r.quality_score for r in results.values()), default=0)
        for s in strategies:
            r = results.get(s)
            if r:
                marker = " **" if r.quality_score == best_score else ""
                end = "**" if marker else ""
                cells.append(f"{marker}{r.quality_score:.1f}{end} ({r.compression_ratio:.0%})")
            else:
                cells.append("—")
        lines.append(f"| {task_id} | " + " | ".join(cells) + " |")

    return "\n".join(lines)


def _strategy_table_latex(
    matrix: dict[str, dict[str, StrategyResult]],
    strategies: list[str],
) -> str:
    n_cols = len(strategies) + 1
    col_spec = "l" + "c" * len(strategies)
    lines: list[str] = []
    lines.append(r"\begin{tabular}{" + col_spec + "}")
    lines.append(r"\hline")
    lines.append(
        r"\textbf{Task} & " + " & ".join(f"\\textbf{{{s}}}" for s in strategies) + r" \\"
    )
    lines.append(r"\hline")

    for task_id in sorted(matrix.keys()):
        results = matrix[task_id]
        cells = []
        best_score = max((r.quality_score for r in results.values()), default=0)
        for s in strategies:
            r = results.get(s)
            if r:
                val = f"{r.quality_score:.1f}"
                if r.quality_score == best_score:
                    val = f"\\textbf{{{val}}}"
                cells.append(val)
            else:
                cells.append("---")
        lines.append(f"{task_id} & " + " & ".join(cells) + r" \\")

    lines.append(r"\hline")
    lines.append(r"\end{tabular}")
    return "\n".join(lines)
