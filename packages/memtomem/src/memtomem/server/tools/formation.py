"""Review-first memory formation actions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from memtomem.formation import DEFAULT_STALE_CLAIM_MINUTES, scan_session_candidates
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
    return json.dumps({"created": len(candidates), "candidates": candidates}, ensure_ascii=False)


@mcp.tool()
@tool_handler
@register("formation")
async def mem_candidate_list(status: str = "pending", limit: int = 100, ctx: CtxType = None) -> str:
    """List memory-formation review candidates."""
    app = await _get_app_initialized(ctx)
    return json.dumps(
        await app.storage.list_memory_candidates(status=status, limit=limit),
        ensure_ascii=False,
    )


@mcp.tool()
@tool_handler
@register("formation")
async def mem_candidate_recover(
    stale_after_minutes: int = DEFAULT_STALE_CLAIM_MINUTES,
    limit: int = 100,
    actor: str = "mcp-operator",
    ctx: CtxType = None,
) -> str:
    """Return stale interrupted approval claims to the pending queue."""
    if not 1 <= stale_after_minutes <= 1440:
        return json.dumps({"ok": False, "reason": "stale_after_minutes must be between 1 and 1440"})
    if not 1 <= limit <= 1000:
        return json.dumps({"ok": False, "reason": "limit must be between 1 and 1000"})
    if not actor.strip():
        return json.dumps({"ok": False, "reason": "actor cannot be empty"})
    app = await _get_app_initialized(ctx)
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    recovered = await app.storage.recover_stale_memory_candidates(
        stale_before=stale_before.isoformat(timespec="seconds"),
        actor=actor,
        limit=limit,
    )
    return json.dumps(
        {
            "ok": True,
            "recovered": len(recovered),
            "candidate_ids": recovered,
            "stale_before": stale_before.isoformat(timespec="seconds"),
        }
    )


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
    if candidate is None:
        return json.dumps({"ok": False, "reason": "candidate not found"})
    if decision == "reject":
        if candidate["status"] == "write_uncertain":
            if not reviewer.strip():
                return json.dumps({"ok": False, "reason": "reviewer cannot be empty"})
            if not reason.strip():
                return json.dumps(
                    {
                        "ok": False,
                        "reason": (
                            "resolving write_uncertain requires a reason after "
                            "inspecting the durable destination"
                        ),
                    }
                )
            changed = await app.storage.resolve_uncertain_memory_candidate(
                candidate_id, reviewer=reviewer, reason=reason
            )
            return json.dumps(
                {
                    "ok": changed,
                    "status": "rejected" if changed else "state_changed",
                    "resolved_from": "write_uncertain",
                }
            )
        if candidate["status"] != "pending":
            return json.dumps(
                {"ok": False, "reason": f"candidate not pending ({candidate['status']})"}
            )
        changed = await app.storage.decide_memory_candidate(
            candidate_id, "rejected", reviewer, reason
        )
        return json.dumps({"ok": changed, "status": "rejected"})
    if decision != "approve":
        return json.dumps({"ok": False, "reason": "decision must be approve or reject"})
    if candidate["status"] != "pending":
        return json.dumps({"ok": False, "reason": f"candidate not pending ({candidate['status']})"})

    claimed = await app.storage.claim_memory_candidate(candidate_id, reviewer, reason)
    if claimed is None:
        return json.dumps({"ok": False, "reason": "candidate state changed concurrently"})
    try:
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
            from memtomem.server.tools.memory_crud import _mem_add_core

            message, stats = await _mem_add_core(
                content=candidate["content"],
                title=f"Approved {candidate['kind']}",
                tags=["formation-approved", candidate["kind"]],
                file=None,
                namespace=None,
                template=None,
                ctx=ctx,
            )
            if stats is None:
                await app.storage.release_memory_candidate(candidate_id)
                return json.dumps({"ok": False, "reason": message}, ensure_ascii=False)
            write_result = {
                "message": message,
                "new_chunk_ids": [str(chunk_id) for chunk_id in stats.new_chunk_ids],
            }
    except asyncio.CancelledError:
        await app.storage.release_memory_candidate(candidate_id)
        raise
    except Exception:
        await app.storage.release_memory_candidate(candidate_id)
        raise
    changed = await app.storage.finalize_memory_candidate(candidate_id)
    if not changed:
        warning = (
            "Durable write completed, but the approval claim was recovered concurrently. "
            "The write already persists; inspect the returned write details before taking "
            "further action and do not re-approve this candidate."
        )
        quarantined = await app.storage.mark_memory_candidate_write_uncertain(
            candidate_id, actor="mcp-finalizer", reason=warning
        )
        return json.dumps(
            {
                "ok": False,
                "status": "write_uncertain" if quarantined else "state_changed",
                "durable_write_persisted": True,
                "reason": warning,
                "write": write_result,
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {"ok": True, "status": "approved", "write": write_result},
        ensure_ascii=False,
    )
