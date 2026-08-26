"""Tools: mem_timeline, mem_activity."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _names_a_whole_day, _parse_recall_date

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_timeline(
    topic: str,
    since: str | None = None,
    until: str | None = None,
    namespace: str | None = None,
    limit: int = 50,
    ctx: CtxType = None,
) -> str:
    """Show how memories about a topic evolved over time.

    Groups matching memories into time periods (weeks or months) and shows
    the progression of knowledge on that topic.

    Args:
        topic: Subject to track through time
        since: Start date (YYYY, YYYY-MM, YYYY-MM-DD)
        until: End date
        namespace: Namespace scope
        limit: Maximum chunks to analyze (default 50)
    """
    if not 1 <= limit <= 500:
        return f"Error: limit must be between 1 and 500, got {limit}."

    from memtomem.tools.temporal import build_timeline, format_timeline

    app = await _get_app_initialized(ctx)

    # ADR-0011 PR-D round 9: thread project context so the always-on
    # storage scope filter sees the same boundary the primary mem_search
    # uses. Without this, mem_temporal_search inside a registered
    # project would silently drop project_shared / project_local rows.
    from memtomem.server.tools.search import _resolve_project_context_root

    project_context_root = _resolve_project_context_root(app)

    # Search for topic
    results, _stats = await app.search_pipeline.search(
        query=topic,
        top_k=limit,
        namespace=namespace,
        project_context_root=project_context_root,
    )

    if not results:
        return f"No memories found for topic '{topic}'."

    # Parse date filters
    try:
        since_dt = _parse_recall_date(since) if since else None
        until_dt = _parse_recall_date(until, end_of_period=True) if until else None
    except ValueError as exc:
        return f"Error: {exc}"

    if since_dt and until_dt and since_dt >= until_dt:
        return "Error: 'since' must be earlier than 'until'."

    # Convert search results to dicts for timeline builder
    chunks = []
    for r in results:
        chunk = r.chunk
        dt = chunk.created_at
        if since_dt and dt < since_dt:
            continue
        # ``>=``, not ``>``: ``until_dt`` is the start of the period *after*
        # the requested one, so a chunk landing exactly on it belongs to the
        # next period.
        if until_dt and dt >= until_dt:
            continue

        tags = list(chunk.metadata.tags) if chunk.metadata.tags else []

        chunks.append(
            {
                "content": chunk.content,
                "created_at": dt.isoformat(),
                "source_file": str(chunk.metadata.source_file),
                "tags": tags,
                "score": r.score,
            }
        )

    buckets = build_timeline(chunks)
    return format_timeline(topic, buckets)


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_activity(
    since: str | None = None,
    until: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Show memory activity summary by day.

    Displays how many memories were created, updated, and accessed
    per day within the given time range.

    Args:
        since: Start date (YYYY, YYYY-MM, YYYY-MM-DD, default: 30 days ago)
        until: End date, inclusive — the last day counted (default: now).
            Counts are per day, so a time of day is refused rather than
            rounded up to the whole day.
        namespace: Namespace scope
    """
    from memtomem.tools.temporal import ActivityDay, format_activity

    app = await _get_app_initialized(ctx)

    # This summary groups by ``DATE(created_at)``, so it can only express whole
    # days. An intraday bound would have to be rounded to one, which counts the
    # rest of that day — the caller asked for less than they get, silently. The
    # documented shapes are all whole periods, so refuse the rest rather than
    # round. (The implicit ``now`` default is the one instant that does round,
    # and it rounds to "through today", which is what "default: now" means.)
    for name, raw in (("since", since), ("until", until)):
        if raw and not _names_a_whole_day(raw):
            return (
                f"Error: {name}={raw!r} names a time of day, but this summary counts "
                "whole days. Use YYYY, YYYY-MM, or YYYY-MM-DD."
            )

    # Default: last 30 days
    now = datetime.now(timezone.utc)
    try:
        if since:
            since_dt = _parse_recall_date(since)
            since_str = since_dt.strftime("%Y-%m-%d")
        else:
            since_dt = now - timedelta(days=30)
            since_str = since_dt.strftime("%Y-%m-%d")

        if until:
            until_dt = _parse_recall_date(until, end_of_period=True)
        else:
            until_dt = now
        # ``until_dt`` is exclusive — ``_parse_recall_date`` advances a whole
        # period to the start of the next one — but ``get_activity_summary``
        # compares ``DATE(created_at) <= ?``, which is inclusive. Passing the
        # exclusive bound straight through therefore counted one day too many:
        # ``until="2026-04"`` reached 2026-05-01. Step back to the last instant
        # the bound admits and take its date, which is also the right label for
        # the rendered range. Every value reaching here names a whole period
        # (guarded above), so the step back never underflows the minimum
        # representable date.
        until_str = (until_dt - timedelta(microseconds=1)).strftime("%Y-%m-%d")
    except ValueError as exc:
        return f"Error: {exc}"

    if since_dt >= until_dt:
        return "Error: 'since' must be earlier than 'until'."

    # Get activity from storage
    summary = await app.storage.get_activity_summary(
        since=since_str,
        until=until_str,
        namespace=namespace,
    )

    days = [
        ActivityDay(
            date=d["date"],
            created=d.get("created", 0),
            updated=d.get("updated", 0),
            accessed=d.get("accessed", 0),
        )
        for d in summary
    ]

    return format_activity(days, since_str, until_str)
