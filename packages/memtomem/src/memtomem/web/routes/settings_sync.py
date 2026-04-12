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


def _compare_hooks(
    canonical_path: Path,
    target_path: Path,
) -> dict:
    """Compare hooks between canonical and target settings files."""
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

    if not target_path.is_file():
        # All canonical hooks are pending
        for hook in canonical.get("hooks", []):
            if isinstance(hook, dict) and hook.get("name"):
                result["hooks"]["pending"].append({"name": hook["name"], "hook": hook})
        result["status"] = "out_of_sync" if result["hooks"]["pending"] else "in_sync"
        return result

    target = _safe_load_json(target_path)
    if not isinstance(target, dict):
        result["status"] = "error"
        result["error"] = f"{target_path} is not valid JSON"
        return result

    # Index target hooks by name
    target_by_name: dict[str, dict] = {}
    for hook in target.get("hooks", []):
        if isinstance(hook, dict) and hook.get("name"):
            target_by_name[hook["name"]] = hook

    canonical_hooks = canonical.get("hooks", [])
    for hook in canonical_hooks:
        if not isinstance(hook, dict):
            continue
        name = hook.get("name", "")
        if not name:
            continue

        if name in target_by_name:
            if target_by_name[name] == hook:
                result["hooks"]["synced"].append({"name": name, "hook": hook})
            else:
                result["hooks"]["conflicts"].append(
                    {
                        "name": name,
                        "existing": target_by_name[name],
                        "proposed": hook,
                    }
                )
        else:
            result["hooks"]["pending"].append({"name": name, "hook": hook})

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
    hook_name: str
    action: str = "use_proposed"


@router.post("/settings-sync/resolve")
@router.post("/context/settings/resolve")
async def resolve_conflict(
    body: ResolveRequest,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Resolve a single hook conflict by replacing the target's hook."""
    if body.action != "use_proposed":
        return {"status": "error", "reason": f"Unknown action: {body.action}"}

    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target()

    # Read canonical hook
    canonical = _safe_load_json(canonical_path)
    if not isinstance(canonical, dict):
        return {"status": "error", "reason": "Canonical source is not valid JSON"}

    proposed = None
    for hook in canonical.get("hooks", []):
        if isinstance(hook, dict) and hook.get("name") == body.hook_name:
            proposed = hook
            break
    if proposed is None:
        return {"status": "error", "reason": f"Hook '{body.hook_name}' not in canonical source"}

    # Read target + mtime guard
    if not target_path.is_file():
        return {"status": "error", "reason": "Target settings file does not exist"}

    mtime = target_path.stat().st_mtime
    target = _safe_load_json(target_path)
    if not isinstance(target, dict):
        return {"status": "error", "reason": "Target settings is not valid JSON"}

    # Replace the hook in-place
    hooks = target.get("hooks", [])
    replaced = False
    for i, hook in enumerate(hooks):
        if isinstance(hook, dict) and hook.get("name") == body.hook_name:
            hooks[i] = proposed
            replaced = True
            break

    if not replaced:
        return {"status": "error", "reason": f"Hook '{body.hook_name}' not found in target"}

    # mtime check before write
    if target_path.stat().st_mtime != mtime:
        return {
            "status": "aborted",
            "reason": "Target file was modified by another process. Retry.",
        }

    target["hooks"] = hooks
    _write_json(target_path, target)
    return {"status": "ok", "reason": f"Hook '{body.hook_name}' replaced with memtomem's version"}
