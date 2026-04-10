"""Tools: mem_search, mem_expand."""

from __future__ import annotations

from uuid import UUID

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import _display_path, _format_results
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


def _webhook_error_cb(task: "asyncio.Task") -> None:  # noqa: F821
    """Log errors from fire-and-forget webhook tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("Webhook fire failed: %s", exc)


@mcp.tool()
@tool_handler
async def mem_search(
    query: str,
    top_k: int = 10,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    namespace: str | None = None,
    bm25_weight: float | None = None,
    dense_weight: float | None = None,
    context_window: int = 0,
    verbose: bool = False,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Search across indexed memory files using hybrid BM25 + semantic search.

    Args:
        query: Natural language search query
        top_k: Number of results to return (default 10)
        source_filter: Filter by source file path (substring match, or glob pattern with *, ?, [])
        tag_filter: Comma-separated tags — matches chunks containing ANY of the listed tags (OR logic)
        namespace: Namespace scope (single value)
        bm25_weight: Override BM25 weight in RRF fusion (default 1.0). Set higher to favor keyword matches.
        dense_weight: Override dense/semantic weight in RRF fusion (default 1.0). Set higher to favor meaning.
        context_window: Expand each result with ±N adjacent chunks (0=disabled). Use for more context.
        verbose: Show full details (UUID, full path, score 4dp, pipeline stats). Default: compact output.
    """
    if not query.strip():
        return "Error: query cannot be empty."
    if len(query) > 10_000:
        return "Error: query too long (max 10,000 characters)."
    if not 1 <= top_k <= 100:
        return "Error: top_k must be between 1 and 100."

    app = _get_app(ctx)
    effective_ns = namespace or app.current_namespace

    rrf_weights = None
    if bm25_weight is not None or dense_weight is not None:
        rrf_weights = [bm25_weight or 1.0, dense_weight or 1.0]

    results, stats = await app.search_pipeline.search(
        query=query,
        top_k=top_k,
        source_filter=source_filter,
        tag_filter=tag_filter,
        namespace=effective_ns,
        rrf_weights=rrf_weights,
        context_window=context_window if context_window > 0 else None,
    )

    if not results:
        if (source_filter or tag_filter) and stats.fused_total > 0:
            return (
                f"No results match your filters "
                f"({stats.fused_total} results found before filtering). "
                f"Try broader filters or remove source_filter/tag_filter."
            )
        if stats.bm25_error and stats.dense_error:
            return (
                "Search unavailable: both keyword and semantic search failed.\n"
                f"- BM25: {stats.bm25_error}\n"
                f"- Dense: {stats.dense_error}"
            )
        if stats.bm25_error:
            return f"No results found. (Note: keyword search unavailable: {stats.bm25_error})"
        if stats.dense_error:
            return f"No results found. (Note: semantic search unavailable: {stats.dense_error})"
        return "No results found."

    output = _format_results(results, verbose=verbose)

    if stats.bm25_error and not verbose:
        output += "\n\n(Note: keyword index unavailable — results from semantic search only)"

    if verbose:
        pipeline_info = []
        if stats.bm25_candidates:
            pipeline_info.append(f"BM25:{stats.bm25_candidates}")
        if stats.dense_candidates:
            pipeline_info.append(f"Dense:{stats.dense_candidates}")
        if stats.fused_total:
            pipeline_info.append(f"RRF:{stats.fused_total}")
        pipeline_info.append(f"Final:{stats.final_total}")
        if stats.bm25_error:
            pipeline_info.append(f"BM25-err:{stats.bm25_error}")
        if stats.dense_error:
            pipeline_info.append(f"Dense-err:{stats.dense_error}")
        output += f"\n\n---\npipeline: {' → '.join(pipeline_info)}"

    # Fire webhook
    if app.webhook_manager:
        import asyncio

        task = asyncio.create_task(
            app.webhook_manager.fire("search", {"query": query, "result_count": len(results)})
        )
        task.add_done_callback(_webhook_error_cb)

    return output


@mcp.tool()
@tool_handler
@register("search")
async def mem_expand(
    chunk_id: str,
    window: int = 2,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Expand a chunk with adjacent context from the same source file.

    Use this after mem_search when you need more surrounding context for
    a specific result. Returns ±N adjacent chunks ordered by line number.

    Args:
        chunk_id: The UUID of the chunk to expand (from mem_search results)
        window: Number of adjacent chunks before and after (default 2, max 10)
    """
    window = max(0, min(window, 10))
    app = _get_app(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Chunk {chunk_id} not found."

    source_file = chunk.metadata.source_file
    all_chunks = await app.storage.list_chunks_by_source(source_file, limit=10000)

    # Find position of this chunk
    idx_map = {str(c.id): i for i, c in enumerate(all_chunks)}
    pos = idx_map.get(chunk_id)
    if pos is None:
        return f"Chunk {chunk_id} not found in source file listing."

    before = all_chunks[max(0, pos - window) : pos]
    after = all_chunks[pos + 1 : pos + 1 + window]

    parts = [
        f"## Expand: chunk {pos + 1}/{len(all_chunks)} in {_display_path(source_file)}",
        f"Window: ±{window} chunks\n",
    ]

    if before:
        parts.append("### Before")
        for c in before:
            hierarchy = (
                " > ".join(c.metadata.heading_hierarchy) if c.metadata.heading_hierarchy else ""
            )
            header = f"**[{_display_path(c.metadata.source_file)} L{c.metadata.start_line}-{c.metadata.end_line}]**"
            if hierarchy:
                header += f" {hierarchy}"
            parts.append(f"{header}\n```\n{c.content}\n```")

    parts.append("### Matched")
    parts.append(f"```\n{chunk.content}\n```")

    if after:
        parts.append("### After")
        for c in after:
            hierarchy = (
                " > ".join(c.metadata.heading_hierarchy) if c.metadata.heading_hierarchy else ""
            )
            header = f"**[{_display_path(c.metadata.source_file)} L{c.metadata.start_line}-{c.metadata.end_line}]**"
            if hierarchy:
                header += f" {hierarchy}"
            parts.append(f"{header}\n```\n{c.content}\n```")

    return "\n\n".join(parts)


@register("search")
async def mem_increment_access(
    chunk_ids: list[str],
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Increment access_count for the given chunks (drives access-frequency boost in search ranking).

    Used by external surfacing systems (e.g. memtomem-stm) to record positive
    feedback as a future search-ranking boost. Each call increments the count
    by 1 per chunk; the search pipeline applies a logarithmic transform with
    ``max_boost`` capping (default 1.5×) so this never produces runaway scores.

    Idempotency / per-event capping is the caller's responsibility — this
    action just forwards the IDs to storage.

    Args:
        chunk_ids: List of chunk UUIDs (strings) to boost
    """
    app = _get_app(ctx)

    if not chunk_ids:
        return "No chunk_ids provided."

    valid: list[UUID] = []
    invalid: list[str] = []
    for cid in chunk_ids:
        try:
            valid.append(UUID(cid))
        except (ValueError, TypeError):
            invalid.append(str(cid))

    if not valid:
        return f"Error: no valid UUIDs in chunk_ids (rejected: {len(invalid)})."

    await app.storage.increment_access(valid)

    msg = f"Incremented access_count for {len(valid)} chunk(s)."
    if invalid:
        msg += f" Skipped {len(invalid)} invalid id(s)."
    return msg
