"""Tools: mem_timeline, mem_activity."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _parse_recall_date

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
    ctx: CtxType = None,  # type: ignore[assignment]
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
        return "Error: limit must be between 1 and 500."

    from memtomem.tools.temporal import build_timeline, format_timeline

    app = _get_app(ctx)

    # Search for topic
    results, _stats = await app.search_pipeline.search(
        query=topic,
        top_k=limit,
        namespace=namespace,
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
        if until_dt and dt > until_dt:
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
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show memory activity summary by day.

    Displays how many memories were created, updated, and accessed
    per day within the given time range.

    Args:
        since: Start date (YYYY, YYYY-MM, YYYY-MM-DD, default: 30 days ago)
        until: End date (default: now)
        namespace: Namespace scope
    """
    from memtomem.tools.temporal import ActivityDay, format_activity

    app = _get_app(ctx)

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
            until_str = until_dt.strftime("%Y-%m-%d")
        else:
            until_dt = now
            until_str = until_dt.strftime("%Y-%m-%d")
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
