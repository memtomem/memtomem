"""Review-first memory formation actions."""

from __future__ import annotations

import json

from memtomem.formation import scan_session_candidates
from memtomem.pinned import PinnedContextStore
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("formation")
async def mem_formation_scan(session_id: str, ctx: CtxType = None) -> str:
    """Generate review candidates from one exact session's events; writes no long-term memory."""
    app = await _get_app_initialized(ctx)
    candidates = await scan_session_candidates(app.storage, session_id)
    return json.dumps({"created": len(candidates), "candidates": candidates})


@mcp.tool()
@tool_handler
@register("formation")
async def mem_candidate_list(status: str = "pending", limit: int = 100, ctx: CtxType = None) -> str:
    """List memory-formation review candidates."""
    app = await _get_app_initialized(ctx)
    return json.dumps(await app.storage.list_memory_candidates(status=status, limit=limit))


@mcp.tool()
@tool_handler
@register("formation")
async def mem_candidate_review(
    candidate_id: str,
    decision: str,
    reviewer: str = "user",
    reason: str = "",
    ctx: CtxType = None,
) -> str:
    """Approve or reject a candidate; only approval writes durable memory."""
    app = await _get_app_initialized(ctx)
    candidate = await app.storage.get_memory_candidate(candidate_id)
    if candidate is None or candidate["status"] != "pending":
        return json.dumps({"ok": False, "reason": "candidate not pending"})
    if decision == "reject":
        changed = await app.storage.decide_memory_candidate(
            candidate_id, "rejected", reviewer, reason
        )
        return json.dumps({"ok": changed, "status": "rejected"})
    if decision != "approve":
        return json.dumps({"ok": False, "reason": "decision must be approve or reject"})

    if candidate["destination"] == "pinned":
        from memtomem.server.tools.search import _resolve_project_context_root

        store = PinnedContextStore(app.config, project_root=_resolve_project_context_root(app))
        block = store.set(
            f"candidate-{candidate_id[:8]}",
            candidate["content"],
            description=f"Approved {candidate['kind']} candidate",
        )
        write_result: object = block.as_dict()
    else:
        from memtomem.server.tools.memory_crud import mem_add

        write_result = await mem_add(
            content=candidate["content"],
            title=f"Approved {candidate['kind']}",
            tags=["formation-approved", candidate["kind"]],
            ctx=ctx,
        )
        if isinstance(write_result, str) and (
            "blocked" in write_result.lower() or "error" in write_result.lower()
        ):
            return json.dumps({"ok": False, "reason": write_result})
    changed = await app.storage.decide_memory_candidate(candidate_id, "approved", reviewer, reason)
    return json.dumps({"ok": changed, "status": "approved", "write": write_result})
