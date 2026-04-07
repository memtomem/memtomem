"""Tools: mem_dedup_scan, mem_dedup_merge, mem_decay_scan, mem_decay_expire, mem_cleanup_orphans."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_dedup_scan(
    threshold: float = 0.92,
    limit: int = 50,
    max_scan: int = 500,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Scan for duplicate chunk candidates (dry-run, no mutations).

    Args:
        threshold: Cosine similarity threshold (0-1, default 0.92)
        limit: Maximum number of candidate pairs to return
        max_scan: Maximum chunks to inspect for near-duplicate search
    """
    app = _get_app(ctx)
    if app.dedup_scanner is None:
        return "DedupScanner not initialized."
    candidates = await app.dedup_scanner.scan(threshold=threshold, limit=limit, max_scan=max_scan)

    if not candidates:
        return f"No duplicate chunks found (threshold={threshold})."

    parts: list[str] = [f"Duplicate candidates: {len(candidates)} pairs (threshold={threshold}):\n"]
    for i, c in enumerate(candidates, 1):
        kind = "exact duplicate" if c.exact else f"score={c.score:.4f}"
        meta_a = c.chunk_a.metadata
        meta_b = c.chunk_b.metadata
        preview_a = c.chunk_a.content[:80].replace("\n", " ")
        preview_b = c.chunk_b.content[:80].replace("\n", " ")
        parts.append(
            f"[{i}] {kind}\n"
            f'  A ({c.chunk_a.id}): {meta_a.source_file}:{meta_a.start_line} — "{preview_a}"\n'
            f'  B ({c.chunk_b.id}): {meta_b.source_file}:{meta_b.start_line} — "{preview_b}"'
        )

    return "\n".join(parts)


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_dedup_merge(
    keep_id: str,
    delete_ids: list[str],
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Merge duplicate chunks: keep keep_id, delete delete_ids.

    Tags from deleted chunks are merged into the kept chunk.

    Args:
        keep_id: UUID of the chunk to keep
        delete_ids: UUIDs of chunks to delete
    """
    app = _get_app(ctx)
    if app.dedup_scanner is None:
        return "DedupScanner not initialized."
    try:
        keep_uuid = UUID(keep_id)
        delete_uuids = [UUID(d) for d in delete_ids]
    except ValueError as exc:
        return f"Invalid UUID: {exc}"

    deleted = await app.dedup_scanner.merge(keep_uuid, delete_uuids)
    return f"Merge complete: {deleted} chunks deleted, keep_id={keep_id}"


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_decay_scan(
    max_age_days: float = 90,
    source_filter: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Preview chunks that would be expired by TTL (dry-run, no deletions).

    Args:
        max_age_days: Chunks older than this many days are listed as candidates.
        source_filter: Only scan chunks from sources containing this substring.
    """
    from memtomem.search.decay import expire_chunks

    app = _get_app(ctx)
    stats = await expire_chunks(
        app.storage, max_age_days=max_age_days, dry_run=True, source_filter=source_filter
    )
    return (
        f"Expiry scan (max_age_days={max_age_days}, dry-run):\n"
        f"- Total chunks:   {stats.total_chunks}\n"
        f"- Expired:        {stats.expired_chunks}\n"
        f"(no deletions -- use mem_decay_expire to delete)"
    )


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_decay_expire(
    max_age_days: float = 90,
    source_filter: str | None = None,
    dry_run: bool = True,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Delete chunks older than max_age_days from the index.

    Defaults to dry_run=True for safety -- set dry_run=False to actually delete.

    Args:
        max_age_days: Chunks older than this many days are expired.
        source_filter: Only expire chunks from sources containing this substring.
        dry_run: If True (default), preview without making any changes.
    """
    from memtomem.search.decay import expire_chunks

    app = _get_app(ctx)
    stats = await expire_chunks(
        app.storage, max_age_days=max_age_days, dry_run=dry_run, source_filter=source_filter
    )
    mode = " (dry-run)" if dry_run else ""
    return (
        f"Memory expiry{mode}:\n"
        f"- Total chunks:   {stats.total_chunks}\n"
        f"- Expired:        {stats.expired_chunks}\n"
        f"- Deleted:        {stats.deleted_chunks}"
    )


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_cleanup_orphans(
    dry_run: bool = True,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Find and remove orphaned chunks whose source files no longer exist.

    Defaults to dry_run=True for safety -- set dry_run=False to actually delete.

    Args:
        dry_run: If True (default), only list orphaned files without deleting.
    """
    app = _get_app(ctx)
    source_files = await app.storage.get_all_source_files()

    orphaned: list[Path] = []
    for sf in source_files:
        if not sf.exists():
            orphaned.append(sf)

    if not orphaned:
        return f"No orphaned chunks found ({len(source_files)} source files checked)."

    if dry_run:
        lines = [f"Orphaned files: {len(orphaned)} (dry-run, no deletions)\n"]
        for sf in sorted(orphaned):
            lines.append(f"  {sf}")
        lines.append("\nSet dry_run=False to delete these chunks.")
        return "\n".join(lines)

    total_deleted = 0
    for sf in orphaned:
        deleted = await app.storage.delete_by_source(sf)
        total_deleted += deleted

    return (
        f"Cleanup complete:\n- Orphaned files: {len(orphaned)}\n- Chunks deleted: {total_deleted}"
    )
