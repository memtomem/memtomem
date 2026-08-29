"""Tools: mem_list, mem_read.

``mem_read`` resolves its id through the ADR-0011 project boundary
(``_id_access.resolve_chunk``): outside a project only user-scope chunks
resolve, inside one user-scope plus that project's, and an id from another
project answers exactly as a nonexistent one — knowing an id is not
authorization. It deliberately does *not* screen system namespaces or
temporal validity: those are search-relevance defaults an explicit
``namespace=`` already lifts, so reading an archived or expired chunk by id
keeps working (ADR-0036). The tool's own description says this in one line,
because the core-tool description budget is a hard test.
"""

from __future__ import annotations

from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import _display_path
from memtomem.server.tools._id_access import not_found, resolve_chunk


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
    from memtomem.search.pipeline import match_source_filter

    app = await _get_app_initialized(ctx)
    rows = await app.storage.get_source_files_with_counts()

    if not rows:
        return "No indexed files."

    # Apply source_filter — same substring + glob contract as
    # ``mem_search(source_filter=...)``, including separator-fold for
    # Windows portability (#720).
    if source_filter:
        rows = [r for r in rows if match_source_filter(source_filter, str(r[0]))]

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
    """Read a chunk's content and metadata by UUID.

    Inspect a chunk before editing, or see the text behind a search
    preview. Ids resolve in the current project's scope; another's
    reads as not found.

    Args:
        chunk_id: The chunk's UUID (from mem_search results)
    """
    app = await _get_app_initialized(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await resolve_chunk(app, uid)
    if chunk is None:
        return not_found(chunk_id)

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
