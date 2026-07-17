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
    """List effective Pinned Context blocks after scope and agent shadowing."""
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
    """Read one exact Pinned Context block."""
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
    """Create or replace a Pinned Context block after privacy and scope gates."""
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
    """Delete one exact Pinned Context block."""
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
