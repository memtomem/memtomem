"""MCP Resources — expose indexed data for client auto-discovery."""

from __future__ import annotations

import json
from uuid import UUID

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app


@mcp.resource("memtomem://sources")
async def sources_resource(ctx: CtxType = None) -> str:
    """List all indexed source files with chunk counts."""
    app = _get_app(ctx)
    rows = await app.storage.get_source_files_with_counts()
    result = []
    for path, count, updated, ns, avg_tok, min_tok, max_tok in rows:
        result.append(
            {
                "path": str(path),
                "chunks": count,
                "updated": updated,
                "namespaces": ns,
                "avg_tokens": avg_tok,
            }
        )
    return json.dumps(result, indent=2)


@mcp.resource("memtomem://namespaces")
async def namespaces_resource(ctx: CtxType = None) -> str:
    """List all namespaces and their chunk counts."""
    app = _get_app(ctx)
    ns_list = await app.storage.list_namespaces()
    result = [{"namespace": ns, "chunks": count} for ns, count in ns_list]
    return json.dumps(result, indent=2)


@mcp.resource("memtomem://tags")
async def tags_resource(ctx: CtxType = None) -> str:
    """List all tags and their usage counts."""
    app = _get_app(ctx)
    tag_counts = await app.storage.get_tag_counts()
    result = [{"tag": tag, "chunks": count} for tag, count in tag_counts]
    return json.dumps(result, indent=2)


@mcp.resource("memtomem://stats")
async def stats_resource(ctx: CtxType = None) -> str:
    """Current index statistics."""
    app = _get_app(ctx)
    stats = await app.storage.get_stats()
    return json.dumps(stats, indent=2)


@mcp.resource("memtomem://chunks/{chunk_id}")
async def chunk_resource(chunk_id: str, ctx: CtxType = None) -> str:
    """Read a specific chunk by UUID."""
    app = _get_app(ctx)
    chunk = await app.storage.get_chunk(UUID(chunk_id))
    if chunk is None:
        return json.dumps({"error": f"Chunk {chunk_id} not found"})
    meta = chunk.metadata
    return json.dumps(
        {
            "id": str(chunk.id),
            "content": chunk.content,
            "source_file": str(meta.source_file),
            "heading_hierarchy": list(meta.heading_hierarchy),
            "tags": list(meta.tags),
            "namespace": meta.namespace,
            "start_line": meta.start_line,
            "end_line": meta.end_line,
            "created_at": str(meta.created_at),
        },
        indent=2,
    )
