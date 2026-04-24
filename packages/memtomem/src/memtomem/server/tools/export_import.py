"""Tools: mem_export, mem_import."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.server.tool_registry import register


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
    on_conflict: str = "skip",
    preserve_ids: bool = False,
    ctx: CtxType = None,
) -> str:
    """Import memory chunks from a JSON bundle file (produced by mem_export).

    Each chunk is re-embedded with the current embedder and upserted to storage.

    Args:
        input_file: Path to the JSON bundle file to import.
        namespace: Override the namespace for all imported chunks.
        on_conflict: How to resolve content-hash collisions against the
            existing DB. ``"skip"`` (default) drops records whose content
            already exists (idempotent re-import). ``"update"`` overwrites
            the existing row's metadata while preserving its UUID.
            ``"duplicate"`` is the pre-v2 behaviour: every record gets a
            fresh UUID, so re-imports and overlapping merges produce
            duplicate rows.
        preserve_ids: For non-conflicting records in a v2 bundle, reuse the
            bundle's original chunk UUID (skipped if already claimed by
            unrelated content). Ignored when ``on_conflict="duplicate"``.
    """
    from memtomem.tools.export_import import _VALID_ON_CONFLICT, import_chunks

    app = await _get_app_initialized(ctx)

    if on_conflict not in _VALID_ON_CONFLICT:
        return f"Invalid on_conflict={on_conflict!r}. Must be one of {sorted(_VALID_ON_CONFLICT)}."

    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    source = Path(input_file).expanduser().resolve()

    if not source.exists():
        return f"File not found: {source}"

    stats = await import_chunks(
        app.storage,
        app.embedder,
        source,
        namespace=namespace,
        on_conflict=on_conflict,  # type: ignore[arg-type]
        preserve_ids=preserve_ids,
    )

    return (
        f"Import complete ({on_conflict=}, {preserve_ids=}):\n"
        f"- Total in bundle:  {stats.total_chunks}\n"
        f"- Imported (new):   {stats.imported_chunks}\n"
        f"- Updated:          {stats.updated_chunks}\n"
        f"- Conflict skipped: {stats.conflict_skipped_chunks}\n"
        f"- Malformed:        {stats.skipped_chunks}\n"
        f"- Failed:           {stats.failed_chunks}"
    )
