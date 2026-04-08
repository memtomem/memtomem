"""Tool: mem_index."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch


@mcp.tool()
@tool_handler
async def mem_index(
    path: str = ".",
    recursive: bool = True,
    force: bool = False,
    namespace: str | None = None,
    auto_tag: bool = False,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Index or re-index markdown files for hybrid search.

    Args:
        path: File or directory path to index
        recursive: Whether to recurse into subdirectories (default True)
        force: If True, re-index all files even if unchanged (default False)
        namespace: Assign all indexed chunks to this namespace
        auto_tag: If True, run keyword-based auto-tagging on newly indexed chunks
    """
    app = _get_app(ctx)

    # Block indexing if embedding config mismatches DB
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    target = Path(path).resolve()
    effective_ns = namespace or app.current_namespace

    stats = await app.index_engine.index_path(
        target,
        recursive=recursive,
        force=force,
        namespace=effective_ns,
    )

    result = (
        f"Indexing complete:\n"
        f"- Files scanned: {stats.total_files}\n"
        f"- Total chunks: {stats.total_chunks}\n"
        f"- Indexed: {stats.indexed_chunks}\n"
        f"- Skipped (unchanged): {stats.skipped_chunks}\n"
        f"- Deleted (stale): {stats.deleted_chunks}\n"
        f"- Duration: {stats.duration_ms:.0f}ms"
    )

    if auto_tag and stats.indexed_chunks > 0:
        from memtomem.tools.auto_tag import auto_tag_storage

        tagged = await auto_tag_storage(
            app.storage,
            source_filter=str(target) if target.is_file() else None,
            max_tags=5,
        )
        result += f"\n- Auto-tagged: {tagged} chunks"

    return result
