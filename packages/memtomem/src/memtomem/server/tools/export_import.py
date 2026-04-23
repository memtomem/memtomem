"""Tools: mem_export, mem_import."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _check_embedding_mismatch


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_export(
    output_file: str,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    since: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Export indexed memory chunks to a JSON bundle file.

    Args:
        output_file: Destination path for the JSON export (e.g. ~/backup.json).
        source_filter: Only export chunks whose source file path contains this substring.
        tag_filter: Only export chunks that carry this exact tag.
        since: ISO 8601 datetime lower bound on created_at (e.g. "2026-01-01T00:00:00Z").
        namespace: Only export chunks in this namespace.
    """
    from datetime import datetime, timezone

    from memtomem.tools.export_import import export_chunks

    app = await _get_app_initialized(ctx)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            return f"Invalid 'since' datetime: {exc}"

    target = Path(output_file).expanduser().resolve()
    bundle = await export_chunks(
        app.storage,
        output_path=target,
        source_filter=source_filter,
        tag_filter=tag_filter,
        since=since_dt,
        namespace_filter=namespace,
    )

    return f"Export complete:\n- Chunks exported: {bundle.total_chunks}\n- Output: {target}"


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_import(
    input_file: str,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Import memory chunks from a JSON bundle file (produced by mem_export).

    Each chunk is re-embedded with the current embedder and upserted to storage.
    Imported chunks receive new UUIDs to avoid collisions with existing entries.

    Args:
        input_file: Path to the JSON bundle file to import.
        namespace: Override the namespace for all imported chunks.
    """
    from memtomem.tools.export_import import import_chunks

    app = await _get_app_initialized(ctx)

    # Block import if embedding config mismatches DB
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    source = Path(input_file).expanduser().resolve()

    if not source.exists():
        return f"File not found: {source}"

    stats = await import_chunks(app.storage, app.embedder, source, namespace=namespace)

    return (
        f"Import complete:\n"
        f"- Total in bundle: {stats.total_chunks}\n"
        f"- Imported:        {stats.imported_chunks}\n"
        f"- Skipped:         {stats.skipped_chunks}\n"
        f"- Failed:          {stats.failed_chunks}"
    )
