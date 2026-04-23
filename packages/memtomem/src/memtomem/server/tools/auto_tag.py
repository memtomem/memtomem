"""Tool: mem_auto_tag."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("tags")
async def mem_auto_tag(
    source_filter: str | None = None,
    max_tags: int = 5,
    overwrite: bool = False,
    dry_run: bool = False,
    ctx: CtxType = None,
) -> str:
    """Extract and apply keyword-based tags to indexed memory chunks.

    Tags are derived from word frequency analysis of chunk content.
    Chunks that already have tags are skipped unless overwrite=True.

    Args:
        source_filter: Only process chunks from sources whose path contains this substring.
        max_tags: Maximum number of tags to assign per chunk (default 5).
        overwrite: If True, replace existing tags with newly extracted ones.
        dry_run: If True, report what would be tagged without making changes.
    """
    from memtomem.tools.auto_tag import auto_tag_storage

    app = await _get_app_initialized(ctx)
    stats = await auto_tag_storage(
        app.storage,
        source_filter=source_filter,
        max_tags=max_tags,
        overwrite=overwrite,
        dry_run=dry_run,
        llm_provider=app.llm_provider,
    )

    mode = " (dry-run)" if dry_run else ""
    return (
        f"Auto-tag complete{mode}:\n"
        f"- Total chunks:  {stats.total_chunks}\n"
        f"- Tagged:        {stats.tagged_chunks}\n"
        f"- Skipped:       {stats.skipped_chunks}"
    )
