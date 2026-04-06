"""Benchmark report formatter."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import ComparisonReport


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
