"""Settings hooks sync status and conflict resolution."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    resolve_scope_path,
    _safe_load_json,
    _write_json,
    generate_all_settings,
)
from memtomem.context.settings_doctor import (
    DuplicateTier,
    detect_duplicate_tiers,
)
from memtomem.web.deps import get_hooks_target_scope, get_project_root
from memtomem.web.routes._locks import _gateway_lock

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings-sync", "context-gateway"])

_MALFORMED = object()


def _claude_target(project_root: Path, scope: str) -> Path:
    """Resolve the Claude Code settings file under *scope* (ADR-0010 §3).

    Thin wrapper over :func:`memtomem.context.settings.resolve_scope_path`
    so this module owns no path math of its own — keeps the route helper
    in lock-step with :class:`ClaudeSettingsGenerator` and the detector.
    """
    return resolve_scope_path(project_root, scope)


def _rule_label(event: str, matcher: str) -> str:
    """Human-readable label for a hook rule: ``event`` or ``event:matcher``."""
    return f"{event}:{matcher}" if matcher else event


def _serialize_duplicate_tiers(duplicates: list[DuplicateTier]) -> list[dict]:
    """Serialize :class:`DuplicateTier` results for the JSON response.

    Per ADR-0010 §4 the Web hooks panel surfaces this list as a
    read-only banner; the schema mirrors the CLI ``settings-doctor
    --json`` payload so both surfaces stay in lock-step.
    """
    return [
        {
            "tier": dup.tier,
            "path": str(dup.path),
            "entries": [
                {
                    "event": sig.event,
                    "matcher": sig.matcher,
                    "command_preview": sig.command_shape,
                }
                for sig in dup.entries
            ],
        }
        for dup in duplicates
    ]


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

    # Index target rules by (event, matcher) preserving multiplicity. Claude
    # Code allows the same matcher (or no matcher) to appear more than once
    # under one event, so collapsing to a single dict-of-rule lets a
    # byte-identical canonical contribution mismatch the *second* user rule
    # and surface as a false-positive conflict (mirrors the same fix on the
    # merge side in PR #844).
    target_index: dict[tuple[str, str], list[dict]] = {}
    for event, rules in target_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if isinstance(rule, dict):
                target_index.setdefault((event, rule.get("matcher", "")), []).append(rule)

    for event, rules in canonical_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = rule.get("matcher", "")
            key = (event, matcher)
            same_matcher = target_index.get(key, [])

            if same_matcher:
                if any(existing == rule for existing in same_matcher):
                    result["hooks"]["synced"].append(
                        {"event": event, "matcher": matcher, "rule": rule}
                    )
                else:
                    # Surface the first same-matcher rule as the conflict
                    # representative — keeps the API payload shape stable
                    # for the resolve flow while not silently shadowing the
                    # rest of the user's same-matcher rules.
                    result["hooks"]["conflicts"].append(
                        {
                            "event": event,
                            "matcher": matcher,
                            "existing": same_matcher[0],
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
    scope: str = Depends(get_hooks_target_scope),
) -> dict:
    """Return structured settings sync status with conflict details."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, scope)
    payload = _compare_hooks(canonical_path, target_path)
    # Surface the active scope so the Web UI can render a scope-accurate
    # target label (issue #962). Without this the panel falls back to a
    # hardcoded "User-scope target:" that lies when the scope is
    # project_shared or project_local.
    payload["target_scope"] = scope
    payload["duplicate_tier_warnings"] = _serialize_duplicate_tiers(
        detect_duplicate_tiers(project_root, active_scope=scope)
    )
    return payload


class ApplySettingsSyncRequest(BaseModel):
    """Body for ``POST /settings-sync``.

    ``allow_host_writes`` mirrors :func:`generate_all_settings`. The default
    (``False``) makes the route refuse writes outside the project root so a
    UI built on top must surface the host paths and re-post with the flag
    set to true after the user confirms — the same gate the CLI confirms
    interactively.
    """

    allow_host_writes: bool = False


@router.post("/settings-sync")
@router.post("/context/settings/sync")
async def apply_settings_sync(
    body: ApplySettingsSyncRequest | None = None,
    project_root: Path = Depends(get_project_root),
    scope: str = Depends(get_hooks_target_scope),
) -> dict:
    """Run the full settings merge (generate_all_settings).

    Default ``allow_host_writes=False`` returns ``needs_confirmation`` for
    any generator whose target lives outside the project root. The UI is
    expected to display those targets and re-post with
    ``{"allow_host_writes": true}`` once the user has confirmed.
    """
    allow_host_writes = body.allow_host_writes if body else False
    # Detect duplicate-tier hooks BEFORE the merge so the warning
    # reflects pre-write state (ADR-0010 §4: the warning fires in the
    # user's actual workflow, not behind a separate command).
    duplicates = detect_duplicate_tiers(project_root, active_scope=scope)
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                results = generate_all_settings(
                    project_root,
                    scope=scope,
                    allow_host_writes=allow_host_writes,
                )
    except TimeoutError:
        raise HTTPException(503, "Settings sync timed out — another sync may be in progress")
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
    return {
        "results": out,
        "duplicate_tier_warnings": _serialize_duplicate_tiers(duplicates),
    }


class ResolveRequest(BaseModel):
    event: str
    matcher: str = ""
    action: str = "use_proposed"


@router.post("/settings-sync/resolve")
@router.post("/context/settings/resolve")
async def resolve_conflict(
    body: ResolveRequest,
    project_root: Path = Depends(get_project_root),
    scope: str = Depends(get_hooks_target_scope),
) -> dict:
    """Resolve a single hook conflict by replacing the target's rule."""
    if body.action != "use_proposed":
        raise HTTPException(400, detail=f"Unknown action: {body.action}")

    label = _rule_label(body.event, body.matcher)

    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, scope)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Read canonical rule
                if not canonical_path.is_file():
                    raise HTTPException(404, detail="Canonical source does not exist")
                canonical = _safe_load_json(canonical_path)
                if not isinstance(canonical, dict):
                    raise HTTPException(422, detail="Canonical source is not valid JSON")

                proposed = None
                canonical_hooks: dict = canonical.get("hooks", {})
                for rule in canonical_hooks.get(body.event, []):
                    if isinstance(rule, dict) and rule.get("matcher", "") == body.matcher:
                        proposed = rule
                        break
                if proposed is None:
                    raise HTTPException(404, detail=f"Rule '{label}' not in canonical source")

                # Read target + mtime guard. ``st_mtime_ns`` matches
                # ``hot_reload.py`` precision and detects sub-second writes
                # that ``st_mtime`` (float seconds) misses.
                if not target_path.is_file():
                    raise HTTPException(404, detail="Target settings file does not exist")

                mtime_ns = target_path.stat().st_mtime_ns
                target = _safe_load_json(target_path)
                if not isinstance(target, dict):
                    raise HTTPException(422, detail="Target settings is not valid JSON")

                # Replace the rule in-place
                target_hooks: dict = target.get("hooks", {})
                if not isinstance(target_hooks, dict):
                    raise HTTPException(422, detail="Target hooks is not a record")

                rules = target_hooks.get(body.event, [])
                replaced = False
                for i, rule in enumerate(rules):
                    if isinstance(rule, dict) and rule.get("matcher", "") == body.matcher:
                        rules[i] = proposed
                        replaced = True
                        break

                if not replaced:
                    raise HTTPException(404, detail=f"Rule '{label}' not found in target")

                # mtime check before write — protects against cross-process
                # writers (CLI, manual edit) that the in-process lock can't see.
                # Echo the current ``mtime_ns`` on abort so clients can refresh
                # local state without an extra round-trip — matches the
                # Skills/Commands/Agents 409 envelope contract.
                current_mtime_ns = target_path.stat().st_mtime_ns
                if current_mtime_ns != mtime_ns:
                    return {
                        "status": "aborted",
                        "reason": "Target file was modified by another process. Retry.",
                        "mtime_ns": str(current_mtime_ns),
                    }

                target_hooks[body.event] = rules
                target["hooks"] = target_hooks
                _write_json(target_path, target)
                return {
                    "status": "ok",
                    "reason": f"Rule '{label}' replaced with memtomem's version",
                }
    except TimeoutError:
        raise HTTPException(503, "Resolve timed out — another sync may be in progress")
