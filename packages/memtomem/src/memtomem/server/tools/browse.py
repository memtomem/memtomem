"""Tools: mem_list, mem_read."""

from __future__ import annotations

from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import _display_path


@mcp.tool()
@tool_handler
async def mem_list(
    source_filter: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """List all indexed source files with chunk counts and metadata.

    Args:
        source_filter: Filter by source file path (substring match, or glob pattern with *, ?, [])
        namespace: Only list sources containing chunks in this namespace

    Examples:
        mem_list()                               — all indexed files
        mem_list(source_filter="*.md")           — markdown files (glob)
        mem_list(source_filter="docs/")          — files with "docs/" in path (substring)
        mem_list(namespace="work")               — files in the "work" namespace
    """
    from fnmatch import fnmatch

    app = await _get_app_initialized(ctx)
    rows = await app.storage.get_source_files_with_counts()

    if not rows:
        return "No indexed files."

    # Apply source_filter
    if source_filter:
        has_glob = any(c in source_filter for c in ("*", "?", "["))
        rows = [
            r
            for r in rows
            if (fnmatch(str(r[0]), source_filter) if has_glob else source_filter in str(r[0]))
        ]

    # Apply namespace filter
    if namespace:
        rows = [r for r in rows if r[3] and namespace in r[3].split(",")]

    if not rows:
        return "No files match the filter."

    lines = [f"Indexed files: {len(rows)}\n"]
    for path, count, updated, ns, avg_tok, min_tok, max_tok in rows:
        ns_label = f" [{ns}]" if ns else ""
        lines.append(f"  {_display_path(path)}  — {count} chunks, ~{avg_tok} tok/chunk{ns_label}")

    total_chunks = sum(r[1] for r in rows)
    lines.append(f"\nTotal: {len(rows)} files, {total_chunks} chunks")
    return "\n".join(lines)


@mcp.tool()
@tool_handler
async def mem_read(
    chunk_id: str,
    ctx: CtxType = None,
) -> str:
    """Read the full content and metadata of a specific chunk by its UUID.

    Use this to inspect a chunk before editing or deleting it,
    or to see the full content after a search result preview.

    Args:
        chunk_id: The UUID of the chunk (shown in mem_search results)
    """
    app = await _get_app_initialized(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Chunk {chunk_id} not found."

    meta = chunk.metadata
    parts = [
        f"## Chunk {chunk.id}",
        f"- Source: {_display_path(meta.source_file)}",
        f"- Lines: {meta.start_line}-{meta.end_line}",
    ]
    if meta.heading_hierarchy:
        parts.append(f"- Heading: {' > '.join(meta.heading_hierarchy)}")
    if meta.tags:
        parts.append(f"- Tags: {', '.join(meta.tags)}")
    if meta.namespace:
        parts.append(f"- Namespace: {meta.namespace}")
    parts.append(f"\n---\n\n{chunk.content}")

    return "\n".join(parts)
