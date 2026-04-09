"""Benchmark report formatter — comparison, matrix, and curve reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import ComparisonReport, CurvePoint, StageBreakdown, StrategyResult, SurfacingValue


def format_report(comparisons: list[ComparisonReport]) -> str:
    """Format benchmark comparisons into a readable report."""
    lines: list[str] = []
    lines.append("=== memtomem STM Pipeline Benchmark ===")
    lines.append("")

    total_quality = 0.0
    total_reduction = 0.0
    total_overhead = 0.0
    passed = 0
    count = 0

    for c in comparisons:
        count += 1
        lines.append(f"Task: {c.task_id}")

        d = c.direct
        lines.append(f"  Direct:      {len(d.text)} chars → quality: {d.quality_score:.1f}/10")

        s = c.stm
        m = s.stage_metrics
        if m:
            lines.append(
                f"  STM-proxied: {m.original_chars} → {m.cleaned_chars} → "
                f"{m.compressed_chars} (+{m.surfaced_chars - m.compressed_chars} surfacing) "
                f"→ quality: {s.quality_score:.1f}/10"
            )
            lines.append(
                f"  Compression: clean {(m.cleaning_ratio - 1) * 100:+.0f}%, "
                f"compress {(m.compression_ratio - 1) * 100:+.0f}%, "
                f"surface {m.surfacing_overhead * 100:+.0f}%"
            )
            lines.append(
                f"  Timing: clean {m.clean_ms:.1f}ms, "
                f"compress {m.compress_ms:.1f}ms, "
                f"surface {m.surface_ms:.1f}ms"
            )
            if m.strategy != "unknown":
                lines.append(f"  Strategy: {m.strategy}")
        else:
            lines.append(f"  STM-proxied: quality: {s.quality_score:.1f}/10")

        qp = c.quality_preservation
        total_quality += qp
        total_reduction += (1 - c.total_reduction) * 100
        total_overhead += c.surfacing_overhead * 100

        if qp < 80.0:
            lines.append(f"  ⚠️  Quality preservation: {qp:.1f}% (below 80% threshold)")
        else:
            lines.append(f"  Quality preservation: {qp:.1f}%")
            passed += 1

        if s.error:
            lines.append(f"  ERROR: {s.error}")

        lines.append("")

    # Summary
    lines.append("--- Summary ---")
    if count > 0:
        lines.append(f"  Tasks: {count}")
        lines.append(f"  Passed (≥80% quality): {passed}/{count}")
        lines.append(f"  Avg quality preservation: {total_quality / count:.1f}%")
        lines.append(f"  Avg compression: {total_reduction / count:.0f}%")
        lines.append(f"  Avg surfacing overhead: {total_overhead / count:.0f}%")
    else:
        lines.append("  No tasks run.")

    return "\n".join(lines)


def format_matrix(
    task_id: str,
    results: dict[str, StrategyResult],
    optimal: str | None = None,
) -> str:
    """Format strategy matrix for a single task."""
    lines = [f"Strategy matrix: {task_id}"]
    lines.append(f"  {'Strategy':<25} {'Quality':>8} {'Ratio':>8} {'Chars':>8}")
    lines.append(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 8}")

    best_strategy = ""
    best_score = -1.0
    for name, r in sorted(results.items(), key=lambda x: -x[1].quality_score):
        marker = ""
        if r.quality_score > best_score:
            best_score = r.quality_score
            best_strategy = name
        lines.append(
            f"  {name:<25} {r.quality_score:>7.1f} {r.compression_ratio:>7.1%} {r.compressed_chars:>8}"
        )

    if optimal:
        # Check if auto matches optimal
        auto_key = [k for k in results if k.startswith("auto(")]
        auto_matches = any(optimal in k for k in auto_key) if auto_key else False
        lines.append(f"  Optimal: {optimal} | Best: {best_strategy} | Auto correct: {auto_matches}")

    return "\n".join(lines)


def format_curve(task_id: str, points: list[CurvePoint]) -> str:
    """Format compression curve for a single task."""
    lines = [f"Compression curve: {task_id} ({points[0].strategy if points else '?'})"]
    lines.append(f"  {'Budget':>8} {'MaxChars':>10} {'Actual':>8} {'Quality':>8}")
    lines.append(f"  {'-' * 8} {'-' * 10} {'-' * 8} {'-' * 8}")

    for p in points:
        lines.append(
            f"  {p.budget_ratio:>7.0%} {p.max_chars:>10} {p.compressed_chars:>8} {p.quality_score:>7.1f}"
        )

    # Monotonicity check
    scores = [p.quality_score for p in points]
    is_monotone = all(s1 <= s2 for s1, s2 in zip(scores, scores[1:]))
    if not is_monotone:
        lines.append("  ⚠️  Non-monotonic: more budget doesn't always mean more quality")

    return "\n".join(lines)


def format_full_report(
    comparisons: list[ComparisonReport],
    matrices: dict[str, dict[str, StrategyResult]] | None = None,
    curves: dict[str, list[CurvePoint]] | None = None,
    optimal_strategies: dict[str, str] | None = None,
) -> str:
    """Format complete benchmark report with comparisons, matrices, and curves."""
    parts = [format_report(comparisons)]

    if matrices:
        parts.append("")
        parts.append("=== Strategy Matrix ===")
        parts.append("")
        for task_id, results in matrices.items():
            opt = optimal_strategies.get(task_id) if optimal_strategies else None
            parts.append(format_matrix(task_id, results, optimal=opt))
            parts.append("")

    if curves:
        parts.append("")
        parts.append("=== Compression Curves ===")
        parts.append("")
        for task_id, points in curves.items():
            parts.append(format_curve(task_id, points))
            parts.append("")

    return "\n".join(parts)


def format_stage_breakdown(breakdown: StageBreakdown) -> str:
    """Format per-stage quality breakdown."""
    lines = [f"Stage breakdown: {breakdown.task_id}"]
    lines.append(f"  {'Stage':<12} {'Chars':>8} {'Quality':>8} {'QA':>10}")
    lines.append(f"  {'-' * 12} {'-' * 8} {'-' * 8} {'-' * 10}")

    for s in breakdown.stages:
        qa_str = f"{s.qa_answerable}/{s.qa_total}" if s.qa_total else "n/a"
        lines.append(f"  {s.stage:<12} {s.chars:>8} {s.quality_score:>7.1f} {qa_str:>10}")

    # Deltas
    lines.append("  ---")
    lines.append(f"  Clean info loss:    {breakdown.clean_info_loss:+.1f}")
    lines.append(f"  Compress info loss: {breakdown.compress_info_loss:+.1f}")
    lines.append(f"  Surfacing value:    {breakdown.surfacing_value:+.1f}")
    if breakdown.surfacing_qa_gain:
        lines.append(f"  Surfacing QA gain:  +{breakdown.surfacing_qa_gain} answers")

    return "\n".join(lines)


def format_surfacing_value(values: list[SurfacingValue]) -> str:
    """Format surfacing value comparison."""
    lines = ["=== Surfacing Value ===", ""]
    lines.append(f"  {'Task':<20} {'Without':>8} {'With':>8} {'Delta':>8} {'QA +':>6}")
    lines.append(f"  {'-' * 20} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 6}")

    total_delta = 0.0
    total_qa_delta = 0
    for v in values:
        total_delta += v.quality_delta
        total_qa_delta += v.qa_delta
        lines.append(
            f"  {v.task_id:<20} {v.without_surfacing:>7.1f} {v.with_surfacing:>7.1f} "
            f"{v.quality_delta:>+7.1f} {v.qa_delta:>+5}"
        )

    if values:
        n = len(values)
        lines.append("  ---")
        lines.append(f"  Avg quality delta: {total_delta / n:+.1f}")
        lines.append(f"  Total QA gain:     +{total_qa_delta} answers")

    return "\n".join(lines)
