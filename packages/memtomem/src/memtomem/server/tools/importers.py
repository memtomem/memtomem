"""Tools: mem_import_notion, mem_import_obsidian — migrate notes from other apps."""

from __future__ import annotations

from pathlib import Path

from memtomem.errors import ConfigError
from memtomem.memory_scope import require_user_base
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import (
    capture_session_for_untracked_write,
    flag_untracked_write,
)


@mcp.tool()
@tool_handler
@register("importers")
async def mem_import_notion(
    path: str,
    namespace: str | None = None,
    tags: list[str] | None = None,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Import a Notion export (ZIP or directory) into memtomem.

    Cleans Notion-specific artifacts (UUID filenames, property tables,
    broken links) and indexes the imported files for search.

    Args:
        path: Path to Notion export ZIP file or extracted directory.
        namespace: Namespace for imported content (default: "notion").
        tags: Tags to apply to all imported chunks.
        force_unsafe: Bypass the redaction guard when an exported page matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line (see ``mem_add_redaction_stats``).
            It never applies to a ``project_shared`` destination — that
            combination is hard-refused, because git history cannot be
            retracted from clones.
    """
    from memtomem.indexing.importers import import_notion

    app = await _get_app_initialized(ctx)
    # A bulk import is not session work — its chunks are an ingest, and
    # summarizing them would describe someone else's notes as this
    # session's output. But it does change the session's chunk set, so
    # the session must stop claiming its provenance is the whole story.
    # Captured before the import's awaits so a session that ends midway
    # still gets the flag.
    provenance_session_id = await capture_session_for_untracked_write(app)
    export_path = Path(path).expanduser().resolve()

    if not export_path.exists():
        return f"Error: Path not found: {export_path}"

    try:
        memory_dir = require_user_base(app.config.indexing.memory_dirs)
    except ConfigError as exc:
        return f"Error: {exc}"
    output_dir = memory_dir / "_imported" / "notion"

    from memtomem.config import classify_scope

    scope, _ = classify_scope(output_dir, app.config.indexing.project_memory_dirs)
    blocked_paths: list[str] = []
    imported = await import_notion(
        export_path,
        output_dir,
        force_unsafe=force_unsafe,
        scope=scope,
        blocked_paths=blocked_paths,
    )

    if not imported and blocked_paths:
        return f"Notion import blocked by redaction guard: {len(blocked_paths)} file(s)."
    if not imported:
        return "No markdown files found in the Notion export."

    # Index all imported files. ADR-0006 PR-A: imported content is
    # un-adjudicated, so the engine redaction gate is active; skip + count
    # secret-bearing files rather than aborting the whole import.
    from memtomem.indexing.engine import PrivacyRejection

    effective_ns = namespace or "notion"
    total_chunks = 0
    blocked = len(blocked_paths)
    for f in imported:
        try:
            stats = await app.index_engine.index_file(
                f, namespace=effective_ns, already_scanned=True
            )
        except PrivacyRejection:
            blocked += 1
            continue
        total_chunks += stats.indexed_chunks

    # Apply tags
    if tags and total_chunks > 0:
        for f in imported:
            chunks = await app.storage.list_chunks_by_source(f)
            for c in chunks:
                merged = set(c.metadata.tags) | set(tags) | {"notion", "imported"}
                if merged != set(c.metadata.tags):
                    c.metadata = c.metadata.__class__(
                        **{
                            **{
                                field: getattr(c.metadata, field)
                                for field in c.metadata.__dataclass_fields__
                            },
                            "tags": tuple(sorted(merged)),
                        }
                    )
            if chunks:
                await app.storage.upsert_chunks(chunks)

    app.search_pipeline.invalidate_cache()

    if total_chunks:
        await flag_untracked_write(app, provenance_session_id)

    return (
        f"Notion import complete:\n"
        f"- Files imported: {len(imported)}\n"
        f"- Chunks indexed: {total_chunks}\n"
        f"- Blocked (redaction): {blocked}\n"
        f"- Namespace: {effective_ns}\n"
        f"- Output: {output_dir}"
    )


@mcp.tool()
@tool_handler
@register("importers")
async def mem_import_obsidian(
    vault_path: str,
    namespace: str | None = None,
    tags: list[str] | None = None,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Import an Obsidian vault into memtomem.

    Converts Obsidian-specific syntax ([[wikilinks]], ![[embeds]],
    callouts) to standard markdown and indexes for search.

    Args:
        vault_path: Path to Obsidian vault root directory.
        namespace: Namespace for imported content (default: "obsidian").
        tags: Tags to apply to all imported chunks.
        force_unsafe: Bypass the redaction guard when a vault note matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line (see ``mem_add_redaction_stats``).
            It never applies to a ``project_shared`` destination — that
            combination is hard-refused, because git history cannot be
            retracted from clones.
    """
    from memtomem.indexing.importers import import_obsidian

    app = await _get_app_initialized(ctx)
    # A bulk import is not session work — its chunks are an ingest, and
    # summarizing them would describe someone else's notes as this
    # session's output. But it does change the session's chunk set, so
    # the session must stop claiming its provenance is the whole story.
    # Captured before the import's awaits so a session that ends midway
    # still gets the flag.
    provenance_session_id = await capture_session_for_untracked_write(app)
    vault = Path(vault_path).expanduser().resolve()

    if not vault.exists() or not vault.is_dir():
        return f"Error: Obsidian vault not found: {vault}"

    try:
        memory_dir = require_user_base(app.config.indexing.memory_dirs)
    except ConfigError as exc:
        return f"Error: {exc}"
    output_dir = memory_dir / "_imported" / "obsidian"

    from memtomem.config import classify_scope

    scope, _ = classify_scope(output_dir, app.config.indexing.project_memory_dirs)
    blocked_paths: list[str] = []
    imported = await import_obsidian(
        vault,
        output_dir,
        force_unsafe=force_unsafe,
        scope=scope,
        blocked_paths=blocked_paths,
    )

    if not imported and blocked_paths:
        return f"Obsidian import blocked by redaction guard: {len(blocked_paths)} file(s)."
    if not imported:
        return "No markdown files found in the Obsidian vault."

    # Index all imported files. ADR-0006 PR-A: imported content is
    # un-adjudicated, so the engine redaction gate is active; skip + count
    # secret-bearing files rather than aborting the whole import.
    from memtomem.indexing.engine import PrivacyRejection

    effective_ns = namespace or "obsidian"
    total_chunks = 0
    blocked = len(blocked_paths)
    for f in imported:
        try:
            stats = await app.index_engine.index_file(
                f, namespace=effective_ns, already_scanned=True
            )
        except PrivacyRejection:
            blocked += 1
            continue
        total_chunks += stats.indexed_chunks

    # Apply tags
    if tags and total_chunks > 0:
        for f in imported:
            chunks = await app.storage.list_chunks_by_source(f)
            for c in chunks:
                merged = set(c.metadata.tags) | set(tags) | {"obsidian", "imported"}
                if merged != set(c.metadata.tags):
                    c.metadata = c.metadata.__class__(
                        **{
                            **{
                                field: getattr(c.metadata, field)
                                for field in c.metadata.__dataclass_fields__
                            },
                            "tags": tuple(sorted(merged)),
                        }
                    )
            if chunks:
                await app.storage.upsert_chunks(chunks)

    app.search_pipeline.invalidate_cache()

    if total_chunks:
        await flag_untracked_write(app, provenance_session_id)

    return (
        f"Obsidian import complete:\n"
        f"- Files imported: {len(imported)}\n"
        f"- Chunks indexed: {total_chunks}\n"
        f"- Blocked (redaction): {blocked}\n"
        f"- Namespace: {effective_ns}\n"
        f"- Output: {output_dir}"
    )
