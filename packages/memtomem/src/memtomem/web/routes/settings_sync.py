"""Settings hooks sync status and conflict resolution."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    _safe_load_json,
    _write_json,
    generate_all_settings,
)
from memtomem.web.deps import get_project_root

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings-sync", "context-gateway"])

_MALFORMED = object()


def _claude_target() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _rule_label(event: str, matcher: str) -> str:
    """Human-readable label for a hook rule: ``event`` or ``event:matcher``."""
    return f"{event}:{matcher}" if matcher else event


def _compare_hooks(
    canonical_path: Path,
    target_path: Path,
) -> dict:
    """Compare record-format hooks between canonical and target settings."""
    result: dict = {
        "canonical_path": str(canonical_path),
        "target_path": str(target_path),
        "hooks": {"synced": [], "conflicts": [], "pending": []},
    }

    if not canonical_path.is_file():
        result["status"] = "no_source"
        return result

    canonical = _safe_load_json(canonical_path)
    if not isinstance(canonical, dict):
        result["status"] = "error"
        result["error"] = f"{canonical_path} is not valid JSON"
        return result

    canonical_hooks: dict = canonical.get("hooks", {})
    if not isinstance(canonical_hooks, dict):
        result["status"] = "error"
        result["error"] = "hooks must be a record (object), not an array"
        return result

    if not target_path.is_file():
        # All canonical rules are pending
        for event, rules in canonical_hooks.items():
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if isinstance(rule, dict):
                    matcher = rule.get("matcher", "")
                    result["hooks"]["pending"].append(
                        {"event": event, "matcher": matcher, "rule": rule}
                    )
        result["status"] = "out_of_sync" if result["hooks"]["pending"] else "in_sync"
        return result

    target = _safe_load_json(target_path)
    if not isinstance(target, dict):
        result["status"] = "error"
        result["error"] = f"{target_path} is not valid JSON"
        return result

    target_hooks: dict = target.get("hooks", {})
    if not isinstance(target_hooks, dict):
        target_hooks = {}

    # Index target rules by (event, matcher)
    target_index: dict[tuple[str, str], dict] = {}
    for event, rules in target_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if isinstance(rule, dict):
                target_index[(event, rule.get("matcher", ""))] = rule

    for event, rules in canonical_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = rule.get("matcher", "")
            key = (event, matcher)

            if key in target_index:
                if target_index[key] == rule:
                    result["hooks"]["synced"].append(
                        {"event": event, "matcher": matcher, "rule": rule}
                    )
                else:
                    result["hooks"]["conflicts"].append(
                        {
                            "event": event,
                            "matcher": matcher,
                            "existing": target_index[key],
                            "proposed": rule,
                        }
                    )
            else:
                result["hooks"]["pending"].append(
                    {"event": event, "matcher": matcher, "rule": rule}
                )

    if result["hooks"]["conflicts"]:
        result["status"] = "conflicts"
    elif result["hooks"]["pending"]:
        result["status"] = "out_of_sync"
    else:
        result["status"] = "in_sync"

    return result


@router.get("/settings-sync")
@router.get("/context/settings")
async def get_settings_sync(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Return structured settings sync status with conflict details."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target()
    return _compare_hooks(canonical_path, target_path)


@router.post("/settings-sync")
@router.post("/context/settings/sync")
async def apply_settings_sync(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Run the full settings merge (generate_all_settings)."""
    results = generate_all_settings(project_root)
    out: list[dict] = []
    for name, r in results.items():
        out.append(
            {
                "name": name,
                "status": r.status,
                "reason": r.reason,
                "warnings": r.warnings,
                "target": str(r.target) if r.target else None,
            }
        )
    return {"results": out}


class ResolveRequest(BaseModel):
    event: str
    matcher: str = ""
    action: str = "use_proposed"


@router.post("/settings-sync/resolve")
@router.post("/context/settings/resolve")
async def resolve_conflict(
    body: ResolveRequest,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Resolve a single hook conflict by replacing the target's rule."""
    if body.action != "use_proposed":
        return {"status": "error", "reason": f"Unknown action: {body.action}"}

    label = _rule_label(body.event, body.matcher)

    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target()

    # Read canonical rule
    canonical = _safe_load_json(canonical_path)
    if not isinstance(canonical, dict):
        return {"status": "error", "reason": "Canonical source is not valid JSON"}

    proposed = None
    canonical_hooks: dict = canonical.get("hooks", {})
    for rule in canonical_hooks.get(body.event, []):
        if isinstance(rule, dict) and rule.get("matcher", "") == body.matcher:
            proposed = rule
            break
    if proposed is None:
        return {"status": "error", "reason": f"Rule '{label}' not in canonical source"}

    # Read target + mtime guard
    if not target_path.is_file():
        return {"status": "error", "reason": "Target settings file does not exist"}

    mtime = target_path.stat().st_mtime
    target = _safe_load_json(target_path)
    if not isinstance(target, dict):
        return {"status": "error", "reason": "Target settings is not valid JSON"}

    # Replace the rule in-place
    target_hooks: dict = target.get("hooks", {})
    if not isinstance(target_hooks, dict):
        return {"status": "error", "reason": "Target hooks is not a record"}

    rules = target_hooks.get(body.event, [])
    replaced = False
    for i, rule in enumerate(rules):
        if isinstance(rule, dict) and rule.get("matcher", "") == body.matcher:
            rules[i] = proposed
            replaced = True
            break

    if not replaced:
        return {"status": "error", "reason": f"Rule '{label}' not found in target"}

    # mtime check before write
    if target_path.stat().st_mtime != mtime:
        return {
            "status": "aborted",
            "reason": "Target file was modified by another process. Retry.",
        }

    target_hooks[body.event] = rules
    target["hooks"] = target_hooks
    _write_json(target_path, target)
    return {"status": "ok", "reason": f"Rule '{label}' replaced with memtomem's version"}
