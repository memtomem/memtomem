"""Tools: mem_tag_list, mem_tag_rename, mem_tag_delete, mem_tag_merge.

All mutating tools route through ``services.tag_management`` so MCP and
the Web ``/api/tags/{...}`` routes stay symmetric: same lock, same
``updated_at`` policy, same search-cache invalidation. See #688.
"""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.services import tag_management as tag_svc

logger = logging.getLogger(__name__)


def _format_samples(result: tag_svc.TagOpResult) -> str:
    if not result.samples:
        return ""
    lines = ["", "Sample affected chunks:"]
    for s in result.samples:
        lines.append(f"  {s.chunk_id} ({s.source_file})")
        lines.append(f"    {s.content_preview}")
    return "\n".join(lines)


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
    dry_run: bool = False,
    ctx: CtxType = None,
) -> str:
    """Rename a tag across all chunks that use it.

    Args:
        old_tag: The current tag name to replace
        new_tag: The new tag name
        dry_run: If True, return the count + a sample of affected chunks
            without writing. Defaults to False (apply).
    """
    if not old_tag.strip() or not new_tag.strip():
        return "Error: both old_tag and new_tag must be non-empty."
    if old_tag == new_tag:
        return "Error: old_tag and new_tag are the same."

    app = await _get_app_initialized(ctx)
    result = await tag_svc.rename_tag(
        app.storage,
        old_tag.strip(),
        new_tag.strip(),
        dry_run=dry_run,
        search_pipeline=app.search_pipeline,
    )
    if result.dry_run:
        head = (
            f"DRY RUN: rename '{old_tag}' → '{new_tag}' would affect "
            f"{result.affected_chunks} chunks."
        )
        return head + _format_samples(result)
    return f"Renamed tag '{old_tag}' → '{new_tag}' in {result.affected_chunks} chunks."


@mcp.tool()
@tool_handler
@register("tags")
async def mem_tag_delete(
    tag: str,
    dry_run: bool = False,
    ctx: CtxType = None,
) -> str:
    """Remove a tag from all chunks that use it.

    The chunks themselves are not deleted — only the tag is removed.

    Args:
        tag: The tag name to remove
        dry_run: If True, return the count + a sample of affected chunks
            without writing. Defaults to False (apply).
    """
    if not tag.strip():
        return "Error: tag must be non-empty."

    app = await _get_app_initialized(ctx)
    result = await tag_svc.delete_tag(
        app.storage,
        tag.strip(),
        dry_run=dry_run,
        search_pipeline=app.search_pipeline,
    )
    if result.dry_run:
        head = f"DRY RUN: delete '{tag}' would affect {result.affected_chunks} chunks."
        return head + _format_samples(result)
    return f"Removed tag '{tag}' from {result.affected_chunks} chunks."


@mcp.tool()
@tool_handler
@register("tags")
async def mem_tag_merge(
    sources: list[str],
    target: str,
    dry_run: bool = False,
    ctx: CtxType = None,
) -> str:
    """Fold multiple source tags into a single target tag across all chunks.

    Each chunk that carries any source tag gets the source replaced with
    ``target``; the resulting per-chunk tag list is deduplicated. Chunks
    that already only carry ``target`` are not affected.

    Args:
        sources: List of source tags to fold into ``target``.
        target: The tag every source should be rewritten to.
        dry_run: If True, return the candidate count + a sample without
            writing. Defaults to False (apply).
    """
    if not target.strip():
        return "Error: target must be non-empty."
    cleaned_sources = [s.strip() for s in sources if s and s.strip()]
    if not cleaned_sources:
        return "Error: sources must contain at least one non-empty tag."

    app = await _get_app_initialized(ctx)
    result = await tag_svc.merge_tags(
        app.storage,
        cleaned_sources,
        target.strip(),
        dry_run=dry_run,
        search_pipeline=app.search_pipeline,
    )
    if result.dry_run:
        head = (
            f"DRY RUN: merge {cleaned_sources} → '{target}' would affect "
            f"{result.affected_chunks} chunks."
        )
        return head + _format_samples(result)
    return f"Merged {cleaned_sources} → '{target}' across {result.affected_chunks} chunks."
