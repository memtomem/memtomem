"""Tools: schedule_register, schedule_list, schedule_run_now, schedule_delete.

P2 Phase A user-facing surface for the cron scheduler. Direct-cron only —
the natural-language ``spec="…"`` field arrives in Phase B. Each tool is
routed via ``mem_do`` (category="schedule") and also exposed individually
when ``MEMTOMEM_TOOL_MODE=standard``.

Return shapes follow ``feedback_cli_json_read_vs_write_shape``:
write actions return ``{ok, reason, ...}`` (always serialized JSON for
the MCP layer); read actions return either ``{schedules: [...]}`` or
``{error: ...}``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from croniter import croniter

from memtomem.scheduler.jobs import JOB_KINDS
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


def _run_now_timeout(app) -> float:
    """Resolve the per-run timeout, mirroring the dispatcher exactly.

    ``schedule_run_now`` and the watchdog dispatcher (PR-A3) must agree
    on the timeout — otherwise the same schedule fires for 60s under
    ``mm schedule run-now`` but 300s under auto-dispatch (review
    feedback PR #522). When the scheduler block is unset (callers
    constructing ``Mem2MemConfig()`` without overrides), fall back to
    ``SchedulerConfig().runner_timeout_seconds`` so the default tracks
    whatever PR-A3 ships.
    """
    cfg = getattr(app, "config", None)
    sched_cfg = getattr(cfg, "scheduler", None) if cfg is not None else None
    if sched_cfg is not None:
        return float(sched_cfg.runner_timeout_seconds)
    from memtomem.config import SchedulerConfig

    return float(SchedulerConfig().runner_timeout_seconds)


@mcp.tool()
@tool_handler
@register("schedule")
async def mem_schedule_register(
    cron: str,
    job_kind: str,
    params: dict | None = None,
    ctx: CtxType = None,
) -> str:
    """Register a cron-scheduled job (direct-cron mode).

    Args:
        cron: Five-field cron expression interpreted in UTC (e.g. ``"0 3 * * 0"``).
        job_kind: One of the registered ``JOB_KINDS`` keys (e.g. ``"compaction"``).
        params: Optional dict of parameters validated against the job's
            ``params_model`` before storage.
    """
    if not croniter.is_valid(cron):
        return json.dumps({"ok": False, "reason": f"invalid cron expression: {cron!r}"})
    spec = JOB_KINDS.get(job_kind)
    if spec is None:
        kinds = ", ".join(sorted(JOB_KINDS))
        return json.dumps(
            {"ok": False, "reason": f"unknown job_kind {job_kind!r}; available: {kinds}"}
        )
    try:
        spec.params_model.model_validate(params or {})
    except Exception as exc:
        return json.dumps({"ok": False, "reason": f"invalid params: {exc}"})

    app = await _get_app_initialized(ctx)
    sched_id = await app.storage.schedule_insert(cron, job_kind, params or {})
    return json.dumps({"ok": True, "reason": "registered", "id": sched_id})


@mcp.tool()
@tool_handler
@register("schedule")
async def mem_schedule_list(ctx: CtxType = None) -> str:
    """List all registered schedules with last-run status."""
    app = await _get_app_initialized(ctx)
    rows = await app.storage.schedule_list_all()
    return json.dumps({"schedules": rows})


@mcp.tool()
@tool_handler
@register("schedule")
async def mem_schedule_run_now(id: str, ctx: CtxType = None) -> str:
    """Run a registered schedule synchronously (out-of-band).

    Routes through the same ``JOB_KINDS[...].runner`` path as the
    dispatcher with the same per-job timeout, and records the outcome
    via ``schedule_mark_run`` so the next ``schedule_list`` reflects it.

    Args:
        id: Schedule id returned by ``schedule_register``.
    """
    app = await _get_app_initialized(ctx)
    sched = await app.storage.schedule_get(id)
    if sched is None:
        return json.dumps({"ok": False, "reason": f"schedule {id!r} not found"})

    spec = JOB_KINDS.get(sched["job_kind"])
    if spec is None:
        reason = f"unknown job_kind: {sched['job_kind']}"
        await app.storage.schedule_mark_run(id, "error", error=reason)
        return json.dumps({"ok": False, "reason": reason})

    try:
        validated = spec.params_model.model_validate(sched.get("params") or {})
    except Exception as exc:
        reason = f"invalid params: {exc}"
        await app.storage.schedule_mark_run(id, "error", error=reason)
        return json.dumps({"ok": False, "reason": reason})

    timeout = _run_now_timeout(app)

    # Race note: ``run_now`` and the watchdog dispatcher can both fire
    # the same schedule (different ticks). The current JOB_KINDS are
    # idempotent maintenance jobs so this is a known, accepted limit
    # for Phase A; non-idempotent jobs added later (e.g.
    # ``embedding_rebuild``) need a per-schedule lock here. Tracked for
    # Phase C alongside disable/enable.
    try:
        result = await asyncio.wait_for(
            spec.runner(app, **validated.model_dump()),
            timeout=timeout,
        )
        await app.storage.schedule_mark_run(id, "ok")
        return json.dumps({"ok": True, "reason": "ran", "result": result})
    except asyncio.TimeoutError:
        reason = f"exceeded {timeout}s"
        await app.storage.schedule_mark_run(id, "timeout", error=reason)
        return json.dumps({"ok": False, "reason": f"timeout: {reason}"})
    except Exception as exc:
        await app.storage.schedule_mark_run(id, "error", error=str(exc))
        return json.dumps({"ok": False, "reason": f"runner failed: {exc}"})


@mcp.tool()
@tool_handler
@register("schedule")
async def mem_schedule_delete(id: str, ctx: CtxType = None) -> str:
    """Delete a registered schedule by id.

    Paired with ``schedule_register`` so a bad cron entry can be removed
    without editing SQLite directly. ``disable``/``enable`` are deferred
    to Phase C.

    Args:
        id: Schedule id to delete.
    """
    app = await _get_app_initialized(ctx)
    deleted = await app.storage.schedule_delete(id)
    if not deleted:
        return json.dumps({"ok": False, "reason": f"schedule {id!r} not found"})
    return json.dumps({"ok": True, "reason": "deleted"})
