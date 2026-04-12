"""Context gateway overview — aggregate sync status across all artifact types."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends

from memtomem.web.deps import get_project_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-gateway"])


def _count_statuses(triples: list[tuple[str, str, str]]) -> dict:
    """Summarise ``(runtime, name, status)`` triples into per-status counts."""
    names: set[str] = set()
    counts: dict[str, int] = {}
    for _runtime, name, status in triples:
        names.add(name)
        key = status.replace(" ", "_")
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(names), **counts}


@router.get("/context/overview")
async def context_overview(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Aggregate sync status across skills, commands, agents, and settings."""
    from memtomem.context.agents import diff_agents
    from memtomem.context.commands import diff_commands
    from memtomem.context.settings import diff_settings
    from memtomem.context.skills import diff_skills

    result: dict = {}

    try:
        result["skills"] = _count_statuses(diff_skills(project_root))
    except Exception:
        logger.exception("diff_skills failed")
        result["skills"] = {"total": 0, "error": True}

    try:
        result["commands"] = _count_statuses(diff_commands(project_root))
    except Exception:
        logger.exception("diff_commands failed")
        result["commands"] = {"total": 0, "error": True}

    try:
        result["agents"] = _count_statuses(diff_agents(project_root))
    except Exception:
        logger.exception("diff_agents failed")
        result["agents"] = {"total": 0, "error": True}

    try:
        settings_diff = diff_settings(project_root)
        statuses = [r.status for r in settings_diff.values()]
        if all(s in ("in sync", "skipped") for s in statuses):
            result["settings"] = {"status": "in_sync"}
        elif any(s == "error" for s in statuses):
            result["settings"] = {"status": "error"}
        else:
            result["settings"] = {"status": "out_of_sync"}
    except Exception:
        logger.exception("diff_settings failed")
        result["settings"] = {"status": "error"}

    return result
