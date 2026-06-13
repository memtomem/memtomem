"""Settings hooks sync status and conflict resolution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.config import TargetScope
from memtomem.context.privacy_scan import (
    PrivacyBlockedError,
    format_scan_block_message,
    scan_text_content,
)
from memtomem.context.projects import compute_scope_id
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
from memtomem.context.settings_copy import (
    AmbiguousHookSelectorError,
    HookCopyPlan,
    HookCopyResult,
    HookNotFoundError,
    apply_hook_copy,
    gate_a_scan,
    plan_hook_copy,
)
from memtomem.context.settings_doctor import (
    DuplicateTier,
    detect_duplicate_tiers,
)
from memtomem.web.routes._confirm import needs_confirmation_envelope
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_projects import (
    resolve_scope_root,
    resolve_writable_scope_root,
)
from memtomem.web.routes.context_transfer import (
    _reject_ineligible_destination,
    _resolve_destination,
)

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

    # Split the target rows (which carry their original ``rule_index`` +
    # ``rule_hash`` via ``_iter_hook_rules``) by (event, matcher), preserving
    # multiplicity. Claude Code allows the same matcher (or no matcher) to
    # appear more than once under one event, so collapsing to a single
    # dict-of-rule lets a byte-identical canonical contribution mismatch the
    # *second* user rule and surface as a false-positive conflict (mirrors the
    # merge-side fix in PR #844). Keeping the full rows lets each conflict carry
    # a stable identity for the resolve endpoint (issue #1112).
    configured_rows: list[dict] = result["target_hooks"]["configured"]
    owned_by_matcher: dict[tuple[str, str], list[dict]] = {}
    user_rows_by_matcher: dict[tuple[str, str], list[dict]] = {}
    for row in configured_rows:
        key = (row["event"], row["matcher"])
        if _rule_is_memtomem_owned(row["rule"]):
            owned_by_matcher.setdefault(key, []).append(row["rule"])
        else:
            user_rows_by_matcher.setdefault(key, []).append(row)
    consumed_owned: dict[tuple[str, str], int] = {}

    # A user row that is byte-equal to *some* canonical rule under the same
    # matcher is already in sync and must never be offered as a replacement
    # target. Everything else is conflict-eligible; we hand those out FIFO so
    # the Nth colliding canonical rule pairs with a *distinct* Nth user row —
    # this is what lets the resolve endpoint update the Nth duplicate instead
    # of always rewriting the first (issue #1112).
    canonical_by_matcher: dict[tuple[str, str], list[dict]] = {}
    for event, rules in canonical_hooks.items():
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if isinstance(rule, dict):
                canonical_by_matcher.setdefault((event, rule.get("matcher", "")), []).append(rule)
    conflict_user_rows: dict[tuple[str, str], list[dict]] = {
        key: [
            row
            for row in rows
            if not any(
                _rule_content_equal(row["rule"], c) for c in canonical_by_matcher.get(key, [])
            )
        ]
        for key, rows in user_rows_by_matcher.items()
    }
    consumed_conflict: dict[tuple[str, str], int] = {}

    # Mirror _merge_hooks_record's resolution so the GET diff classifies rules
    # exactly as a sync would (ADR-0019). Each memtomem-owned target rule under
    # a matcher is one in-place "managed update" slot, consumed FIFO before
    # falling back to the user-wins conflict / append logic. Comparing against
    # the *specific* owned rule being consumed — not any same-matcher rule —
    # keeps the diff and the merge in agreement even when a user rule happens to
    # equal canonical while a stale owned rule still needs replacing (Codex r2).
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

            # Owned slots exhausted → mirror _merge_hooks_record's Pass 2: a
            # canonical rule byte-equal to a user rule is synced; one with *no*
            # same-matcher user rule is appended (pending); otherwise the user
            # rule wins and we surface a conflict the user can manually accept.
            user_rows = user_rows_by_matcher.get(key, [])
            if any(_rule_content_equal(r["rule"], rule) for r in user_rows):
                result["hooks"]["synced"].append({"event": event, "matcher": matcher, "rule": rule})
                continue
            if not user_rows:
                # Merge only appends when nothing under the matcher belongs to
                # the user — that is the sole "will be added" case.
                result["hooks"]["pending"].append(
                    {"event": event, "matcher": matcher, "rule": rule}
                )
                continue

            # A conflict. Pair it with a *distinct* conflict-eligible user row
            # (FIFO) and stamp both identities so the resolve endpoint replaces
            # the *exact* row with the *exact* proposed rule — the Nth conflict
            # updates the Nth row (issue #1112). ``proposed`` is the
            # marker-stamped canonical rule so ``proposed_hash`` matches what
            # resolve re-derives. When more colliding canonical rules share a
            # matcher than there are distinct user rows, the overflow stays a
            # conflict (merge would also warn, never append) bound to the
            # representative row; there is only one slot to replace, and
            # resolve's ``rule_hash`` guard turns the second attempt on it into
            # a refresh-and-retry rather than a silent double-write.
            eligible = conflict_user_rows.get(key, [])
            taken = consumed_conflict.get(key, 0)
            if taken < len(eligible):
                partner = eligible[taken]
                consumed_conflict[key] = taken + 1
            else:
                partner = eligible[-1] if eligible else user_rows[0]
            result["hooks"]["conflicts"].append(
                {
                    "event": event,
                    "matcher": matcher,
                    "existing": partner["rule"],
                    "proposed": rule,
                    "target_rule_index": partner["rule_index"],
                    "target_rule_hash": partner["rule_hash"],
                    "proposed_hash": _rule_hash(rule),
                }
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


async def _sync_settings_core(
    project_root: Path,
    target_scope: TargetScope,
    *,
    allow_host_writes: bool = False,
) -> dict:
    """Lock-free settings sync core — the caller MUST hold ``_gateway_lock``.

    Shared by the standalone route below and ``POST /context/sync-all``
    (#1278), which runs every per-type core under ONE outer lock
    acquisition — the lock is a non-reentrant ``_LoopLocalLock``, so the
    core must never acquire it itself.

    Duplicate-tier detection runs BEFORE the merge so the warning
    reflects pre-write state (ADR-0010 §4: the warning fires in the
    user's actual workflow, not behind a separate command).

    ``generate_all_settings`` takes a per-target ``portalocker``
    lock (#1123 B3-3). Run it in a worker thread: a cross-process
    holder of ``.settings.json.lock`` would otherwise block this
    synchronous call ON the event loop thread, stalling every
    request AND preventing the caller's ``asyncio.timeout``
    from firing (its callback is scheduled on the blocked loop).
    The lock waits share a single whole-call budget
    (``_SETTINGS_LOCK_BUDGET_S``, below every caller's timeout, across
    all runtime targets, not per target), so the worker self-aborts with
    an ``aborted`` status rather than running past the timeout —
    ``asyncio.to_thread`` cannot cancel a thread, so without the
    budget a timed-out request would orphan a thread that writes
    after the 503 (#1145 review).

    Refusals stay in-band by design: host-target generators report a
    ``needs_confirmation`` *result row* instead of raising (the
    ``_confirm.py`` hold-out), so this core raises no ``SyncPhaseError``.
    """
    duplicates = detect_duplicate_tiers(project_root, active_scope=target_scope)
    results = await asyncio.to_thread(
        generate_all_settings,
        project_root,
        scope=target_scope,
        allow_host_writes=allow_host_writes,
    )
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


@router.post("/settings-sync")
@router.post("/context/settings/sync")
async def apply_settings_sync(
    body: ApplySettingsSyncRequest | None = None,
    project_root: Path = Depends(resolve_writable_scope_root),
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
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return await _sync_settings_core(
                    project_root, target_scope, allow_host_writes=allow_host_writes
                )
    except TimeoutError:
        raise _error(503, "busy", "Settings sync timed out — another sync may be in progress")


class ResolveRequest(BaseModel):
    event: str
    matcher: str = ""
    action: str = "use_proposed"
    # Exact-rule identity (issue #1112). When the client sends these, the
    # endpoint replaces the *specific* target row (``rule_index`` + ``rule_hash``)
    # with the *specific* proposed canonical rule (``proposed_hash``), so
    # duplicate same-matcher conflict rows resolve deterministically — the Nth
    # displayed conflict updates the Nth row. Omitting them falls back to
    # best-effort first-match by (event, matcher): old label-only clients keep
    # working and the single-conflict case is unaffected, at the cost of the
    # historical "first row wins" ambiguity when a matcher is duplicated.
    rule_index: int | None = None
    rule_hash: str | None = None
    proposed_hash: str | None = None


@router.post("/settings-sync/resolve")
@router.post("/context/settings/resolve")
async def resolve_conflict(
    body: ResolveRequest,
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Claude Code settings tier to update.",
    ),
) -> dict:
    """Resolve a single hook conflict by replacing the target's rule."""
    if body.action != "use_proposed":
        raise _error(400, "validation", f"Unknown action: {body.action}")

    # Rule identity (issue #1112) is all-or-nothing: ``rule_index`` +
    # ``rule_hash`` pin the exact target row and ``proposed_hash`` pins the
    # exact canonical rule. A partial set would mix an exact target row with a
    # first-match proposed rule and could write the wrong rule when a matcher
    # is duplicated, so reject it rather than silently mis-resolve. With none
    # set we fall back to the legacy label-only first-match for both sides.
    identity = (body.rule_index, body.rule_hash, body.proposed_hash)
    if any(v is not None for v in identity) and not all(v is not None for v in identity):
        raise _error(
            400,
            "validation",
            (
                "Partial rule identity: send rule_index, rule_hash, and "
                "proposed_hash together, or none for label-only resolve."
            ),
        )

    label = _rule_label(body.event, body.matcher)

    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    target_path = _claude_target(project_root, target_scope)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Read canonical rule
                if not canonical_path.is_file():
                    raise _error(404, "missing", "Canonical source does not exist")
                canonical = _safe_load_json(canonical_path)
                if not isinstance(canonical, dict):
                    raise _error(422, "parse", "Canonical source is not valid JSON")

                # Resolve the proposed canonical rule. Each candidate is
                # marker-stamped (ADR-0019) so the rule we write matches what
                # ``generate_all_settings`` would write — a later re-sync then
                # recognizes it as memtomem-owned and updates it instead of
                # re-flagging it as a conflict (issue #1110). When the client
                # sends ``proposed_hash`` (issue #1112) we pick the *exact*
                # canonical rule whose stamped hash the GET diff published,
                # which is the same hash the diff computed over stamped
                # canonical; otherwise we keep the first-match-by-matcher
                # behavior for old label-only clients.
                proposed = None
                # Shape guards mirroring the target side below (and the promote
                # route): array-form hooks / a non-list event value in valid
                # JSON escaped as an AttributeError 500 while the GET diff
                # reports a structured error (#1247 id 51). ``create=True``
                # only inserts into the local dict — this path never writes
                # the canonical file, so a missing ``hooks`` key stays the
                # legacy "rule not found" 404, not a 422.
                canonical_hooks = _hooks_record(canonical, label="Canonical", create=True)
                canonical_event_rules = canonical_hooks.get(body.event, [])
                if not isinstance(canonical_event_rules, list):
                    raise _error(422, "validation", "Canonical hook event is not a list")
                for rule in canonical_event_rules:
                    if not (isinstance(rule, dict) and rule.get("matcher", "") == body.matcher):
                        continue
                    stamped = _stamp_status_markers({"hooks": {body.event: [rule]}})["hooks"][
                        body.event
                    ][0]
                    if body.proposed_hash is None:
                        proposed = stamped
                        break
                    if _rule_hash(stamped) == body.proposed_hash:
                        proposed = stamped
                        break
                if proposed is None:
                    detail = (
                        f"Proposed rule for '{label}' not found in canonical source"
                        if body.proposed_hash is not None
                        else f"Rule '{label}' not in canonical source"
                    )
                    raise _error(404, "missing", detail)

                # Read target + mtime guard. ``st_mtime_ns`` matches
                # ``hot_reload.py`` precision and detects sub-second writes
                # that ``st_mtime`` (float seconds) misses.
                if not target_path.is_file():
                    raise _error(404, "missing", "Target settings file does not exist")

                mtime_ns = target_path.stat().st_mtime_ns
                target = _safe_load_json(target_path)
                if not isinstance(target, dict):
                    raise _error(422, "parse", "Target settings is not valid JSON")

                target_hooks: dict = target.get("hooks", {})
                if not isinstance(target_hooks, dict):
                    raise _error(422, "validation", "Target hooks is not a record")

                rules = target_hooks.get(body.event, [])
                if not isinstance(rules, list):
                    raise _error(422, "validation", "Target hook event is not a list")

                # Replace the rule in place. With identity (issue #1112) the Nth
                # duplicate resolves deterministically: address the exact row by
                # ``rule_index`` and verify ``rule_hash`` so a row that moved or
                # changed under us is rejected (stale → aborted, not a silent
                # mis-resolve). Without identity, keep the historical
                # first-match-by-matcher behavior.
                if body.rule_index is not None and body.rule_hash is not None:
                    candidate = (
                        rules[body.rule_index] if 0 <= body.rule_index < len(rules) else None
                    )
                    if (
                        not isinstance(candidate, dict)
                        or candidate.get("matcher", "") != body.matcher
                        or _rule_hash(candidate) != body.rule_hash
                    ):
                        return JSONResponse(
                            status_code=409,
                            content={
                                "status": "aborted",
                                "reason": "Target rule changed. Refresh and retry.",
                                "mtime_ns": str(target_path.stat().st_mtime_ns),
                                "error_kind": "conflict",
                                "reason_code": "stale_rule",
                            },
                        )
                    rules[body.rule_index] = proposed
                else:
                    replaced = False
                    for i, rule in enumerate(rules):
                        if isinstance(rule, dict) and rule.get("matcher", "") == body.matcher:
                            rules[i] = proposed
                            replaced = True
                            break
                    if not replaced:
                        raise _error(404, "missing", f"Rule '{label}' not found in target")

                # mtime check before write — protects against cross-process
                # writers (CLI, manual edit) that the in-process lock can't see.
                # Echo the current ``mtime_ns`` on abort so clients can refresh
                # local state without an extra round-trip — HTTP 409 with the
                # same body shape as the Skills/Commands/Agents stale-write
                # envelope (the comment used to CLAIM that parity while the
                # route returned 200, #1229).
                current_mtime_ns = target_path.stat().st_mtime_ns
                if current_mtime_ns != mtime_ns:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "aborted",
                            "reason": "Target file was modified by another process. Retry.",
                            "mtime_ns": str(current_mtime_ns),
                            "error_kind": "conflict",
                            "reason_code": "stale_mtime",
                        },
                    )

                target_hooks[body.event] = rules
                target["hooks"] = target_hooks
                _write_json(target_path, target)
                return {
                    "status": "ok",
                    "reason": f"Rule '{label}' replaced with memtomem's version",
                }
    except TimeoutError:
        raise _error(503, "busy", "Resolve timed out — another sync may be in progress")


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
        "error_kind": "conflict",
        "reason_code": "stale_mtime",
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
        raise _error(422, "parse", f"{label} settings is not valid JSON")
    return data


def _hooks_record(settings: dict, *, label: str, create: bool = False) -> dict:
    hooks = settings.get("hooks")
    if hooks is None and create:
        hooks = {}
        settings["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise _error(422, "validation", f"{label} hooks is not a record")
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
        raise _error(422, "validation", "Target hook event is not a list")
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
    project_root: Path = Depends(resolve_writable_scope_root),
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
                    raise _error(404, "missing", "Target settings file does not exist")
                stale = _check_rule_action_freshness(
                    body,
                    target_path=target_path,
                    canonical_path=canonical_path,
                    check_canonical=False,
                )
                if stale:
                    return JSONResponse(status_code=409, content=stale)
                target = _load_settings_record(target_path, label="Target")
                match = _target_rule_for_action(
                    target,
                    body,
                    target_path=target_path,
                    canonical_path=canonical_path,
                )
                if isinstance(match, dict):
                    return JSONResponse(status_code=409, content=match)
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
        raise _error(503, "busy", "Delete timed out — another sync may be in progress")


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
                    raise _error(404, "missing", "Target settings file does not exist")
                stale = _check_rule_action_freshness(
                    body, target_path=target_path, canonical_path=canonical_path
                )
                if stale:
                    return JSONResponse(status_code=409, content=stale)

                target = _load_settings_record(target_path, label="Target")
                match = _target_rule_for_action(
                    target,
                    body,
                    target_path=target_path,
                    canonical_path=canonical_path,
                )
                if isinstance(match, dict):
                    return JSONResponse(status_code=409, content=match)
                _rules, rule = match

                # Gate A (ADR-0011 §5, #1247): the canonical
                # .memtomem/settings.json is git-tracked project_shared. Scan
                # the EXACT fragment the append would write — {event: [rule]}
                # — because body.event is a free string that lands as a JSON
                # key (a secret-shaped event key must block too, not just a
                # secret in the rule body). Scope is hardcoded
                # project_shared: the route's target_scope is the tier being
                # READ from; using it would skip-warn private tiers, the
                # exact case this gate exists for. Placed before the consent
                # gate so a doomed promote never completes a
                # needs_confirmation round-trip.
                fragment = json.dumps(
                    {body.event: [rule]},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                fragment_scan = scan_text_content(
                    fragment,
                    source_path=target_path,
                    surface="web_settings_rule_promote",
                    scope="project_shared",
                    project_root=project_root,
                )
                if fragment_scan.decision in ("blocked", "blocked_project_shared"):
                    raise HTTPException(
                        422,
                        format_scan_block_message(
                            fragment_scan,
                            scope="project_shared",
                            kind="hook rule",
                            artifact_name=label,
                            remediation_hint=(
                                f"Remove the secret from the hook rule in "
                                f"{target_path}, or keep the rule in your "
                                f"private tier."
                            ),
                        ),
                    )

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
                    raise _error(422, "validation", "Canonical hook event is not a list")

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
        raise _error(503, "busy", "Promote timed out — another sync may be in progress")


# ── cross-project per-hook copy (#1281, campaign #1270 A-11) ─────────


class HookCopyRequest(BaseModel):
    """Body for ``POST /context/settings/hooks/copy``.

    ``to_project_scope_id`` is REQUIRED — the web surface restricts
    destinations to the registered discovery set (the A-5 contract; the
    typed-path consent valve is CLI-only), and a same-project copy is the
    settings-migrate/sync routes' job, refused by the engine.
    """

    event: str
    matcher: str = ""
    hook_command: str | None = None
    to_project_scope_id: str
    to_target_scope: TargetScope = "project_shared"
    confirm_project_shared: bool = False
    allow_host_writes: bool = False


def _serialize_hook_copy_plan(plan: HookCopyPlan) -> dict[str, Any]:
    """Wire shape for the plan half (nested under ``plan`` in envelopes).

    ``dst_project_scope_id`` mirrors the transfer route's field the UI
    uses for one-click follow-up sync.
    """
    return {
        "event": plan.signature.event,
        "matcher": plan.signature.matcher,
        "command_preview": plan.signature.command_shape,
        "src_project_root": str(plan.src_project_root),
        "dst_project_root": str(plan.dst_project_root),
        "dst_project_scope_id": compute_scope_id(plan.dst_project_root),
        "dst_scope": plan.dst_scope,
        "dst_canonical": str(plan.dst_canonical_path),
        "dst_target": str(plan.dst_target_path),
        "canonical": {"state": plan.canonical_state, "reason": plan.canonical_reason},
        "target": {"state": plan.target_state, "reason": plan.target_reason},
    }


def _serialize_hook_copy_result(result: HookCopyResult) -> dict[str, Any]:
    payload = _serialize_hook_copy_plan(result.plan)
    payload["canonical"] = {
        **payload["canonical"],
        "written": result.canonical_written,
        "already": result.canonical_already,
    }
    payload["target"] = {
        **payload["target"],
        "written": result.target_written,
        "already": result.target_already,
    }
    payload["warnings"] = list(result.warnings)
    payload["needs_sync"] = result.needs_sync
    payload["sync_command"] = result.sync_command
    return payload


@router.post("/context/settings/hooks/copy")
async def copy_hook_to_project(
    body: HookCopyRequest,
    request: Request,
    project_root: Path = Depends(resolve_scope_root),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the copy without touching disk: returns the plan "
            "(status='plan') regardless of confirmation flags."
        ),
    ),
) -> dict:
    """Copy one canonical-matched hook entry into another project.

    Web face of :func:`memtomem.context.settings_copy.apply_hook_copy`
    (the CLI sibling is ``mm context settings-copy``). Source = the
    ``?project_scope_id=`` project's canonical settings (server cwd when
    omitted); destination = ``body.to_project_scope_id`` (registered
    projects only) at ``body.to_target_scope``.

    Contracts:

    - **Error envelope** — object details (ADR-0023 §10 vocabulary);
      the privacy block stays the issue-pinned 422 STRING detail.
    - **Gate A before consent** (``promote_target_rule`` precedent): a
      doomed copy never completes a ``needs_confirmation`` round-trip.
      The scan always runs — the destination canonical is git-tracked
      for every tier.
    - **Pending-write-keyed gates** (the #1263 no-op-never-prompts
      contract), sequential round-trips when both apply:
      ``confirm_project_shared`` whenever a git-tracked write is pending
      (the canonical leg always is one), then ``allow_host_writes`` with
      ``host_targets`` when the user-tier file would be written.
    - **Status vocabulary** matches the CLI ``--json``: ``plan`` /
      ``needs_confirmation`` / ``noop`` / ``ok`` / ``conflicts`` (any
      apply warning — conflict skips, drift aborts — is ``conflicts``;
      per-leg detail rides the ``canonical`` / ``target`` objects).
    """
    dst_root, dst_scope_rec = _resolve_destination(
        request, body.to_project_scope_id, project_root, None
    )
    # The canonical leg writes the destination PROJECT for every tier, so
    # eligibility is evaluated as a project-tier destination — the
    # helper's user-tier exemption covers artifact host writes, which
    # have no project-side leg (N/A here).
    _reject_ineligible_destination(dst_scope_rec, "project_shared")
    if not (dst_root / ".memtomem").is_dir():
        raise HTTPException(
            status_code=409,
            detail={
                "error_kind": "conflict",
                "reason_code": "no_memtomem_store",
                "message": (
                    f"destination project has no .memtomem/ store: {dst_root}. "
                    f"Initialize it first: cd {dst_root} && mm context init"
                ),
                "project_scope_id": body.to_project_scope_id,
            },
        )

    try:
        plan = plan_hook_copy(
            project_root,
            event=body.event,
            matcher=body.matcher,
            hook_command=body.hook_command,
            dst_project_root=dst_root,
            dst_scope=body.to_target_scope,
        )
    except HookNotFoundError as exc:
        raise _error(404, "missing", str(exc)) from exc
    except (AmbiguousHookSelectorError, ValueError) as exc:
        raise _error(400, "validation", str(exc)) from exc

    if dry_run:
        return {"status": "plan", **_serialize_hook_copy_plan(plan)}

    # Gate A before the consent round-trip — and before the no-op check:
    # an already-at-target re-POST of secret-bearing content still 422s,
    # matching the promote route's scan-first ordering.
    try:
        gate_a_scan(plan, "web_context_settings_hook_copy")
    except PrivacyBlockedError as exc:
        raise HTTPException(422, exc.message) from exc

    git_tracked_write = plan.pending_canonical_write or (
        plan.pending_target_write and body.to_target_scope == "project_shared"
    )
    if git_tracked_write and not body.confirm_project_shared:
        tier_note = (
            " and its project_shared settings tier"
            if body.to_target_scope == "project_shared"
            else ""
        )
        return needs_confirmation_envelope(
            f"This will copy the hook into the destination project's "
            f"git-tracked files (the canonical .memtomem/settings.json"
            f"{tier_note}). Re-POST with confirm_project_shared=true after "
            f"confirming with the user.",
            confirm="confirm_project_shared",
            plan=_serialize_hook_copy_plan(plan),
        )
    if plan.pending_target_write and body.to_target_scope == "user" and not body.allow_host_writes:
        return needs_confirmation_envelope(
            "The destination tier is the user tier — a host path outside "
            "any project root. Re-POST with allow_host_writes=true after "
            "confirming with the user.",
            confirm="allow_host_writes",
            host_targets=[str(plan.dst_target_path)],
            plan=_serialize_hook_copy_plan(plan),
        )

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Worker thread: the engine takes cross-process sidecar
                # locks (canonical + tier pair) with its own 30s budget
                # (< 60s), so a cross-process holder cannot leave an
                # un-cancellable worker writing after the 503.
                result = await asyncio.to_thread(
                    apply_hook_copy, plan, surface="web_context_settings_hook_copy"
                )
    except TimeoutError:
        raise _error(
            503, "busy", "Copy timed out — another sync or settings write may be in progress"
        )
    except PrivacyBlockedError as exc:
        raise HTTPException(422, exc.message) from exc

    status = "noop" if plan.is_noop else ("conflicts" if result.warnings else "ok")
    return {"status": status, **_serialize_hook_copy_result(result)}
