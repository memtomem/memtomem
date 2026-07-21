"""Tools: mem_export, mem_import."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import (
    capture_session_for_untracked_write,
    flag_untracked_write,
)


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

    Path policy (ADR-0006 Axis G): ``output_file`` is an unrestricted resolved
    path by design — local-tool authority, not a traversal bug. This is
    intentionally asymmetric with the root-bounded read surfaces
    (``mem_add(file=)`` / ``mem_index``): backups are written *outside* the
    indexed tree on purpose, so ``memory_dirs`` is the wrong allowlist for an
    export target. Do not constrain it to memory/export roots without
    revisiting that ADR — it would break the documented ``~/backup.json``
    workflow.
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

    # Resolved, intentionally unrestricted (ADR-0006 Axis G): local-tool
    # authority, not a traversal bug — asymmetric with root-bounded mem_index.
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
    force_unsafe: bool = False,
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
        force_unsafe: Bypass the redaction gate when importing a *foreign*
            bundle (one not exported by this install) whose records contain
            secret-shaped values. Self-exports round-trip without this. The
            bypass is audit-logged (ADR-0006 Axis F.3).

    Path policy (ADR-0006 Axis F/G): ``input_file`` is an unrestricted resolved
    read by design. The import trust boundary is the provenance-aware redaction
    gate (Axis F.3), which is *path-independent* — not the filesystem path. The
    read-side asymmetry with the root-bounded ``mem_index`` is intentional; see
    ADR-0006 Axis G before adding a path allowlist.
    """
    from memtomem.tools.export_import import (
        ImportPrivacyError,
        _VALID_ON_CONFLICT,
        import_chunks,
    )

    app = await _get_app_initialized(ctx)

    if on_conflict not in _VALID_ON_CONFLICT:
        return f"Invalid on_conflict={on_conflict!r}. Must be one of {sorted(_VALID_ON_CONFLICT)}."

    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    # A bundle import is an ingest, not session work: its chunks are
    # someone else's notes and summarizing them would describe them as
    # this session's output. It does change the session's chunk set, so
    # the session stops claiming its provenance is the whole story.
    # Captured before the import's awaits so a session that ends midway
    # through still gets the flag.
    provenance_session_id = await capture_session_for_untracked_write(app)

    # Resolved, intentionally unrestricted (ADR-0006 Axis F/G): the import trust
    # boundary is the F.3 redaction gate below, not this path.
    source = Path(input_file).expanduser().resolve()

    if not source.exists():
        return f"File not found: {source}"

    try:
        stats = await import_chunks(
            app.storage,
            app.embedder,
            source,
            namespace=namespace,
            on_conflict=on_conflict,  # type: ignore[arg-type]
            preserve_ids=preserve_ids,
            force_unsafe=force_unsafe,
            surface="mem_import",
        )
    except ImportPrivacyError as exc:
        return (
            f"Error: {exc.blocked_records} bundle record(s) match privacy "
            "pattern(s); import rejected. Retry with force_unsafe=True to "
            "bypass (audit-logged)."
        )

    if stats.imported_chunks or stats.updated_chunks:
        await flag_untracked_write(app, provenance_session_id)

    return (
        f"Import complete ({on_conflict=}, {preserve_ids=}):\n"
        f"- Total in bundle:  {stats.total_chunks}\n"
        f"- Imported (new):   {stats.imported_chunks}\n"
        f"- Updated:          {stats.updated_chunks}\n"
        f"- Conflict skipped: {stats.conflict_skipped_chunks}\n"
        f"- Malformed:        {stats.skipped_chunks}\n"
        f"- Failed:           {stats.failed_chunks}"
    )
