"""Settings hooks sync status and conflict resolution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from memtomem.config import TargetScope
from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    resolve_scope_path,
    _rule_content_equal,
    _rule_is_memtomem_owned,
    _safe_load_json,
    _stamp_status_markers,
    _write_json,
    generate_all_settings,
)
from memtomem.context.settings_doctor import (
    DuplicateTier,
    detect_duplicate_tiers,
)
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_projects import resolve_scope_root

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


def _rule_hash(rule: dict) -> str:
    """Stable hash for one Claude hooks rule payload."""
    payload = json.dumps(rule, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mtime_ns(path: Path) -> str | None:
    """Return an mtime token safe for JSON clients, or ``None`` if missing."""
    return str(path.stat().st_mtime_ns) if path.is_file() else None


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


def _has_hook_rules(hooks_record: dict) -> bool:
    """True when the canonical hooks record contains at least one rule."""
    for rules in hooks_record.values():
        if not isinstance(rules, list):
            continue
        if any(isinstance(rule, dict) for rule in rules):
            return True
    return False


def _iter_hook_rules(hooks_record: dict) -> list[dict]:
    """Flatten a record-format hooks object into serializable rule rows."""
    rows: list[dict] = []
    for event, rules in hooks_record.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            continue
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            rows.append(
                {
                    "event": event,
                    "matcher": rule.get("matcher", ""),
                    "rule_index": index,
                    "rule_hash": _rule_hash(rule),
                    "rule": rule,
                }
            )
    return rows


def _compare_hooks(
    canonical_path: Path,
    target_path: Path,
) -> dict:
    """Compare record-format hooks between canonical and target settings.

    This display/diff path still compares by ``(event, matcher)`` for the
    existing conflict UI. Rule mutation endpoints below use
    ``rule_index`` + ``rule_hash`` exact matching instead, so duplicate
    same-matcher rows are edited/deleted by identity rather than by label.
    """
    result: dict = {
        "canonical_path": str(canonical_path),
        "target_path": str(target_path),
        "canonical_mtime_ns": _mtime_ns(canonical_path),
        "target_mtime_ns": _mtime_ns(target_path),
        "hooks": {"synced": [], "conflicts": [], "pending": []},
        "target_hooks": {"configured": [], "target_only": []},
    }

    target_hooks: dict = {}
    if target_path.is_file():
        target = _safe_load_json(target_path)
        if not isinstance(target, dict):
            result["status"] = "error"
            result["error"] = f"{target_path} is not valid JSON"
            return result

        target_hooks = target.get("hooks", {})
        if not isinstance(target_hooks, dict):
            target_hooks = {}

        configured = _iter_hook_rules(target_hooks)
        result["target_hooks"]["configured"] = configured

    if not canonical_path.is_file():
        result["target_hooks"]["target_only"] = result["target_hooks"]["configured"]
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

    # Stamp the ownership marker (ADR-0019) so the canonical rules compared
    # here match what ``generate_all_settings`` actually writes to the Claude
    # target — otherwise a synced (already-stamped) target rule would mismatch
    # the raw canonical and surface as a false conflict (issue #1110).
    canonical_hooks = _stamp_status_markers({"hooks": canonical_hooks})["hooks"]

    canonical_index: dict[tuple[str, str], list[dict]] = {}
    for row in _iter_hook_rules(canonical_hooks):
        canonical_index.setdefault((row["event"], row["matcher"]), []).append(row["rule"])

    target_index: dict[tuple[str, str], list[dict]] = {}
    result["target_hooks"]["target_only"] = [
        row
        for row in result["target_hooks"]["configured"]
        if (row["event"], row["matcher"]) not in canonical_index
    ]

    if not _has_hook_rules(canonical_hooks):
        # Canonical emits no hooks. A sync still prunes any memtomem-owned
        # target rule (ADR-0019), so surface that as out_of_sync; only when the
        # target has none of memtomem's rules is there genuinely nothing to do.
        owned_in_target = any(
            _rule_is_memtomem_owned(rule)
            for rules in target_hooks.values()
            if isinstance(rules, list)
            for rule in rules
        )
        result["status"] = "out_of_sync" if owned_in_target else "no_hooks"
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

    # Index target rules by (event, matcher) preserving multiplicity. Claude
    # Code allows the same matcher (or no matcher) to appear more than once
    # under one event, so collapsing to a single dict-of-rule lets a
    # byte-identical canonical contribution mismatch the *second* user rule
    # and surface as a false-positive conflict (mirrors the same fix on the
    # merge side in PR #844).
    for event, rules in target_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if isinstance(rule, dict):
                target_index.setdefault((event, rule.get("matcher", "")), []).append(rule)

    # Mirror _merge_hooks_record's resolution so the GET diff classifies rules
    # exactly as a sync would (ADR-0019). Each memtomem-owned target rule under
    # a matcher is one in-place "managed update" slot, consumed FIFO before
    # falling back to the user-wins conflict / append logic. Comparing against
    # the *specific* owned rule being consumed — not any same-matcher rule —
    # keeps the diff and the merge in agreement even when a user rule happens to
    # equal canonical while a stale owned rule still needs replacing (Codex r2).
    owned_by_matcher: dict[tuple[str, str], list[dict]] = {}
    user_by_matcher: dict[tuple[str, str], list[dict]] = {}
    for key, rules_at in target_index.items():
        for r in rules_at:
            (owned_by_matcher if _rule_is_memtomem_owned(r) else user_by_matcher).setdefault(
                key, []
            ).append(r)
    consumed_owned: dict[tuple[str, str], int] = {}

    for event, rules in canonical_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = rule.get("matcher", "")
            key = (event, matcher)

            owned_slots = owned_by_matcher.get(key, [])
            used = consumed_owned.get(key, 0)
            if used < len(owned_slots):
                # Replaces an existing memtomem-owned rule in place: synced if
                # byte-equal (ignoring the marker), else a pending managed
                # update — never a user conflict (issue #1110).
                consumed_owned[key] = used + 1
                bucket = "synced" if _rule_content_equal(owned_slots[used], rule) else "pending"
                result["hooks"][bucket].append({"event": event, "matcher": matcher, "rule": rule})
                continue

            # Owned slots exhausted → merge additively against the user rules.
            user_rules = user_by_matcher.get(key, [])
            if any(_rule_content_equal(u, rule) for u in user_rules):
                result["hooks"]["synced"].append({"event": event, "matcher": matcher, "rule": rule})
            elif user_rules:
                # Surface the first user rule as the conflict representative —
                # keeps the payload shape stable for the resolve flow while not
                # silently shadowing the rest of the user's same-matcher rules.
                result["hooks"]["conflicts"].append(
                    {
                        "event": event,
                        "matcher": matcher,
                        "existing": user_rules[0],
                        "proposed": rule,
                    }
                )
            else:
                result["hooks"]["pending"].append(
                    {"event": event, "matcher": matcher, "rule": rule}
                )

    # Any memtomem-owned target slot a canonical rule did NOT consume will be
    # pruned by the next sync (ADR-0019) — whether the whole (event, matcher) is
    # gone from canonical, or canonical simply emits fewer rules under it than
    # the target currently holds. Those removals put the status out_of_sync even
    # when there is nothing to add. Plain user hooks are never counted.
    pending_removals = sum(
        max(0, len(owned) - consumed_owned.get(key, 0)) for key, owned in owned_by_matcher.items()
    )

    if result["hooks"]["conflicts"]:
        result["status"] = "conflicts"
    elif result["hooks"]["pending"] or pending_removals:
        result["status"] = "out_of_sync"
    else:
        result["status"] = "in_sync"

    return result


@router.get("/settings-sync")
@router.get("/context/settings")
async def get_settings_sync(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Claude Code settings tier to compare against.",
    ),
) -> dict:
    """Return structured settings sync status with conflict details."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, target_scope)
    payload = _compare_hooks(canonical_path, target_path)
    # Surface the active scope so the Web UI can render a scope-accurate
    # target label (issue #962). Without this the panel falls back to a
    # hardcoded "User-scope target:" that lies when the scope is
    # project_shared or project_local.
    payload["target_scope"] = target_scope
    payload["duplicate_tier_warnings"] = _serialize_duplicate_tiers(
        detect_duplicate_tiers(project_root, active_scope=target_scope)
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
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Claude Code settings tier to write.",
    ),
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
    duplicates = detect_duplicate_tiers(project_root, active_scope=target_scope)
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                results = generate_all_settings(
                    project_root,
                    scope=target_scope,
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
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Claude Code settings tier to update.",
    ),
) -> dict:
    """Resolve a single hook conflict by replacing the target's rule."""
    if body.action != "use_proposed":
        raise HTTPException(400, detail=f"Unknown action: {body.action}")

    label = _rule_label(body.event, body.matcher)

    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, target_scope)

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

                # Stamp the ownership marker (ADR-0019) so the rule we write
                # matches what ``generate_all_settings`` would write — a later
                # re-sync then recognizes it as memtomem-owned and can update
                # it instead of re-flagging it as a conflict (issue #1110).
                proposed = _stamp_status_markers({"hooks": {body.event: [proposed]}})["hooks"][
                    body.event
                ][0]

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


class RuleActionRequest(BaseModel):
    event: str
    matcher: str = ""
    rule_index: int
    rule_hash: str
    target_mtime_ns: str | None = None
    canonical_mtime_ns: str | None = None
    confirm_private_to_shared: bool = False


def _freshness_envelope(
    *,
    reason: str,
    target_path: Path,
    canonical_path: Path,
) -> dict:
    return {
        "status": "aborted",
        "reason": reason,
        "target_mtime_ns": _mtime_ns(target_path),
        "canonical_mtime_ns": _mtime_ns(canonical_path),
    }


def _ok_envelope(
    *,
    reason: str,
    target_path: Path,
    canonical_path: Path,
    extra: dict[str, Any] | None = None,
) -> dict:
    payload = {
        "status": "ok",
        "reason": reason,
        "target_mtime_ns": _mtime_ns(target_path),
        "canonical_mtime_ns": _mtime_ns(canonical_path),
    }
    if extra:
        payload.update(extra)
    return payload


def _load_settings_record(path: Path, *, label: str) -> dict:
    data = _safe_load_json(path)
    if not isinstance(data, dict):
        raise HTTPException(422, detail=f"{label} settings is not valid JSON")
    return data


def _hooks_record(settings: dict, *, label: str, create: bool = False) -> dict:
    hooks = settings.get("hooks")
    if hooks is None and create:
        hooks = {}
        settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise HTTPException(422, detail=f"{label} hooks is not a record")
    return hooks


def _target_rule_for_action(
    target: dict,
    body: RuleActionRequest,
    *,
    target_path: Path,
    canonical_path: Path,
) -> tuple[list, dict] | dict:
    target_hooks = _hooks_record(target, label="Target")
    rules = target_hooks.get(body.event, [])
    if not isinstance(rules, list):
        raise HTTPException(422, detail="Target hook event is not a list")
    stale = _freshness_envelope(
        reason="Target rule changed. Refresh and retry.",
        target_path=target_path,
        canonical_path=canonical_path,
    )
    if body.rule_index < 0 or body.rule_index >= len(rules):
        return stale
    rule = rules[body.rule_index]
    if not isinstance(rule, dict):
        return stale
    if rule.get("matcher", "") != body.matcher or _rule_hash(rule) != body.rule_hash:
        return stale
    return rules, rule


def _check_rule_action_freshness(
    body: RuleActionRequest,
    *,
    target_path: Path,
    canonical_path: Path,
    check_canonical: bool = True,
) -> dict | None:
    target_mtime = _mtime_ns(target_path)
    canonical_mtime = _mtime_ns(canonical_path)
    if body.target_mtime_ns != target_mtime:
        return _freshness_envelope(
            reason="Target settings file was modified by another process. Refresh and retry.",
            target_path=target_path,
            canonical_path=canonical_path,
        )
    if check_canonical and body.canonical_mtime_ns != canonical_mtime:
        return _freshness_envelope(
            reason="Canonical settings file was modified by another process. Refresh and retry.",
            target_path=target_path,
            canonical_path=canonical_path,
        )
    return None


@router.post("/settings-sync/rules/delete")
@router.post("/context/settings/rules/delete")
async def delete_target_rule(
    body: RuleActionRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Claude Code settings tier to update.",
    ),
) -> dict:
    """Delete one exact rule from the selected target settings file."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, target_scope)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if not target_path.is_file():
                    raise HTTPException(404, detail="Target settings file does not exist")
                stale = _check_rule_action_freshness(
                    body,
                    target_path=target_path,
                    canonical_path=canonical_path,
                    check_canonical=False,
                )
                if stale:
                    return stale
                target = _load_settings_record(target_path, label="Target")
                match = _target_rule_for_action(
                    target,
                    body,
                    target_path=target_path,
                    canonical_path=canonical_path,
                )
                if isinstance(match, dict):
                    return match
                rules, _rule = match
                del rules[body.rule_index]
                target_hooks = _hooks_record(target, label="Target")
                if rules:
                    target_hooks[body.event] = rules
                else:
                    target_hooks.pop(body.event, None)
                target["hooks"] = target_hooks
                _write_json(target_path, target)
                return _ok_envelope(
                    reason=f"Rule '{_rule_label(body.event, body.matcher)}' deleted from target",
                    target_path=target_path,
                    canonical_path=canonical_path,
                )
    except TimeoutError:
        raise HTTPException(503, "Delete timed out — another sync may be in progress")


@router.post("/settings-sync/rules/promote")
@router.post("/context/settings/rules/promote")
async def promote_target_rule(
    body: RuleActionRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Claude Code settings tier to read from.",
    ),
) -> dict:
    """Promote one exact target rule into the canonical settings file."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, target_scope)
    label = _rule_label(body.event, body.matcher)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if not target_path.is_file():
                    raise HTTPException(404, detail="Target settings file does not exist")
                stale = _check_rule_action_freshness(
                    body, target_path=target_path, canonical_path=canonical_path
                )
                if stale:
                    return stale

                target = _load_settings_record(target_path, label="Target")
                match = _target_rule_for_action(
                    target,
                    body,
                    target_path=target_path,
                    canonical_path=canonical_path,
                )
                if isinstance(match, dict):
                    return match
                _rules, rule = match
                if target_scope in ("user", "project_local") and not body.confirm_private_to_shared:
                    return {
                        "status": "needs_confirmation",
                        "reason": (
                            "Promoting a private target hook writes it to shared canonical "
                            ".memtomem/settings.json. Confirm to continue."
                        ),
                        "target_scope": target_scope,
                        "target_mtime_ns": _mtime_ns(target_path),
                        "canonical_mtime_ns": _mtime_ns(canonical_path),
                    }

                if canonical_path.is_file():
                    canonical = _load_settings_record(canonical_path, label="Canonical")
                else:
                    canonical = {}
                canonical_hooks = _hooks_record(canonical, label="Canonical", create=True)
                event_rules = canonical_hooks.get(body.event, [])
                if not isinstance(event_rules, list):
                    raise HTTPException(422, detail="Canonical hook event is not a list")

                same_matcher = [
                    existing
                    for existing in event_rules
                    if isinstance(existing, dict) and existing.get("matcher", "") == body.matcher
                ]
                if any(_rule_hash(existing) == body.rule_hash for existing in same_matcher):
                    return _ok_envelope(
                        reason=f"Rule '{label}' already exists in canonical",
                        target_path=target_path,
                        canonical_path=canonical_path,
                        extra={"idempotent": True},
                    )
                if same_matcher:
                    return {
                        "status": "conflict",
                        "reason": f"Canonical already has a different rule for '{label}'",
                        "existing": same_matcher,
                        "proposed": rule,
                        "target_mtime_ns": _mtime_ns(target_path),
                        "canonical_mtime_ns": _mtime_ns(canonical_path),
                    }

                event_rules.append(rule)
                canonical_hooks[body.event] = event_rules
                canonical["hooks"] = canonical_hooks
                canonical_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(canonical_path, canonical)
                return _ok_envelope(
                    reason=f"Rule '{label}' promoted to canonical",
                    target_path=target_path,
                    canonical_path=canonical_path,
                )
    except TimeoutError:
        raise HTTPException(503, "Promote timed out — another sync may be in progress")
