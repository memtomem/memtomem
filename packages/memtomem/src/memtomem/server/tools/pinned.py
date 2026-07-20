"""Pinned Context actions routed through ``mem_do`` in core mode."""

from __future__ import annotations

import json

from memtomem.config import TargetScope
from memtomem.pinned import ContextAssembler, PinnedContextStore
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


async def _store(ctx: CtxType):
    from memtomem.server.tools.search import _resolve_project_context_root

    app = await _get_app_initialized(ctx)
    return app, PinnedContextStore(app.config, project_root=_resolve_project_context_root(app))


@mcp.tool()
@tool_handler
@register("pinned")
async def mem_pinned_list(agent_id: str | None = None, ctx: CtxType = None) -> str:
    """List effective Pinned Context blocks after scope and agent shadowing.

    Returns the winner per block_id, not every stored copy: an agent-specific
    block shadows the general one, and a narrower scope shadows a wider one.
    Results are ordered by descending priority, then block_id.

    Args:
        agent_id: Include blocks pinned to this agent alongside the general
            ones, and let them shadow same-id general blocks. Omit to see the
            general blocks only.
    """
    _, store = await _store(ctx)
    return json.dumps([block.as_dict() for block in store.list(agent_id=agent_id)])


@mcp.tool()
@tool_handler
@register("pinned")
async def mem_pinned_get(
    block_id: str,
    scope: TargetScope = "user",
    agent_id: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Read one exact Pinned Context block.

    Exact lookup, no shadowing: the block is read from the given scope and
    agent slot, and missing returns ``null`` rather than falling back to a
    wider scope. Use ``mem_pinned_list`` for the effective view.

    Args:
        block_id: Block identifier (the filename stem under the pinned store).
        scope: ADR-0011 residency tier — ``user`` (default),
            ``project_shared``, or ``project_local``.
        agent_id: Read the agent-scoped block instead of the general one.
    """
    _, store = await _store(ctx)
    block = store.get(block_id, scope=scope, agent_id=agent_id)
    return json.dumps(block.as_dict() if block else None)


@mcp.tool()
@tool_handler
@register("pinned")
async def mem_pinned_set(
    block_id: str,
    content: str,
    scope: TargetScope = "user",
    agent_id: str | None = None,
    description: str = "",
    priority: int = 0,
    confirm_project_shared: bool = False,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Create or replace a Pinned Context block after privacy and scope gates.

    Writes are whole-block replacements — the block at ``(scope, agent_id,
    block_id)`` is overwritten, not merged.

    Args:
        block_id: Block identifier (becomes the filename stem).
        content: Block body. Rejected above 2,000 characters — pinned context
            is a bounded prelude to retrieval, not a place to park documents.
        scope: ADR-0011 residency tier — ``user`` (default),
            ``project_shared``, or ``project_local``.
        agent_id: Pin the block to one agent. Agent blocks shadow the general
            block of the same id in ``mem_pinned_list``.
        description: Short human-facing label stored in the block frontmatter.
        priority: Ordering weight; higher sorts first in the effective list.
        confirm_project_shared: Required consent for a ``project_shared``
            write, which lands in the git-tracked tree. Without it the call
            is refused.
        force_unsafe: Bypass the redaction guard when the content matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line. It does NOT apply to
            ``scope="project_shared"``: that combination is hard-refused,
            because git history cannot be retracted from clones.
    """
    _, store = await _store(ctx)
    block = store.set(
        block_id,
        content,
        scope=scope,
        agent_id=agent_id,
        description=description,
        priority=priority,
        confirm_project_shared=confirm_project_shared,
        force_unsafe=force_unsafe,
    )
    return json.dumps(block.as_dict())


@mcp.tool()
@tool_handler
@register("pinned")
async def mem_pinned_delete(
    block_id: str,
    scope: TargetScope = "user",
    agent_id: str | None = None,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Delete one exact Pinned Context block.

    Exact deletion, no shadowing: only the block in the given scope and agent
    slot is removed, so deleting an agent block re-exposes the general one.
    Returns ``{"deleted": false}`` when nothing was there.

    Args:
        block_id: Block identifier.
        scope: ADR-0011 residency tier — ``user`` (default),
            ``project_shared``, or ``project_local``.
        agent_id: Delete the agent-scoped block instead of the general one.
        confirm_project_shared: Required consent for deleting from the
            git-tracked ``project_shared`` tree.
    """
    _, store = await _store(ctx)
    return json.dumps(
        {
            "deleted": store.delete(
                block_id,
                scope=scope,
                agent_id=agent_id,
                confirm_project_shared=confirm_project_shared,
            )
        }
    )


@mcp.tool()
@tool_handler
@register("pinned")
async def mem_context_compose(
    query: str | None = None,
    agent_id: str | None = None,
    max_chars: int = 12_000,
    top_k: int = 10,
    namespace: str | list[str] | None = None,
    context_window: int | None = None,
    rerank: bool | None = None,
    ctx: CtxType = None,
) -> str:
    """Compose Pinned Context first, then fill the remaining budget by retrieval.

    Args:
        query: Retrieval query for the remaining budget (omit for pinned-only)
        agent_id: Restrict pinned blocks to this agent
        max_chars: Total character budget for the bundle (default 12,000)
        top_k: Number of retrieval results to consider (default 10)
        namespace: Namespace scope for retrieval
        context_window: Expand each retrieved hit with ±N adjacent chunks
        rerank: Per-call rerank control for the retrieval leg. ``false`` = skip
            the cross-encoder rerank stage — the fast path for latency-bounded
            callers. Omitted/``true`` = follow server config (``rerank.enabled``);
            ``true`` cannot enable reranking when the server has it disabled.

    The bundle names the retrieval leg's score scale at the top level
    (``score_scale``: ``rrf``/``bm25``/``dense``/``none``/``rerank``, plus
    ``reranker`` with the model ID when reranked); both keys are omitted when
    ``retrieved`` is empty. Pinned blocks carry no relevance scale.
    """
    app, store = await _store(ctx)
    bundle = await ContextAssembler(store, app.search_pipeline).compose(
        query,
        agent_id=agent_id,
        max_chars=max_chars,
        top_k=top_k,
        namespace=namespace,
        context_window=context_window,
        rerank=rerank,
    )
    return json.dumps(bundle.as_dict())
