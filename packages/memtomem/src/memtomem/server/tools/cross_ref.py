"""Tools: mem_link, mem_unlink, mem_related."""

from __future__ import annotations

from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("relations")
async def mem_link(
    source_id: str,
    target_id: str,
    relation_type: str = "related",
    ctx: CtxType = None,
) -> str:
    """Create a bidirectional link between two chunks.

    Links are stored as cross-references and shown when viewing related chunks.
    Common relation types: "related", "supersedes", "depends_on", "contradicts".

    Args:
        source_id: UUID of the first chunk
        target_id: UUID of the second chunk
        relation_type: Type of relationship (default: "related")
    """
    if source_id == target_id:
        return "Error: cannot link a chunk to itself."

    app = _get_app(ctx)

    try:
        src_uid = UUID(source_id)
        tgt_uid = UUID(target_id)
    except (ValueError, TypeError) as exc:
        return f"Error: invalid UUID format: {exc}"

    # Verify both chunks exist
    src = await app.storage.get_chunk(src_uid)
    tgt = await app.storage.get_chunk(tgt_uid)
    if src is None:
        return f"Chunk {source_id} not found."
    if tgt is None:
        return f"Chunk {target_id} not found."

    await app.storage.add_relation(src_uid, tgt_uid, relation_type)

    src_preview = src.content[:60].replace("\n", " ")
    tgt_preview = tgt.content[:60].replace("\n", " ")
    return (
        f"Linked: {source_id[:8]}... ←({relation_type})→ {target_id[:8]}...\n"
        f"  Source: {src_preview}...\n"
        f"  Target: {tgt_preview}..."
    )


@mcp.tool()
@tool_handler
@register("relations")
async def mem_unlink(
    source_id: str,
    target_id: str,
    ctx: CtxType = None,
) -> str:
    """Remove a link between two chunks.

    Args:
        source_id: UUID of the first chunk
        target_id: UUID of the second chunk
    """
    app = _get_app(ctx)

    try:
        src_uid = UUID(source_id)
        tgt_uid = UUID(target_id)
    except (ValueError, TypeError) as exc:
        return f"Error: invalid UUID format: {exc}"

    removed = await app.storage.delete_relation(src_uid, tgt_uid)
    if removed:
        return f"Unlinked: {source_id[:8]}... ↔ {target_id[:8]}..."
    return "No link found between these chunks."


@mcp.tool()
@tool_handler
@register("relations")
async def mem_related(
    chunk_id: str,
    ctx: CtxType = None,
) -> str:
    """Find all chunks linked to the given chunk.

    Returns related chunks with their relationship type and content preview.

    Args:
        chunk_id: UUID of the chunk to find relations for
    """
    app = _get_app(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Chunk {chunk_id} not found."

    relations = await app.storage.get_related(uid)
    if not relations:
        return f"No related chunks for {chunk_id[:8]}..."

    lines = [f"Related to {chunk_id[:8]}... ({len(relations)} links):\n"]
    for related_id, rel_type in relations:
        related = await app.storage.get_chunk(related_id)
        if related is None:
            lines.append(f"  - [{rel_type}] {related_id} (deleted)")
            continue
        preview = related.content[:80].replace("\n", " ")
        source = str(related.metadata.source_file).split("/")[-1]
        lines.append(f"  - [{rel_type}] {str(related_id)[:8]}... ({source})")
        lines.append(f"    {preview}...")

    return "\n".join(lines)
