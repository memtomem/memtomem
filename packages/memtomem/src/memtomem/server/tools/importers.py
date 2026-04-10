"""Tools: mem_import_notion, mem_import_obsidian — migrate notes from other apps."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("importers")
async def mem_import_notion(
    path: str,
    namespace: str | None = None,
    tags: list[str] | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Import a Notion export (ZIP or directory) into memtomem.

    Cleans Notion-specific artifacts (UUID filenames, property tables,
    broken links) and indexes the imported files for search.

    Args:
        path: Path to Notion export ZIP file or extracted directory.
        namespace: Namespace for imported content (default: "notion").
        tags: Tags to apply to all imported chunks.
    """
    from memtomem.indexing.importers import import_notion

    app = _get_app(ctx)
    export_path = Path(path).expanduser().resolve()

    if not export_path.exists():
        return f"Error: Path not found: {export_path}"

    if not app.config.indexing.memory_dirs:
        return "Error: no memory directories configured. Run 'mm init' first."
    memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
    output_dir = memory_dir / "_imported" / "notion"

    imported = await import_notion(export_path, output_dir)

    if not imported:
        return "No markdown files found in the Notion export."

    # Index all imported files
    effective_ns = namespace or "notion"
    total_chunks = 0
    for f in imported:
        stats = await app.index_engine.index_file(f, namespace=effective_ns)
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

    return (
        f"Notion import complete:\n"
        f"- Files imported: {len(imported)}\n"
        f"- Chunks indexed: {total_chunks}\n"
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
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Import an Obsidian vault into memtomem.

    Converts Obsidian-specific syntax ([[wikilinks]], ![[embeds]],
    callouts) to standard markdown and indexes for search.

    Args:
        vault_path: Path to Obsidian vault root directory.
        namespace: Namespace for imported content (default: "obsidian").
        tags: Tags to apply to all imported chunks.
    """
    from memtomem.indexing.importers import import_obsidian

    app = _get_app(ctx)
    vault = Path(vault_path).expanduser().resolve()

    if not vault.exists() or not vault.is_dir():
        return f"Error: Obsidian vault not found: {vault}"

    if not app.config.indexing.memory_dirs:
        return "Error: no memory directories configured. Run 'mm init' first."
    memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
    output_dir = memory_dir / "_imported" / "obsidian"

    imported = await import_obsidian(vault, output_dir)

    if not imported:
        return "No markdown files found in the Obsidian vault."

    # Index all imported files
    effective_ns = namespace or "obsidian"
    total_chunks = 0
    for f in imported:
        stats = await app.index_engine.index_file(f, namespace=effective_ns)
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

    return (
        f"Obsidian import complete:\n"
        f"- Files imported: {len(imported)}\n"
        f"- Chunks indexed: {total_chunks}\n"
        f"- Namespace: {effective_ns}\n"
        f"- Output: {output_dir}"
    )
