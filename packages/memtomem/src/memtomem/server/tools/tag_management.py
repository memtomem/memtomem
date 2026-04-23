"""Tools: mem_tag_list, mem_tag_rename, mem_tag_delete."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("tags")
async def mem_tag_list(
    ctx: CtxType = None,
) -> str:
    """List all tags and their usage counts, ordered by frequency.

    Use this to see which tags exist in the index and how many chunks use each tag.
    """
    app = await _get_app_initialized(ctx)
    tag_counts = await app.storage.get_tag_counts()

    if not tag_counts:
        return "No tags found."

    lines = [f"Tags: {len(tag_counts)}\n"]
    for tag, count in tag_counts:
        lines.append(f"  {tag}  — {count} chunks")

    total = sum(c for _, c in tag_counts)
    lines.append(f"\nTotal: {len(tag_counts)} tags across {total} chunk-tag assignments")
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("tags")
async def mem_tag_rename(
    old_tag: str,
    new_tag: str,
    ctx: CtxType = None,
) -> str:
    """Rename a tag across all chunks that use it.

    Args:
        old_tag: The current tag name to replace
        new_tag: The new tag name
    """
    if not old_tag.strip() or not new_tag.strip():
        return "Error: both old_tag and new_tag must be non-empty."
    if old_tag == new_tag:
        return "Error: old_tag and new_tag are the same."

    app = await _get_app_initialized(ctx)
    updated = await app.storage.rename_tag(old_tag.strip(), new_tag.strip())
    return f"Renamed tag '{old_tag}' → '{new_tag}' in {updated} chunks."


@mcp.tool()
@tool_handler
@register("tags")
async def mem_tag_delete(
    tag: str,
    ctx: CtxType = None,
) -> str:
    """Remove a tag from all chunks that use it.

    The chunks themselves are not deleted — only the tag is removed.

    Args:
        tag: The tag name to remove
    """
    if not tag.strip():
        return "Error: tag must be non-empty."

    app = await _get_app_initialized(ctx)
    updated = await app.storage.delete_tag(tag.strip())
    return f"Removed tag '{tag}' from {updated} chunks."
