"""Tools: mem_agent_register, mem_agent_search, mem_agent_share."""

from __future__ import annotations

import logging

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    validate_agent_id,
    validate_namespace,
)
from memtomem.server import mcp
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.formatters import OutputFormat, _VALID_OUTPUT_FORMATS
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


def _resolve_agent_namespace(app: AppContext, agent_id: str | None) -> str | None:
    """Resolve the namespace a session-aware MCP tool should target.

    Used by both the read path (``mem_agent_search``,
    ``mem_agent_share``) and the write path (``mem_add``,
    ``mem_batch_add``, ``mem_index``, ``mem_fetch``) so the
    "session bound writes go to the agent scope" contract holds
    across the entire MCP surface — see G1 in
    ``memtomem-docs/memtomem/planning/multi-agent-public-surface-review-2026-04-26.md``.

    Priority order (each falls back to the next when ``None``):

    1. Explicit ``agent_id`` argument — the caller wants to override the
       session context for this single call. Only the read tools take
       an ``agent_id`` arg today; write tools always pass ``None``.
    2. ``app.current_agent_id`` — set by ``mem_session_start(agent_id=...)``;
       lets agents avoid repeating their identity on every tool call.
    3. ``app.current_namespace`` — pre-multi-agent legacy fallback. Kept
       so workflows that pre-date session-driven agent inheritance keep
       working unchanged.

    Returns ``None`` if no source resolved a namespace, in which case
    the caller treats the operation as un-pinned (read tools search
    everything; write tools defer to the indexing engine's auto-NS
    rules and config default).
    """

    if agent_id:
        return f"{AGENT_NAMESPACE_PREFIX}{agent_id}"
    if app.current_agent_id:
        return f"{AGENT_NAMESPACE_PREFIX}{app.current_agent_id}"
    return app.current_namespace


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

    Creates a namespace for the agent (``agent-runtime:{agent_id}``) and
    optionally registers metadata. If the agent is already registered,
    updates metadata.

    ``agent_id`` must match the canonical ``[A-Za-z0-9._-]`` charset used
    everywhere else that builds an ``agent-runtime:`` namespace; hostile
    shapes like ``"foo:bar"`` or ``"../x"`` are rejected loudly rather
    than silently sanitised. This keeps the read/write contract symmetric
    with ``mem_session_start`` so the same id either works on every
    surface or fails on every surface.

    Args:
        agent_id: Unique identifier for the agent
        description: Optional description of the agent's role
        color: Optional color hex code for UI display
    """
    validate_agent_id(agent_id)
    app = await _get_app_initialized(ctx)
    namespace = f"{AGENT_NAMESPACE_PREFIX}{agent_id}"

    await app.storage.set_namespace_meta(namespace, description=description, color=color)

    # Ensure shared namespace exists
    shared_meta = await app.storage.get_namespace_meta(SHARED_NAMESPACE)
    if shared_meta is None:
        await app.storage.set_namespace_meta(
            SHARED_NAMESPACE, description="Shared knowledge base for all agents"
        )

    return (
        f"Agent registered: {agent_id}\n"
        f"- Namespace: {namespace}\n"
        f"- Shared namespace: {SHARED_NAMESPACE}\n"
        f"Use namespace='{namespace}' for agent-specific memories,\n"
        f"or namespace='{SHARED_NAMESPACE}' for cross-agent knowledge."
    )


@mcp.tool()
@tool_handler
@register("multi_agent")
async def mem_agent_search(
    query: str,
    agent_id: str | None = None,
    include_shared: bool = True,
    top_k: int = 10,
    output_format: OutputFormat = "compact",
    ctx: CtxType = None,
) -> str:
    """Search memories with multi-agent scope awareness.

    Searches the agent's private namespace and optionally the shared
    namespace, merging results by relevance.

    When ``agent_id`` is supplied it must match the canonical
    ``[A-Za-z0-9._-]`` charset enforced at registration / session start;
    hostile shapes are rejected loudly so a typoed lookup can't quietly
    fall back to "search everything". Pass ``None`` to use the active
    session's agent (set by ``mem_session_start``) or the legacy
    ``current_namespace`` fallback.

    Args:
        query: Search query
        agent_id: Agent ID to search (omit for current agent)
        include_shared: Also search the shared namespace (default True)
        top_k: Maximum results to return
        output_format: Output format — ``"compact"`` (default,
            human-readable), ``"verbose"`` (full details with UUID), or
            ``"structured"`` (JSON for machine parsing — exposes
            ``chunk_id`` directly so callers can capture UUIDs without
            scraping). Mirrors the same option on ``mem_search`` /
            ``mem_recall`` so callers don't have to switch tools to get
            structured output.
    """
    if agent_id is not None:
        validate_agent_id(agent_id)
    if output_format not in _VALID_OUTPUT_FORMATS:
        return f"Error: invalid output_format '{output_format}'."
    app = await _get_app_initialized(ctx)
    from memtomem.server.formatters import _format_results, _format_structured_results

    agent_ns = _resolve_agent_namespace(app, agent_id)

    # Build namespace filter
    if include_shared and agent_ns:
        ns_filter = f"{agent_ns},{SHARED_NAMESPACE}"
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
        if output_format == "structured":
            return _format_structured_results([], hints=None)
        return f"No results found for agent '{agent_id or 'current'}'."

    if output_format == "structured":
        return _format_structured_results(results, hints=None)
    return _format_results(results, verbose=output_format == "verbose")


_SHARED_FROM_TAG_PREFIX = "shared-from="
_SHARED_TITLE_PREFIX = "Shared: "


def _build_shared_title(heading_hierarchy: tuple[str, ...] | list[str]) -> str:
    """Return the ``mem_agent_share`` copy's title.

    Strips any leading ``Shared: `` prefixes from each heading entry
    before re-prepending a single one. Without this, a chain of re-shares
    would produce ``Shared: Shared: Shared: ...`` because the source
    chunk's heading already includes the ``Shared: `` prefix from the
    previous share, and the naive ``f"Shared: {join}"`` doubles up on
    every hop. Mirrors the audit-tag chain in :func:`_build_shared_tags`
    which also collapses re-share history (immediate parent only).
    """

    if not heading_hierarchy:
        return f"{_SHARED_TITLE_PREFIX}memory"

    cleaned: list[str] = []
    for heading in heading_hierarchy:
        # Strip every leading ``Shared: `` so an N-hop chain — even one
        # that already accumulated to ``Shared: Shared: Cache ...`` under
        # the pre-fix code — collapses to a single prefix on the next
        # share.
        stripped = heading
        while stripped.startswith(_SHARED_TITLE_PREFIX):
            stripped = stripped[len(_SHARED_TITLE_PREFIX) :]
        cleaned.append(stripped)
    return f"{_SHARED_TITLE_PREFIX}{' > '.join(cleaned)}"


def _build_shared_tags(source_tags: tuple[str, ...] | list[str], source_chunk_id: str) -> list[str]:
    """Return the tag list to put on a ``mem_agent_share`` copy.

    Strips any inherited ``shared-from=...`` entries (so a chain of
    re-shares produces ``shared-from=<immediate-parent>`` only, not a
    growing audit chain) and appends a single ``shared-from=<source>``
    pointing at the immediate parent. Extracted as a top-level function
    so the dedup contract can be unit-tested without spinning up MCP
    components.
    """

    inherited = [t for t in source_tags if not t.startswith(_SHARED_FROM_TAG_PREFIX)]
    inherited.append(f"{_SHARED_FROM_TAG_PREFIX}{source_chunk_id}")
    return inherited


@mcp.tool()
@tool_handler
@register("multi_agent")
async def mem_agent_share(
    chunk_id: str,
    target: str = SHARED_NAMESPACE,
    ctx: CtxType = None,
) -> str:
    """Copy a memory chunk's content into another namespace.

    Despite the name, this performs a content **copy** into ``target``,
    not a reference link. The new chunk has a fresh UUID; deleting the
    source does not delete the copy and updating the source does not
    propagate. Source linkage is recorded only via a
    ``shared-from=<source-uuid>`` tag on the new chunk so audit tools
    can trace provenance. The function name is preserved for API
    stability — true cross-reference / link semantics (no duplication,
    bidirectional propagation) are tracked as a separate RFC follow-up.

    Tags from the source chunk are carried over verbatim, with one
    exception: any pre-existing ``shared-from=...`` tag is **dropped**
    so a chain of re-shares produces ``shared-from=<immediate-parent>``
    only, not a growing audit chain. Use the parent UUID to walk back
    one hop at a time if needed.

    ``target`` is run through :func:`validate_namespace` before any
    storage write — it is the same caller-supplied bypass shape that
    issue #496 closes on the session-start surfaces. Without the gate a
    caller could land a ``"shared:foo:bar"`` or ``"agent-runtime:foo:bar"``
    chunk_links row even though the equivalent ``mem_session_start`` and
    ``mem_agent_register`` paths refuse the same shape.

    Args:
        chunk_id: UUID of the chunk to copy
        target: Target namespace — ``'shared'`` or ``'agent-runtime:{agent_id}'``
    """
    from uuid import UUID

    validate_namespace(target)
    app = await _get_app_initialized(ctx)

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        return f"Chunk {chunk_id} not found."

    inherited_tags = _build_shared_tags(chunk.metadata.tags, chunk_id)

    from memtomem.server.tools.memory_crud import _mem_add_core

    result, stats = await _mem_add_core(
        content=chunk.content,
        title=_build_shared_title(chunk.metadata.heading_hierarchy),
        tags=inherited_tags,
        file=None,
        namespace=target,
        template=None,
        ctx=ctx,
    )

    # Record the structured link (PR-2 of the chunk_links series). The
    # markdown ``shared-from=`` audit tag is still written into content
    # + metadata.tags for humans and for the back-fill of pre-RFC DBs,
    # but structured queries (fanout / provenance walk / per-NS audit)
    # use ``chunk_links`` because that's what has a covering index.
    #
    # ``stats.new_chunk_ids`` holds the UUIDs of chunks freshly upserted
    # by this call. ``append_entry`` writes a single section so we
    # normally see exactly one new chunk and ``[0]`` is the section head
    # — the right representative for the share copy.
    #
    # Edge case the chunker can produce: ``_merge_short_chunks`` may
    # fold a freshly-appended short entry *into* the previous trailing
    # chunk of the daily file, in which case ``new_chunk_ids[0]`` is
    # the re-merged chunk (old+new content) rather than a pure share
    # copy. Same indexer behavior exposed by re-sharing into multiple
    # namespaces on the same day; the link writer inherits it.
    # Mitigating factors: the markdown ``shared-from=`` tag is still on
    # the content so humans / tag-filter search still find it, and a
    # future bump of ``_CHUNK_LINKS_BACKFILL_KEY`` (not done in this PR)
    # would let a migration widen / re-derive links if we ever need to.
    #
    # ``stats`` is ``None`` on the early-error paths of ``_mem_add_core``
    # (empty content, oversized, template failure) — those paths also
    # return an error message so the copy did not happen and there is
    # nothing to link.
    if stats is not None and stats.new_chunk_ids:
        try:
            await app.storage.add_chunk_link(
                source_id=uid,
                target_id=stats.new_chunk_ids[0],
                link_type="shared",
                namespace_target=target,
            )
        except Exception:
            # Link recording is best-effort — the copy itself is durable
            # via the markdown file and ``shared-from=`` tag, so a writer
            # failure here must not surface to the user or roll the copy
            # back. Log for diagnostic use.
            logger.warning("chunk_links writer failed for share copy", exc_info=True)
    elif stats is not None:
        logger.warning(
            "mem_agent_share: no new chunk UUID from mem_add — "
            "skipping chunk_links writer (content_hash collision?)"
        )

    return f"Shared to namespace '{target}'.\n{result}"
