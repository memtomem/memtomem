"""Search history and auto-suggest MCP tools."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_search_history(
    limit: int = 20,
    since: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """List past search queries with result counts.

    Args:
        limit: Maximum number of queries to return (default 20).
        since: ISO date filter — only queries after this date.
    """
    app = _get_app(ctx)
    rows = await app.storage.get_query_history(limit=limit, since=since)
    if not rows:
        return "No search history found."
    lines = [f"Search History ({len(rows)} queries):"]
    for r in rows:
        result_count = len(r.get("result_chunk_ids", []))
        lines.append(f'  [{r["created_at"]}] "{r["query_text"]}" -> {result_count} results')
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_search_suggest(
    prefix: str,
    limit: int = 5,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Autocomplete search queries from history.

    Args:
        prefix: The query prefix to match.
        limit: Maximum suggestions (default 5).
    """
    app = _get_app(ctx)
    suggestions = await app.storage.suggest_queries(prefix=prefix, limit=limit)
    if not suggestions:
        return f'No suggestions for "{prefix}".'
    lines = [f'Suggestions for "{prefix}":']
    for s in suggestions:
        lines.append(f"  - {s}")
    return "\n".join(lines)
