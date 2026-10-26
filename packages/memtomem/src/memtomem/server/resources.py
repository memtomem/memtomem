"""MCP Resources — expose indexed data for client auto-discovery."""

from __future__ import annotations

import json
from uuid import UUID

from mcp.server.mcpserver import ResourceSecurity
from mcp.shared.uri_template import UriTemplate

from memtomem.server import mcp
from memtomem.server.context import (
    AppContext,
    CtxType,
    _get_active_app_initialized,
    _get_app_initialized,
)
from memtomem.server.tools._id_access import caller_boundary, resolve_chunk

_SOURCES_URI = "memtomem://sources"
_SOURCES_PAGE_TEMPLATE = "memtomem://sources{?cursor,limit,namespace}"
# The same page as ``mem_list``'s defaults, so a client switching between the
# tool and the resource sees the same first fifty files.
_SOURCES_PAGE_LIMIT = 50
_SOURCES_PAGE_MAX = 500

# The 2.0 SDK injects ``Context`` into templated resource handlers only —
# a static URI carries no request, and declaring the parameter there is a
# registration-time error. The four static resources below therefore read
# the lifespan's AppContext through ``_get_active_app_initialized``;
# ``chunk_resource`` is templated and keeps the injected ctx.


async def _sources_page(
    app: AppContext, *, cursor: str | None, limit: int, namespace: str | None
) -> str:
    """One page of visible source files as JSON, with the URI of the next page.

    Visibility is ``mem_list``'s (#2753): held and pending sources, policy
    summaries of hidden parents and sources that live only in system
    namespaces — unless ``namespace`` names one — are dropped in SQL before
    the page is cut, so the resource and the tool show one listing. The
    response is one page whatever the store holds; ``next`` is ``None`` on
    the last page.
    """
    if not 1 <= limit <= _SOURCES_PAGE_MAX:
        return json.dumps(
            {"error": f"limit must be between 1 and {_SOURCES_PAGE_MAX}, got {limit}"}
        )
    rows, next_cursor = await app.storage.get_source_files_page(
        after=cursor,
        limit=limit,
        namespace=namespace,
        system_prefixes=tuple(app.config.search.system_namespace_prefixes),
    )
    sources = [
        {
            "path": str(path),
            "chunks": count,
            "updated": updated,
            "namespaces": ns,
            "avg_tokens": avg_tok,
        }
        for path, count, updated, ns, avg_tok, _min_tok, _max_tok in rows
    ]
    next_uri: str | None = None
    if next_cursor is not None:
        variables = {"cursor": next_cursor}
        if limit != _SOURCES_PAGE_LIMIT:
            variables["limit"] = str(limit)
        if namespace:
            variables["namespace"] = namespace
        # Expanded by the SDK's own template so the percent-encoding is the
        # one its matcher decodes; the cursor is a stored path and may carry
        # anything a path can.
        next_uri = UriTemplate.parse(_SOURCES_PAGE_TEMPLATE).expand(variables)
    return json.dumps({"sources": sources, "page_size": limit, "next": next_uri}, indent=2)


@mcp.resource(_SOURCES_URI)
async def sources_resource() -> str:
    """The first page of visible source files with chunk counts.

    Follow ``next`` for the rest; it reads through the
    ``memtomem://sources{?cursor,limit,namespace}`` template.
    """
    app = await _get_active_app_initialized()
    return await _sources_page(app, cursor=None, limit=_SOURCES_PAGE_LIMIT, namespace=None)


# Both query values are bound SQL parameters, never paths the server opens:
# ``cursor`` is the stored path of the last file on the previous page (an
# absolute path, which the default template security rejects) and
# ``namespace`` is whatever string the chunks were stored under (``a:b``
# reads as a drive letter to the same checks). ``mem_list`` accepts both as
# is, so the resource must too.
@mcp.resource(
    _SOURCES_PAGE_TEMPLATE,
    security=ResourceSecurity(exempt_params={"cursor", "namespace"}),
)
async def sources_page_resource(
    cursor: str | None = None,
    limit: int = _SOURCES_PAGE_LIMIT,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """A page of visible source files starting after ``cursor``.

    ``limit`` is 1–500 (default 50); ``namespace`` restricts the page to
    files with chunks in that namespace and is the only way to list files
    that live solely in a system namespace, as with ``mem_list``.
    """
    app = await _get_app_initialized(ctx)
    return await _sources_page(app, cursor=cursor, limit=limit, namespace=namespace)


@mcp.resource("memtomem://namespaces")
async def namespaces_resource() -> str:
    """List all namespaces and their chunk counts."""
    app = await _get_active_app_initialized()
    ns_list = await app.storage.list_namespaces()
    result = [{"namespace": ns, "chunks": count} for ns, count in ns_list]
    return json.dumps(result, indent=2)


@mcp.resource("memtomem://tags")
async def tags_resource() -> str:
    """List tags visible in the caller's project boundary and their counts."""
    app = await _get_active_app_initialized()
    tag_counts = await app.storage.get_tag_counts(project_context_root=caller_boundary(app))
    result = [{"tag": tag, "chunks": count} for tag, count in tag_counts]
    return json.dumps(result, indent=2)


@mcp.resource("memtomem://stats")
async def stats_resource() -> str:
    """Current index statistics."""
    app = await _get_active_app_initialized()
    stats = await app.storage.get_stats()
    return json.dumps(stats, indent=2)


@mcp.resource("memtomem://chunks/{chunk_id}")
async def chunk_resource(chunk_id: str, ctx: CtxType = None) -> str:
    """Read a specific chunk by UUID, inside the caller's project boundary.

    Same rule as ``mem_read`` — an id from another project reads as not-found
    (ADR-0036). The resource is a second door onto the same read; leaving it
    open would make the tool's screen decorative.
    """
    app = await _get_app_initialized(ctx)
    chunk = await resolve_chunk(app, UUID(chunk_id))
    if chunk is None:
        return json.dumps({"error": f"Chunk {chunk_id} not found"})
    meta = chunk.metadata
    return json.dumps(
        {
            "id": str(chunk.id),
            "content": chunk.content,
            "retrieval_context": meta.retrieval_context,
            "redaction_count": meta.redaction_count,
            "source_read_only": meta.source_read_only,
            "source_file": str(meta.source_file),
            "heading_hierarchy": list(meta.heading_hierarchy),
            "tags": list(meta.tags),
            "namespace": meta.namespace,
            "start_line": meta.start_line,
            "end_line": meta.end_line,
            "created_at": chunk.created_at.isoformat(),
            "updated_at": chunk.updated_at.isoformat(),
        },
        indent=2,
    )
