"""Tools: mem_agent_register, mem_agent_search, mem_agent_share."""

from __future__ import annotations

import re

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

# Characters not in the namespace allowlist (mirrors ``_NS_SAFE_RE`` in
# ``cli/ingest_cmd.py``). Substituted to ``_`` so a caller-supplied
# ``agent_id`` cannot smuggle a second ``/`` separator into the generated
# ``agent/{agent_id}`` namespace — keeping the agent namespace at depth 1.
_AGENT_ID_SAFE_RE = re.compile(r"[^\w\-.:@ ]")


def _sanitize_agent_id(agent_id: str) -> str:
    """Strip surrounding whitespace and replace disallowed chars with ``_``.

    Returns the sanitized form; emptiness check is the caller's responsibility
    so the sanitizer itself has no error paths.
    """
    return _AGENT_ID_SAFE_RE.sub("_", agent_id.strip())


@mcp.tool()
@tool_handler
@register("multi_agent")
async def mem_agent_register(
    agent_id: str,
    description: str | None = None,
    color: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Register an agent in the multi-agent memory system.

    Creates a namespace for the agent (agent/{agent_id}) and optionally
    registers metadata. If the agent is already registered, updates metadata.

    Args:
        agent_id: Unique identifier for the agent
        description: Optional description of the agent's role
        color: Optional color hex code for UI display
    """
    if not agent_id or not agent_id.strip():
        return "Error: agent_id must be non-empty."
    agent_id = _sanitize_agent_id(agent_id)
    if not agent_id:
        return "Error: agent_id must contain at least one allowed character."
    app = _get_app(ctx)
    namespace = f"agent/{agent_id}"

    await app.storage.set_namespace_meta(namespace, description=description, color=color)

    # Ensure "shared" namespace exists
    shared_meta = await app.storage.get_namespace_meta("shared")
    if shared_meta is None:
        await app.storage.set_namespace_meta(
            "shared", description="Shared knowledge base for all agents"
        )

    return (
        f"Agent registered: {agent_id}\n"
        f"- Namespace: {namespace}\n"
        f"- Shared namespace: shared\n"
        f"Use namespace='{namespace}' for agent-specific memories,\n"
        f"or namespace='shared' for cross-agent knowledge."
    )


@mcp.tool()
@tool_handler
@register("multi_agent")
async def mem_agent_search(
    query: str,
    agent_id: str | None = None,
    include_shared: bool = True,
    top_k: int = 10,
    ctx: CtxType = None,
) -> str:
    """Search memories with multi-agent scope awareness.

    Searches the agent's private namespace and optionally the shared
    namespace, merging results by relevance.

    Args:
        query: Search query
        agent_id: Agent ID to search (omit for current agent)
        include_shared: Also search the shared namespace (default True)
        top_k: Maximum results to return
    """
    if agent_id is not None and not agent_id.strip():
        return "Error: agent_id must be non-empty if provided."
    if agent_id is not None:
        agent_id = _sanitize_agent_id(agent_id)
        if not agent_id:
            return "Error: agent_id must contain at least one allowed character."
    app = _get_app(ctx)
    from memtomem.server.formatters import _format_results

    agent_ns = f"agent/{agent_id}" if agent_id else app.current_namespace

    # Build namespace filter
    if include_shared and agent_ns:
        ns_filter = f"{agent_ns},shared"
    elif agent_ns:
        ns_filter = agent_ns
    else:
        ns_filter = None

    results, stats = await app.search_pipeline.search(
        query=query,
        top_k=top_k,
        namespace=ns_filter,
    )

    if not results:
        return f"No results found for agent '{agent_id or 'current'}'."

    output = _format_results(results)
    return output


@mcp.tool()
@tool_handler
@register("multi_agent")
async def mem_agent_share(
    chunk_id: str,
    target: str = "shared",
    ctx: CtxType = None,
) -> str:
    """Share a memory chunk with another agent or the shared namespace.

    Creates a copy of the chunk in the target namespace. The original
    chunk remains in its source namespace.

    Args:
        chunk_id: UUID of the chunk to share
        target: Target namespace — 'shared' or 'agent/{agent_id}'
    """
    from uuid import UUID

    app = _get_app(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Chunk {chunk_id} not found."

    # Create a cross-reference link instead of copying
    # This avoids data duplication while maintaining the relationship
    from memtomem.server.tools.memory_crud import mem_add

    result = await mem_add(
        content=chunk.content,
        title=f"Shared: {' > '.join(chunk.metadata.heading_hierarchy) if chunk.metadata.heading_hierarchy else 'memory'}",
        tags=list(chunk.metadata.tags),
        namespace=target,
        ctx=ctx,
    )

    return f"Shared to namespace '{target}'.\n{result}"
