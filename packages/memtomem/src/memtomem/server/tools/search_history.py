"""Search history and auto-suggest MCP tools."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_search_history(
    limit: int = 20,
    since: str | None = None,
    ctx: CtxType = None,
) -> str:
    """List past search queries with result counts.

    Args:
        limit: Maximum number of queries to return (default 20).
        since: ISO date filter — only queries after this date.
    """
    if not 1 <= limit <= 200:
        return f"Error: limit must be between 1 and 200, got {limit}."

    app = await _get_app_initialized(ctx)
    rows = await app.storage.get_query_history(limit=limit, since=since)
    if not rows:
        return "No search history found."
    lines = [f"Search History ({len(rows)} queries):"]
    for r in rows:
        result_count = len(r.get("result_chunk_ids", []))
        run_suffix = f" run={r['run_id']}" if r.get("run_id") else ""
        lines.append(
            f'  [{r["created_at"]}]{run_suffix} "{r["query_text"]}" -> {result_count} results'
        )
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_search_feedback(
    run_id: str,
    chunk_id: str | None = None,
    judgment: str | None = None,
    replace: bool = False,
    ctx: CtxType = None,
) -> str:
    """Record or list relevance judgments for one observed search run.

    With ``judgment`` (requires ``chunk_id``): record "relevant" or
    "not_relevant" for that snapshotted result. Resubmitting the same
    judgment is a no-op; a different judgment needs ``replace=True``.
    Without ``judgment``: list the run's current judgments.

    Args:
        run_id: The query_run_id returned by a ranked search.
        chunk_id: Chunk from that run's result snapshot to judge.
        judgment: One of "relevant" / "not_relevant"; omit to read.
        replace: Allow overwriting a different existing judgment.
    """
    if judgment is None:
        if chunk_id is not None:
            return "Error: judgment is required when chunk_id is given."
        if replace:
            return "Error: replace is only valid when judgment is given."
        app = await _get_app_initialized(ctx)
        judgments = await app.storage.get_search_feedback(run_id)
        if not judgments:
            return f"No feedback recorded for run {run_id}."
        lines = [f"Feedback for run {run_id} ({len(judgments)} judgments):"]
        for j in judgments:
            lines.append(f"  {j['chunk_id']}: {j['judgment']} (updated {j['updated_at']})")
        return "\n".join(lines)

    if chunk_id is None:
        return "Error: chunk_id is required when judgment is given."
    app = await _get_app_initialized(ctx)
    saved = await app.storage.save_search_feedback(run_id, chunk_id, judgment, replace=replace)
    if saved["created"]:
        return (
            f"Feedback recorded: run={run_id} chunk={chunk_id} "
            f"judgment={saved['judgment']} (created {saved['created_at']})"
        )
    if saved["replaced"]:
        return (
            f"Feedback replaced: run={run_id} chunk={chunk_id} -> "
            f"{saved['judgment']} (updated {saved['updated_at']})"
        )
    return (
        f"Feedback unchanged: run={run_id} chunk={chunk_id} already "
        f"{saved['judgment']} (created {saved['created_at']})"
    )


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_search_suggest(
    prefix: str,
    limit: int = 5,
    ctx: CtxType = None,
) -> str:
    """Autocomplete search queries from history.

    Args:
        prefix: The query prefix to match.
        limit: Maximum suggestions (default 5).
    """
    if not prefix.strip():
        return "Error: prefix cannot be empty."

    app = await _get_app_initialized(ctx)
    suggestions = await app.storage.suggest_queries(prefix=prefix, limit=limit)
    if not suggestions:
        return f'No suggestions for "{prefix}".'
    lines = [f'Suggestions for "{prefix}":']
    for s in suggestions:
        lines.append(f"  - {s}")
    return "\n".join(lines)
