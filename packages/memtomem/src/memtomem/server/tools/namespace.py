"""Tools: mem_ns_list, mem_ns_delete, mem_ns_set, mem_ns_get, mem_ns_rename,
mem_ns_update.
"""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_list(
    ctx: CtxType = None,
) -> str:
    """List all namespaces and their chunk counts."""
    app = await _get_app_initialized(ctx)
    ns_list = await app.storage.list_namespaces()

    if not ns_list:
        return "No namespaces found (index is empty)."

    parts = [f"Namespaces ({len(ns_list)} total):\n"]
    for ns, count in ns_list:
        parts.append(f"  {ns}: {count} chunks")

    return "\n".join(parts)


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_delete(
    namespace: str,
    ctx: CtxType = None,
) -> str:
    """Delete all chunks in a namespace from the index.

    The source files are NOT modified -- only the index entries are removed.

    Args:
        namespace: The namespace to delete.
    """
    if not namespace.strip():
        return "Error: namespace must be non-empty."
    app = await _get_app_initialized(ctx)
    deleted = await app.storage.delete_by_namespace(namespace)
    return f"Deleted {deleted} chunks from namespace '{namespace}'"


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_set(
    namespace: str,
    ctx: CtxType = None,
) -> str:
    """Set the session-default namespace. Subsequent search/add/recall use this unless overridden.

    Examples::
        mem_ns_set(namespace="work")
        mem_ns_set(namespace="project:myapp")
    """
    if not namespace.strip():
        return "Error: namespace must be non-empty."
    app = await _get_app_initialized(ctx)
    async with app._config_lock:
        app.current_namespace = namespace.strip()
    return f"Session namespace set to '{namespace.strip()}'"


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_get(
    ctx: CtxType = None,
) -> str:
    """Get the current session namespace."""
    app = await _get_app_initialized(ctx)
    ns = app.current_namespace
    if ns is None:
        return "No session namespace set (using global default)"
    return f"Current session namespace: '{ns}'"


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_rename(
    old: str,
    new: str,
    ctx: CtxType = None,
) -> str:
    """Rename a namespace (SQL UPDATE, no re-indexing needed).

    Examples::
        mem_ns_rename(old="project:v1", new="project:v2")
    """
    if not old.strip() or not new.strip():
        return "Error: both old and new namespace names must be non-empty."
    app = await _get_app_initialized(ctx)
    count = await app.storage.rename_namespace(old.strip(), new.strip())
    return f"Renamed namespace '{old.strip()}' -> '{new.strip()}' ({count} chunks updated)"


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_update(
    namespace: str,
    description: str | None = None,
    color: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Update namespace metadata (description and/or color).

    Args:
        namespace: The namespace to update
        description: Optional description text
        color: Optional color hex code (e.g. "#6c5ce7")
    """
    app = await _get_app_initialized(ctx)
    await app.storage.set_namespace_meta(namespace, description=description, color=color)
    return f"Updated metadata for namespace '{namespace}'"


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_assign(
    namespace: str,
    source_filter: str | None = None,
    old_namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Assign existing chunks to a namespace without re-indexing.

    Filter chunks by source path and/or current namespace, then move them
    to the target namespace. This is a SQL UPDATE — fast and non-destructive.

    Args:
        namespace: Target namespace to assign chunks to
        source_filter: Only assign chunks from sources containing this substring
        old_namespace: Only assign chunks currently in this namespace
    """
    if not namespace.strip():
        return "Error: namespace must be non-empty."
    if not source_filter and not old_namespace:
        return "Error: at least one filter (source_filter or old_namespace) is required."
    app = await _get_app_initialized(ctx)
    count = await app.storage.assign_namespace(
        namespace.strip(), source_filter=source_filter, old_namespace=old_namespace
    )
    filters = []
    if source_filter:
        filters.append(f"source={source_filter!r}")
    if old_namespace:
        filters.append(f"from={old_namespace!r}")
    suffix = f" ({', '.join(filters)})" if filters else " (all chunks)"
    return f"Assigned {count} chunks to namespace '{namespace}'{suffix}"
