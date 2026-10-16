"""Tools: mem_list, mem_read.

``mem_list`` pages through source files in stored-path order: each page is one
bounded query, filtered before its ``LIMIT``, and a page that is not the whole
listing ends with a ``More:`` call carrying the stored path of its last file as
the cursor. A store that is a single page reads exactly as the unpaged listing
did. Page counts are that page's, never a store total — ``mem_stats`` has those.

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

import json
from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import _display_path
from memtomem.server.tools._id_access import not_found, resolve_chunk

_DEFAULT_LIMIT = 50


@mcp.tool()
@tool_handler
async def mem_list(
    source_filter: str | None = None,
    namespace: str | None = None,
    limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
    ctx: CtxType = None,
) -> str:
    """List indexed source files with chunk counts, one page at a time.

    Args:
        source_filter: Filter by source file path (substring match, or glob pattern with *, ?, [])
        namespace: Only list sources containing chunks in this namespace
        limit: Files per page (1-500, default 50)
        cursor: Where to resume; copy it from the previous page's "More:" line

    Examples:
        mem_list(source_filter="*.md")           — markdown files (glob)
        mem_list(namespace="work")               — files in the "work" namespace
    """
    if not 1 <= limit <= 500:
        return f"Error: limit must be between 1 and 500, got {limit}."

    app = await _get_app_initialized(ctx)
    # Hidden files (held, pending, policy summaries of either), files the
    # source filter rejects, and — without ``namespace=`` — files that live only
    # in system namespaces are all dropped in SQL, before the page is cut.
    rows, next_cursor = await app.storage.get_source_files_page(
        after=cursor,
        limit=limit,
        source_filter=source_filter,
        namespace=namespace,
        system_prefixes=tuple(app.config.search.system_namespace_prefixes),
    )

    if not rows:
        if cursor is not None:
            return "No more files."
        # Tell an empty store from an over-narrow filter, as the unpaged
        # listing did: only hidden files count as nothing indexed.
        visible, _ = await app.storage.get_source_files_page(after=None, limit=1)
        return "No files match the filter." if visible else "No indexed files."

    lines = []
    for path, count, updated, ns, avg_tok, min_tok, max_tok in rows:
        ns_label = f" [{ns}]" if ns else ""
        lines.append(f"  {_display_path(path)}  — {count} chunks, ~{avg_tok} tok/chunk{ns_label}")
    total_chunks = sum(r[1] for r in rows)

    if cursor is None and next_cursor is None:
        return "\n".join(
            [
                f"Indexed files: {len(rows)}\n",
                *lines,
                f"\nTotal: {len(rows)} files, {total_chunks} chunks",
            ]
        )

    footer = [f"\nPage: {len(rows)} files, {total_chunks} chunks"]
    if next_cursor is None:
        footer.append("End of list.")
    else:
        args = [f"cursor={json.dumps(next_cursor)}"]
        if source_filter:
            args.append(f"source_filter={json.dumps(source_filter)}")
        if namespace:
            args.append(f"namespace={json.dumps(namespace)}")
        if limit != _DEFAULT_LIMIT:
            args.append(f"limit={limit}")
        footer.append(f"More: mem_list({', '.join(args)})")
    return "\n".join([f"Indexed files (page): {len(rows)}\n", *lines, *footer])


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
