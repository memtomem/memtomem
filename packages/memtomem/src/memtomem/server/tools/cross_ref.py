"""Tools: mem_link, mem_unlink, mem_related."""

from __future__ import annotations

from uuid import UUID

from memtomem.models import Chunk
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools._id_access import (
    caller_boundary,
    in_boundary,
    not_found,
    resolve_chunk,
)


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

    Both ends must be inside the caller's project boundary (ADR-0011); an id
    from another project reads as not-found (ADR-0036).

    Args:
        source_id: UUID of the first chunk
        target_id: UUID of the second chunk
        relation_type: Type of relationship (default: "related")
    """
    if source_id == target_id:
        return "Error: cannot link a chunk to itself."

    app = await _get_app_initialized(ctx)

    try:
        src_uid = UUID(source_id)
        tgt_uid = UUID(target_id)
    except (ValueError, TypeError) as exc:
        return f"Error: invalid UUID format: {exc}"

    # Verify both chunks exist and are the caller's to link.
    src = await resolve_chunk(app, src_uid)
    tgt = await resolve_chunk(app, tgt_uid)
    if src is None:
        return not_found(source_id)
    if tgt is None:
        return not_found(target_id)

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

    Both ends must be inside the caller's project boundary (ADR-0011); an id
    from another project reads as not-found (ADR-0036).

    Args:
        source_id: UUID of the first chunk
        target_id: UUID of the second chunk
    """
    app = await _get_app_initialized(ctx)

    try:
        src_uid = UUID(source_id)
        tgt_uid = UUID(target_id)
    except (ValueError, TypeError) as exc:
        return f"Error: invalid UUID format: {exc}"

    # Screened before the delete, not after: unlinking is a write, and a
    # caller who may not read a chunk may not sever its relations either.
    # ``mem_link`` verified both ends already, so requiring it here costs a
    # legitimate unlink nothing it did not already pay.
    if await resolve_chunk(app, src_uid) is None:
        return not_found(source_id)
    if await resolve_chunk(app, tgt_uid) is None:
        return not_found(target_id)

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
    app = await _get_app_initialized(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await resolve_chunk(app, uid)
    if chunk is None:
        return not_found(chunk_id)

    relations = await app.storage.get_related(uid)

    # Out-of-boundary relations leave the listing *and* the count. Rendering
    # one as the dangling-id line below would say more than the boundary
    # allows twice over: ``chunk_relations`` is ON DELETE CASCADE, so a
    # dangling id means "deleted" — reusing it for a live row would report
    # the row as gone while printing its full uuid. A count that included it
    # would leak the same existence more quietly.
    boundary = caller_boundary(app)
    visible: list[tuple[str, str, Chunk | None]] = []
    for related_id, rel_type in relations:
        related = await app.storage.get_chunk(related_id)
        if related is not None and not in_boundary(related, boundary):
            continue
        visible.append((str(related_id), rel_type, related))

    if not visible:
        return f"No related chunks for {chunk_id[:8]}..."

    lines = [f"Related to {chunk_id[:8]}... ({len(visible)} links):\n"]
    for related_id_text, rel_type, related in visible:
        if related is None:
            lines.append(f"  - [{rel_type}] {related_id_text} (deleted)")
            continue
        preview = related.content[:80].replace("\n", " ")
        source = str(related.metadata.source_file).split("/")[-1]
        lines.append(f"  - [{rel_type}] {related_id_text[:8]}... ({source})")
        lines.append(f"    {preview}...")

    return "\n".join(lines)
