"""Tool: mem_search."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import _format_results


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
    """
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
    )

    if not results:
        return "No results found."

    output = _format_results(results)

    pipeline_info = []
    if stats.bm25_candidates or 0:
        pipeline_info.append(f"BM25:{stats.bm25_candidates}")
    if stats.dense_candidates or 0:
        pipeline_info.append(f"Dense:{stats.dense_candidates}")
    if stats.fused_total or 0:
        pipeline_info.append(f"RRF:{stats.fused_total}")
    pipeline_info.append(f"Final:{stats.final_total or 0}")
    if stats.bm25_error:
        pipeline_info.append(f"BM25-err:{stats.bm25_error}")
    output += f"\n\n---\npipeline: {' → '.join(pipeline_info)}"

    # Fire webhook
    if app.webhook_manager:
        import asyncio

        asyncio.create_task(
            app.webhook_manager.fire("search", {"query": query, "result_count": len(results)})
        )

    return output
