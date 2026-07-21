"""Tools: mem_ns_list, mem_ns_delete, mem_ns_set, mem_ns_get, mem_ns_rename,
mem_ns_update.
"""

from __future__ import annotations

from pydantic import StrictBool

from memtomem.constants import validate_namespace
from memtomem.errors import NamespaceConflictError
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import (
    capture_session_for_untracked_write,
    flag_untracked_write,
)
from memtomem.server.tools._validation import strict_bool


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
    validate_namespace(namespace)
    app = await _get_app_initialized(ctx)
    # Removing chunks an earlier provenance event still names leaves the
    # session's record describing a chunk set that no longer exists.
    provenance_session_id = await capture_session_for_untracked_write(app)
    deleted = await app.storage.delete_by_namespace(namespace)
    if deleted:
        await flag_untracked_write(app, provenance_session_id)
    return f"Deleted {deleted} chunks from namespace '{namespace}'"


@mcp.tool()
@tool_handler
@register("namespace")
async def mem_ns_set(
    namespace: str,
    ctx: CtxType = None,
) -> str:
    """Set the session-default namespace.

    Subsequent search / add / recall use it unless they pass namespace=.

    One exception: while a session is active this is the *read* default only —
    resolver-backed writes go to agent-runtime:<agent_id> unless the call
    passes namespace= explicitly.

    ``namespace`` is run through :func:`validate_namespace` before the
    write, mirroring ``mem_session_start(namespace=...)``. Without the
    gate, an attacker who controls the value reaching ``mem_ns_set`` could
    write a hostile-shaped string into ``app.current_namespace`` — and a
    later ``mem_session_start(agent_id="default")`` would land that string
    in the ``sessions`` row via the ``current_namespace`` fallback,
    re-opening the bypass issue #496 closed at the explicit
    ``namespace=`` surface. See issue #500 for the transitive-bypass
    write-up.

    Args:
        namespace: Namespace to make the session default. Validated before
            the write; a rejected value leaves the current default in place.

    Examples::
        mem_ns_set(namespace="work")
        mem_ns_set(namespace="project:myapp")
    """
    validate_namespace(namespace)
    app = await _get_app_initialized(ctx)
    async with app._config_lock:
        app.current_namespace = namespace
    return f"Session namespace set to '{namespace}'"


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
    # StrictBool, not bool: FastMCP builds a LAX pydantic arg model from these
    # annotations, so a bare ``bool`` would coerce ``1`` / ``"true"`` into a
    # merge — a destructive consolidation the caller never asked for.
    # ``strict_bool`` in the body then covers the ``mem_do`` path, which
    # bypasses that model entirely. Both are needed.
    merge: StrictBool = False,
    ctx: CtxType = None,
) -> str:
    """Rename a namespace (SQL UPDATE, no re-indexing needed).

    Refuses when ``new`` already exists (holds chunks or a metadata row)
    so a rename can't silently fold two namespaces together. Pass
    ``merge=True`` to consolidate on purpose: chunks move into ``new``
    and the *target's* description/color are kept.

    Both ``old`` and ``new`` are run through :func:`validate_namespace`
    so a hostile-shaped string cannot land verbatim in the chunks /
    namespace_metadata rows via the rename path. See issue #500.

    Args:
        old: Namespace to rename. A namespace that exists only as
            metadata (registered, zero chunks) renames fine — the
            reported chunk count is 0 but the metadata row moves.
        new: New name. Must not already exist unless ``merge=True``.
        merge: Consolidate into an existing ``new`` instead of refusing.

    Examples::
        mem_ns_rename(old="project:v1", new="project:v2")
        mem_ns_rename(old="project:draft", new="project:v2", merge=True)

    (Legacy ``agent/{id}`` namespaces cannot be named here — the slash fails
    validation. Use ``mm agent migrate``, which consolidates them.)
    """
    validate_namespace(old)
    validate_namespace(new)
    merge = strict_bool(merge, "merge")
    app = await _get_app_initialized(ctx)
    try:
        result = await app.storage.rename_namespace(old, new, merge=merge)
    except NamespaceConflictError as exc:
        # Storage states the condition; this surface adds the remedy that
        # exists *here* — an MCP caller can retry with merge=True, which a
        # web user cannot. See the reason_code note on the exception.
        if exc.reason_code == "target_exists":
            raise NamespaceConflictError(
                f"{exc}. Pass merge=True to consolidate into it — the target's "
                f"description/color are kept, and chunks it already holds are "
                f"dropped rather than duplicated",
                reason_code=exc.reason_code,
            ) from exc
        raise
    if not (result.chunks_moved or result.metadata_renamed or result.merged):
        # Nothing to move: a namespace is its chunks and its metadata row, and
        # this one had neither. Saying "Renamed" would be a lie.
        return f"Namespace '{old}' not found — nothing renamed."
    if result.merged:
        detail = f"merged into existing '{new}'"
        if result.duplicates_dropped:
            detail += (
                f", {result.duplicates_dropped} duplicate chunk(s) dropped "
                f"(already present in '{new}')"
            )
    elif result.metadata_renamed:
        detail = "metadata row renamed"
    else:
        detail = "no metadata row"
    return f"Renamed namespace '{old}' -> '{new}' ({result.chunks_moved} chunks moved, {detail})"


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

    ``namespace`` is run through :func:`validate_namespace` so the lookup
    key cannot carry a hostile shape into the ``namespace_metadata``
    write. See issue #500.

    Args:
        namespace: The namespace to update
        description: Optional description text
        color: Optional color hex code (e.g. "#6c5ce7")
    """
    validate_namespace(namespace)
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
    # This can delete redundant source rows, so consent must stay literal on
    # both FastMCP and raw mem_do paths. See mem_ns_rename for the two gates.
    merge: StrictBool = False,
    ctx: CtxType = None,
) -> str:
    """Assign existing chunks to a namespace without re-indexing.

    Filter chunks by source path and/or current namespace, then move them
    to the target namespace. If a selected chunk already exists there, the
    default call refuses before writing. Pass ``merge=True`` to keep the
    target's copy and remove redundant selected copies on purpose.

    Both ``namespace`` and ``old_namespace`` (when provided) are run
    through :func:`validate_namespace` so a hostile-shaped target cannot
    land verbatim in the chunks rows via the assign path. See issue #500.

    Args:
        namespace: Target namespace to assign chunks to
        source_filter: Only assign chunks from sources containing this substring
        old_namespace: Only assign chunks currently in this namespace
        merge: Consolidate overlapping chunks instead of refusing. Must be a
            literal boolean; moved and dropped rows are reported separately.
    """
    validate_namespace(namespace)
    if old_namespace is not None:
        validate_namespace(old_namespace)
    if not source_filter and not old_namespace:
        return "Error: at least one filter (source_filter or old_namespace) is required."
    merge = strict_bool(merge, "merge")
    app = await _get_app_initialized(ctx)
    try:
        result = await app.storage.assign_namespace(
            namespace,
            source_filter=source_filter,
            old_namespace=old_namespace,
            merge=merge,
        )
    except NamespaceConflictError as exc:
        if exc.reason_code == "chunk_overlap":
            raise NamespaceConflictError(
                f"{exc}. Pass merge=True to consolidate deliberately — existing target "
                "copies are kept and redundant selected copies are dropped",
                reason_code=exc.reason_code,
            ) from exc
        raise
    filters = []
    if source_filter:
        filters.append(f"source={source_filter!r}")
    if old_namespace:
        filters.append(f"from={old_namespace!r}")
    suffix = f" ({', '.join(filters)})" if filters else " (all chunks)"
    detail = ""
    if result.duplicates_dropped:
        detail = f", {result.duplicates_dropped} duplicate chunk(s) dropped"
    return (
        f"Assigned {result.chunks_moved} chunks to namespace '{namespace}'"
        f"{suffix}{detail}"
    )
