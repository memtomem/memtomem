"""Temporal analysis — timeline and activity summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TimelineBucket:
    period_label: str
    period_start: str
    period_end: str
    chunk_count: int
    sources: list[str]
    key_topics: list[str]
    sample_content: str


@dataclass(frozen=True)
class ActivityDay:
    date: str
    created: int
    updated: int
    accessed: int


def build_timeline(
    chunks: list[dict],
    granularity: str = "auto",
) -> list[TimelineBucket]:
    """Group chunks by time period and summarize each bucket.

    Args:
        chunks: list of dicts with 'content', 'created_at', 'source_file', 'tags', 'score'
        granularity: 'week', 'month', or 'auto' (auto picks based on span)
    """
    if not chunks:
        return []

    # Parse dates and sort
    dated = []
    for c in chunks:
        try:
            dt = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        dated.append((dt, c))

    if not dated:
        return []

    dated.sort(key=lambda x: x[0])

    # Determine granularity
    if granularity == "auto":
        span_days = (dated[-1][0] - dated[0][0]).days
        granularity = "month" if span_days > 90 else "week"

    # Group into buckets
    buckets: dict[str, list[tuple[datetime, dict]]] = {}
    for dt, c in dated:
        if granularity == "month":
            key = dt.strftime("%Y-%m")
        else:
            # ISO week
            key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
        buckets.setdefault(key, []).append((dt, c))

    # Build timeline buckets
    result: list[TimelineBucket] = []
    for label, items in sorted(buckets.items()):
        sources = list({c.get("source_file", "unknown") for _, c in items})
        tags: list[str] = []
        for _, c in items:
            chunk_tags = c.get("tags", [])
            if isinstance(chunk_tags, str):
                import json

                try:
                    chunk_tags = json.loads(chunk_tags)
                except (ValueError, TypeError):
                    chunk_tags = []
            tags.extend(chunk_tags)
        key_topics = list(dict.fromkeys(tags))[:5]  # unique, max 5

        first_dt = items[0][0]
        last_dt = items[-1][0]
        sample = items[0][1].get("content", "")[:200]

        result.append(
            TimelineBucket(
                period_label=label,
                period_start=first_dt.strftime("%Y-%m-%d"),
                period_end=last_dt.strftime("%Y-%m-%d"),
                chunk_count=len(items),
                sources=[str(s).split("/")[-1] for s in sources[:3]],
                key_topics=key_topics,
                sample_content=sample,
            )
        )

    return result


def format_timeline(topic: str, buckets: list[TimelineBucket]) -> str:
    """Format timeline buckets as readable text."""
    if not buckets:
        return f"No memories found for topic '{topic}'."

    date_range = f"{buckets[0].period_start} -> {buckets[-1].period_end}"
    lines = [f'Timeline for "{topic}" ({date_range}):', ""]

    for b in buckets:
        sources_str = ", ".join(b.sources) if b.sources else ""
        lines.append(f"## {b.period_label} ({b.chunk_count} memories)")
        if sources_str:
            lines.append(f"Sources: {sources_str}")
        if b.key_topics:
            lines.append(f"Topics: {', '.join(b.key_topics)}")
        preview = b.sample_content.replace("\n", " ").strip()
        lines.append(f"  - [{b.period_start}] {preview}...")
        lines.append("")

    total = sum(b.chunk_count for b in buckets)
    lines.append(f"Total: {total} memories across {len(buckets)} periods")
    return "\n".join(lines)


def format_activity(days: list[ActivityDay], since: str, until: str) -> str:
    """Format activity summary as text table."""
    if not days:
        return f"No activity found between {since} and {until}."

    lines = [
        f"Memory Activity ({since} -> {until}):",
        "",
        "Date       | Created | Updated | Accessed",
        "-----------|---------|---------|--------",
    ]

    total_c, total_u, total_a = 0, 0, 0
    for d in days:
        lines.append(f"{d.date} | {d.created:>7} | {d.updated:>7} | {d.accessed:>7}")
        total_c += d.created
        total_u += d.updated
        total_a += d.accessed

    lines.append("")
    lines.append(f"Totals: {total_c} created, {total_u} updated, {total_a} accessed")
    return "\n".join(lines)
