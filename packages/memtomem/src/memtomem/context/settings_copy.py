"""Cross-project per-hook copy for settings hooks (#1281, campaign #1270 A-11).

:mod:`memtomem.context.settings_migrate` moves canonical-matched hook
entries between tiers of ONE project; this module copies ONE
canonical-matched inner hook entry from a source project into another
project. It is deliberately a separate mechanism from the artifact
transfer engine (ADR-0023 support matrix: settings hooks are not
``{kind}/{name}`` artifacts).

Durability contract (the load-bearing design decision): a stamped
(memtomem-owned) rule that is NOT in the destination project's canonical
``.memtomem/settings.json`` is garbage-collected by that project's next
settings sync (``_merge_hooks_record`` drops owned rules with no matching
contribution — ADR-0019). A tier-only copy would therefore self-destruct.
The copy writes TWO files at the destination, canonical first:

1. ``<dst>/.memtomem/settings.json`` — the inner entry verbatim from the
   source canonical (canonical is the pre-stamp domain; sync stamps at
   fan-out). This makes the destination's syncs own and maintain the rule.
2. the destination-tier Claude settings file
   (``resolve_scope_path(dst_root, dst_scope)``) — the stamped rule
   (ADR-0019 marker), so the hook is live immediately without a sync run.

Canonical-first ordering is self-healing: a crash between the writes
leaves the canonical in place; a re-run classifies it ``exact`` and
completes the tier leg. Codex/Gemini/Kimi runtime fan-out is NOT
generated — the result carries ``needs_sync`` plus the exact follow-up
sync command (the destination project's sync materializes the entry for
every other runtime). The companion fix that makes the tier leg sound —
``generate_all_settings`` re-reading the canonical under the per-target
lock — rides the same change set; without it a sync holding a stale
canonical could prune the freshly stamped rule.

Gate A runs unconditionally with ``scope="project_shared"`` hardcoded:
``.memtomem/settings.json`` is git-tracked regardless of the destination
tier, so every copy lands content in the destination repo's history (the
``promote_target_rule`` precedent — keying the scan on the tier would
skip-warn exactly the private-tier cases the gate exists for). The scan
covers the canonical fragment; the stamped tier rule differs only by the
constant ``statusMessage`` marker prefix, so one scan covers both legs.

Classification reuses the settings-migrate semantics per leg —
``already_*`` is a no-op, a conflict never duplicates a matcher — with
one cross-leg rule: a CANONICAL conflict skips both legs (writing only
the tier rule would set up the destination's own sync to replace it with
the conflicting canonical version, silently evaporating the copy), while
a TIER conflict still writes the canonical leg (the definition is
durable; the destination's next sync surfaces the tier conflict through
the established ownership-merge warnings).
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.context.privacy_scan import raise_or_collect, scan_text_content
from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    _MALFORMED,
    _SETTINGS_LOCK_BUDGET_S,
    _read_with_mtime,
    _rule_content_equal,
)
from memtomem.context.settings import (
    resolve_scope_path as _resolve_tier_path,
)
from memtomem.context.settings_doctor import (
    ALL_SCOPES,
    HookSignature,
    _normalize_command,
    _normalize_matcher,
)
from memtomem.context.settings_migrate import (
    _safe_load_json_dict,
    _signature_for_inner,
    _stamp_rule_for_target,
    _write_json,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AmbiguousHookSelectorError",
    "HookCopyPlan",
    "HookCopyResult",
    "HookNotFoundError",
    "LegState",
    "apply_hook_copy",
    "format_copy_summary",
    "gate_a_scan",
    "plan_hook_copy",
]


class HookNotFoundError(ValueError):
    """The selector matched no canonical inner hook entry at the source."""


class AmbiguousHookSelectorError(ValueError):
    """The selector matched more than one canonical inner hook entry."""


#: Per-leg classification. ``missing`` → the leg will be written;
#: ``exact`` → functionally equal entry already present (no-op);
#: ``conflict`` → a different entry holds the ``(event, matcher)`` slot.
LegState = Literal["missing", "exact", "conflict"]


@dataclass(frozen=True)
class HookCopyPlan:
    """One source hook entry and how it would land at the destination.

    Leg states are computed lock-free at plan time for preview;
    :func:`apply_hook_copy` re-reads and re-classifies both destination
    files under their sidecar locks before writing (the settings-migrate
    TOCTOU discipline), so a separated dry-run → apply workflow stays
    sound. ``canonical_inner`` is the source canonical's inner entry
    verbatim — the copied content is frozen at plan time (the
    settings-migrate ``rule_to_write_at_target`` precedent).
    """

    src_project_root: Path
    dst_project_root: Path
    dst_scope: str
    src_canonical_path: Path
    dst_canonical_path: Path
    dst_target_path: Path
    signature: HookSignature
    canonical_inner: dict
    rule_for_canonical: dict
    rule_for_target: dict
    canonical_state: LegState
    canonical_reason: str
    target_state: LegState
    target_reason: str

    @property
    def label(self) -> str:
        """Human ``event:matcher`` label (event alone for empty matchers)."""
        sig = self.signature
        return f"{sig.event}:{sig.matcher}" if sig.matcher else sig.event

    @property
    def is_noop(self) -> bool:
        """Both legs already carry the entry — nothing to write."""
        return self.canonical_state == "exact" and self.target_state == "exact"

    @property
    def has_conflict(self) -> bool:
        return self.canonical_state == "conflict" or self.target_state == "conflict"

    @property
    def pending_canonical_write(self) -> bool:
        """The canonical leg would write — drives the git-tracked-write gate."""
        return self.canonical_state == "missing"

    @property
    def pending_target_write(self) -> bool:
        """The tier leg would write.

        A canonical conflict blocks BOTH legs in :func:`apply_hook_copy`,
        so a pending tier write requires a non-conflicted canonical.
        Surfaces key their consent gates on these two properties (the
        no-op-never-prompts contract): the CLI's ``--confirm-project-shared``
        / host-write prompt and the web route's ``confirm_project_shared``
        / ``allow_host_writes`` envelopes must agree on what is pending.
        """
        return self.canonical_state != "conflict" and self.target_state == "missing"


@dataclass
class HookCopyResult:
    """Outcome of one apply step."""

    plan: HookCopyPlan
    canonical_written: bool = False
    target_written: bool = False
    canonical_already: bool = False
    target_already: bool = False
    warnings: list[str] = field(default_factory=list)
    needs_sync: bool = True
    sync_command: str = ""


def _sync_followup_command(dst_root: Path, dst_scope: str) -> str:
    """Exact follow-up sync command for the non-Claude runtime fan-out.

    Always ``cd``-prefixed — settings contributions are project-anchored
    even for the user tier (unlike artifact user-tier sync), and a
    single-project cross-cwd sync selector does not exist (A-9 shipped
    only ``--all-projects``). ``--scope`` is pinned because the bare
    command resolves the tier from ``hooks.target_scope`` and would sync
    a different file for a non-default destination tier.
    """
    return (
        f"cd {shlex.quote(str(dst_root))} && mm context sync --include=settings --scope {dst_scope}"
    )


# ── Selection (source side) ─────────────────────────────────────────


def _iter_canonical_candidates(
    canonical_hooks: dict,
    event: str,
    matcher_norm: str,
) -> list[tuple[HookSignature, dict]]:
    """Unique-signature inner entries under ``(event, matcher)`` in source order."""
    out: list[tuple[HookSignature, dict]] = []
    seen: set[HookSignature] = set()
    rules = canonical_hooks.get(event, [])
    if not isinstance(rules, list):
        return out
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _normalize_matcher(rule.get("matcher", "")) != matcher_norm:
            continue
        inner_list = rule.get("hooks", [])
        if not isinstance(inner_list, list):
            continue
        for inner in inner_list:
            sig = _signature_for_inner(event, rule.get("matcher", ""), inner)
            if sig is None or sig in seen:
                continue
            seen.add(sig)
            out.append((sig, inner))
    return out


def _available_labels(canonical_hooks: dict) -> list[str]:
    """All ``event:matcher`` labels carrying at least one signed inner entry."""
    labels: list[str] = []
    seen: set[tuple[str, str]] = set()
    if not isinstance(canonical_hooks, dict):
        return labels
    for event, rules in canonical_hooks.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = _normalize_matcher(rule.get("matcher", ""))
            inner_list = rule.get("hooks", [])
            if not isinstance(inner_list, list):
                continue
            if not any(
                _signature_for_inner(event, matcher, inner) is not None for inner in inner_list
            ):
                continue
            key = (event, matcher)
            if key in seen:
                continue
            seen.add(key)
            labels.append(f"{event}:{matcher}" if matcher else event)
    return labels


# ── Classification (destination side) ───────────────────────────────


def _rule_inner_commands(rules: list[dict]) -> list[str]:
    """Normalized command previews of every inner entry in *rules*."""
    out: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        for inner in rule.get("hooks", []):
            if not isinstance(inner, dict):
                continue
            command = _normalize_command(inner.get("command", ""))
            if command:
                out.append(command)
    return out


def _classify_leg(
    doc: dict,
    sig: HookSignature,
    canonical_inner: dict,
    *,
    leg: str,
) -> tuple[LegState, str]:
    """Classify how one destination document relates to the copied entry.

    Same three-state semantics as ``settings_migrate._classify_target``
    (ownership-marker fields ignored via ``_rule_content_equal``), but the
    conflict reason NAMES the colliding entry — the issue's acceptance
    criterion — instead of the migrate-specific remediation prose.
    """
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, dict):
        # Structurally unusable hooks value — surfaced by the caller as a
        # malformed-leg refusal before classification is attempted.
        return ("conflict", f"{leg} 'hooks' is not a record keyed by event name")
    rules = hooks.get(sig.event, [])
    if not isinstance(rules, list):
        return ("conflict", f"{leg} 'hooks.{sig.event}' is not a list of rules")
    same_matcher = [
        rule
        for rule in rules
        if isinstance(rule, dict) and _normalize_matcher(rule.get("matcher", "")) == sig.matcher
    ]
    if not same_matcher:
        return ("missing", "")
    for rule in same_matcher:
        for inner in rule.get("hooks", []):
            if isinstance(inner, dict) and _rule_content_equal(
                {"matcher": sig.matcher, "hooks": [inner]},
                {"matcher": sig.matcher, "hooks": [canonical_inner]},
            ):
                return ("exact", "")
    label = f"{sig.event}:{sig.matcher}" if sig.matcher else sig.event
    colliding = _rule_inner_commands(same_matcher)
    preview = "; ".join(repr(c) for c in colliding[:3]) if colliding else "no command entries"
    return (
        "conflict",
        (
            f"{leg} already has a rule under '{label}' whose inner hooks "
            f"differ from the copied entry (existing: {preview}). Resolve "
            f"manually, then re-run the copy — a same-matcher duplicate "
            f"would fire twice."
        ),
    )


def _append_rule(doc: dict, event: str, rule: dict) -> dict:
    """Return a copy of *doc* with *rule* appended under ``hooks[event]``.

    Callers guarantee (via :func:`_classify_leg`) that ``hooks`` is a
    record and ``hooks[event]`` is absent or a list — a malformed shape
    classifies as a conflict-grade refusal and never reaches the append,
    so this helper never coerces (the ``MalformedSettingsError`` lesson:
    coercing destroys the user's hook configuration).
    """
    out = dict(doc)
    hooks = dict(out.get("hooks", {})) if isinstance(out.get("hooks"), dict) else {}
    existing = hooks.get(event, [])
    rules = list(existing) if isinstance(existing, list) else []
    rules.append(rule)
    hooks[event] = rules
    out["hooks"] = hooks
    return out


# ── Gate A ──────────────────────────────────────────────────────────


def gate_a_scan(plan: HookCopyPlan, surface: str) -> None:
    """Scan the exact canonical fragment the copy would write (always).

    ``scope="project_shared"`` is hardcoded — the destination canonical
    ``.memtomem/settings.json`` is git-tracked regardless of the
    destination tier (``promote_target_rule`` precedent). Raises
    :class:`memtomem.context.privacy_scan.PrivacyBlockedError` on a hit;
    surfaces translate (CLI → ClickException, web → HTTP 422 string).
    """
    fragment = json.dumps(
        {plan.signature.event: [plan.rule_for_canonical]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    scan = scan_text_content(
        fragment,
        source_path=plan.src_canonical_path,
        surface=surface,
        scope="project_shared",
        project_root=plan.dst_project_root,
    )
    if scan.decision in ("blocked", "blocked_project_shared"):
        raise_or_collect(
            scan,
            scope="project_shared",
            kind="hook rule",
            artifact_name=plan.label,
            remediation_hint=(
                f"Remove the secret from the hook rule in "
                f"{plan.src_canonical_path}, then re-run the copy. There is "
                f"no force valve: the destination canonical "
                f"({plan.dst_canonical_path}) is git-tracked for every "
                f"destination tier."
            ),
        )


# ── Planning ────────────────────────────────────────────────────────


def plan_hook_copy(
    src_project_root: Path,
    *,
    event: str,
    matcher: str,
    dst_project_root: Path,
    dst_scope: str,
    hook_command: str | None = None,
) -> HookCopyPlan:
    """Resolve the selector at the source and classify the destination legs.

    Read-only. Raises:

    * :class:`HookNotFoundError` — source canonical missing/has no
      signed entry under ``(event, matcher)`` (message lists the
      available labels), or the ``hook_command`` filter eliminated every
      candidate (message lists the commands under the matcher).
    * :class:`AmbiguousHookSelectorError` — more than one signed entry
      under ``(event, matcher)`` and no/too-loose ``hook_command``
      (message lists the candidate command previews).
    * :class:`ValueError` — unknown ``dst_scope``, or source and
      destination resolve to the same project (settings-migrate's
      territory).
    """
    if dst_scope not in ALL_SCOPES:
        raise ValueError(f"unknown destination tier: {dst_scope!r} (use one of {ALL_SCOPES})")

    src_root = src_project_root.expanduser().resolve()
    dst_root = dst_project_root.expanduser().resolve()
    if src_root == dst_root:
        raise ValueError(
            "source and destination are the same project; cross-tier moves "
            "within one project are `mm context settings-migrate`'s job, and "
            "fan-out is `mm context sync --include=settings`'s."
        )

    src_canonical_path = src_root / CANONICAL_SETTINGS_FILE
    canonical = _safe_load_json_dict(src_canonical_path)
    if canonical is None:
        raise HookNotFoundError(
            f"source project has no readable canonical settings "
            f"({src_canonical_path}); nothing to copy from."
        )
    canonical_hooks = canonical.get("hooks", {})
    if not isinstance(canonical_hooks, dict):
        canonical_hooks = {}

    matcher_norm = _normalize_matcher(matcher)
    candidates = _iter_canonical_candidates(canonical_hooks, event, matcher_norm)
    if not candidates:
        labels = _available_labels(canonical_hooks)
        listing = "; available: " + ", ".join(labels) if labels else "; the canonical has no hooks"
        label = f"{event}:{matcher_norm}" if matcher_norm else event
        raise HookNotFoundError(
            f"no canonical-matched hook entry under '{label}' in {src_canonical_path}{listing}."
        )
    if hook_command is not None:
        narrowed = [(s, i) for s, i in candidates if hook_command in s.command_shape]
        if not narrowed:
            previews = ", ".join(repr(s.command_shape) for s, _ in candidates)
            raise HookNotFoundError(
                f"--hook-command {hook_command!r} matches none of the entries "
                f"under '{event}:{matcher_norm}' (candidates: {previews})."
            )
        candidates = narrowed
    if len(candidates) > 1:
        previews = ", ".join(repr(s.command_shape) for s, _ in candidates)
        raise AmbiguousHookSelectorError(
            f"{len(candidates)} entries match '{event}:{matcher_norm}'; "
            f"disambiguate with --hook-command <substring> (candidates: {previews})."
        )
    sig, canonical_inner = candidates[0]

    rule_for_canonical = {"matcher": sig.matcher, "hooks": [canonical_inner]}
    rule_for_target = _stamp_rule_for_target(sig.event, rule_for_canonical)

    dst_canonical_path = dst_root / CANONICAL_SETTINGS_FILE
    dst_target_path = _resolve_tier_path(dst_root, dst_scope)

    canonical_state, canonical_reason = _classify_dst_file(
        dst_canonical_path, sig, canonical_inner, leg="destination canonical settings"
    )
    target_state, target_reason = _classify_dst_file(
        dst_target_path, sig, canonical_inner, leg=f"destination {dst_scope} tier"
    )

    return HookCopyPlan(
        src_project_root=src_root,
        dst_project_root=dst_root,
        dst_scope=dst_scope,
        src_canonical_path=src_canonical_path,
        dst_canonical_path=dst_canonical_path,
        dst_target_path=dst_target_path,
        signature=sig,
        canonical_inner=canonical_inner,
        rule_for_canonical=rule_for_canonical,
        rule_for_target=rule_for_target,
        canonical_state=canonical_state,
        canonical_reason=canonical_reason,
        target_state=target_state,
        target_reason=target_reason,
    )


def _classify_dst_file(
    path: Path,
    sig: HookSignature,
    canonical_inner: dict,
    *,
    leg: str,
) -> tuple[LegState, str]:
    """Lock-free plan-time classification of one destination file.

    A missing file is ``missing`` (the leg creates it); an unreadable or
    non-object file classifies as ``conflict`` so the plan surfaces the
    refusal up front (apply re-derives and refuses the same way).
    """
    if not path.is_file():
        return ("missing", "")
    doc = _safe_load_json_dict(path)
    if doc is None:
        return (
            "conflict",
            f"{leg} ({path}) is not valid JSON (or not a JSON object); fix it manually first.",
        )
    return _classify_leg(doc, sig, canonical_inner, leg=leg)


# ── Apply ───────────────────────────────────────────────────────────


def apply_hook_copy(
    plan: HookCopyPlan,
    *,
    surface: str = "cli_context_settings_copy",
) -> HookCopyResult:
    """Apply *plan*: Gate A, then canonical leg, then tier leg.

    Both destination sidecar locks are held for the whole transaction in
    sorted order under one shared ``_SETTINGS_LOCK_BUDGET_S`` deadline
    (the settings-migrate pair-lock discipline; the set() dedupes a
    degenerate same-path pair). Both files are re-read and re-classified
    under the locks; the plan-time states are advisory only. Per-leg
    ``st_mtime_ns`` rechecks remain the second layer against direct disk
    edits that bypass the sidecar locks.

    Cross-leg conflict rule (module docstring): canonical conflict skips
    both legs; tier conflict still writes the canonical leg. Malformed
    files refuse their leg loudly and are never rewritten — a malformed
    CANONICAL also skips the tier leg (a tier-only write is the
    self-destructing copy this module exists to prevent).
    """
    gate_a_scan(plan, surface)

    result = HookCopyResult(
        plan=plan,
        needs_sync=True,
        sync_command=_sync_followup_command(plan.dst_project_root, plan.dst_scope),
    )
    retry_hint = "Re-run the copy to retry."

    lock_deadline = time.monotonic() + _SETTINGS_LOCK_BUDGET_S

    def _lock_timeout() -> float:
        return max(0.0, lock_deadline - time.monotonic())

    lock_paths = sorted(
        {_lock_path_for(plan.dst_canonical_path), _lock_path_for(plan.dst_target_path)},
        key=str,
    )
    try:
        with ExitStack() as stack:
            for lock_path in lock_paths:
                stack.enter_context(_file_lock(lock_path, timeout=_lock_timeout()))

            # ── canonical leg ────────────────────────────────────────
            canonical_raw, canonical_mtime_ns = _read_with_mtime(plan.dst_canonical_path)
            if canonical_raw is _MALFORMED:
                result.warnings.append(
                    f"{plan.dst_canonical_path} is not valid JSON (or not a "
                    f"JSON object); refusing to rewrite it — nothing was "
                    f"written (a tier-only copy would be pruned by the next "
                    f"sync). Fix the file manually, then re-run the copy."
                )
                return result
            canonical_doc: dict = canonical_raw if isinstance(canonical_raw, dict) else {}

            state, reason = _classify_leg(
                canonical_doc,
                plan.signature,
                plan.canonical_inner,
                leg="destination canonical settings",
            )
            if state == "conflict":
                result.warnings.append(reason)
                return result
            if state == "exact":
                result.canonical_already = True
            else:
                if (
                    plan.dst_canonical_path.is_file()
                    and plan.dst_canonical_path.stat().st_mtime_ns != canonical_mtime_ns
                ):
                    result.warnings.append(
                        f"{plan.dst_canonical_path} was modified by another "
                        f"process during apply; nothing was written. {retry_hint}"
                    )
                    return result
                _write_json(
                    plan.dst_canonical_path,
                    _append_rule(canonical_doc, plan.signature.event, plan.rule_for_canonical),
                )
                result.canonical_written = True

            # ── tier leg ─────────────────────────────────────────────
            target_raw, target_mtime_ns = _read_with_mtime(plan.dst_target_path)
            if target_raw is _MALFORMED:
                result.warnings.append(
                    f"{plan.dst_target_path} is not valid JSON (or not a JSON "
                    f"object); the canonical entry was written but the "
                    f"{plan.dst_scope} tier was not. Fix the file manually, "
                    f"then re-run the copy or `{result.sync_command}`."
                )
                return result
            target_doc: dict = target_raw if isinstance(target_raw, dict) else {}

            state, reason = _classify_leg(
                target_doc,
                plan.signature,
                plan.canonical_inner,
                leg=f"destination {plan.dst_scope} tier",
            )
            if state == "conflict":
                result.warnings.append(reason)
                if result.canonical_written:
                    result.warnings.append(
                        "the canonical entry WAS written — the definition is "
                        "durable; resolve the tier conflict and re-run the "
                        "copy, or let the destination's next settings sync "
                        "surface it through the ownership-merge warnings."
                    )
                return result
            if state == "exact":
                result.target_already = True
                return result
            if (
                plan.dst_target_path.is_file()
                and plan.dst_target_path.stat().st_mtime_ns != target_mtime_ns
            ):
                result.warnings.append(
                    f"{plan.dst_target_path} was modified by another process "
                    f"during apply; the canonical entry was written but the "
                    f"{plan.dst_scope} tier was not. {retry_hint}"
                )
                return result
            _write_json(
                plan.dst_target_path,
                _append_rule(target_doc, plan.signature.event, plan.rule_for_target),
            )
            result.target_written = True
    except TimeoutError:
        result.warnings.append(
            f"another process held a settings lock past the "
            f"{_SETTINGS_LOCK_BUDGET_S:g}s acquisition budget "
            f"({plan.dst_canonical_path} / {plan.dst_target_path}); nothing "
            f"was written. {retry_hint}"
        )

    return result


# ── Reporting ───────────────────────────────────────────────────────


def format_copy_summary(result: HookCopyResult) -> str:
    """One-line summary for the CLI footer."""
    parts: list[str] = []
    if result.canonical_written:
        parts.append("canonical entry added")
    elif result.canonical_already:
        parts.append("already in canonical")
    if result.target_written:
        parts.append(f"{result.plan.dst_scope} tier rule added")
    elif result.target_already:
        parts.append(f"already at {result.plan.dst_scope} tier")
    if result.warnings:
        parts.append(f"{len(result.warnings)} warning(s)")
    return ", ".join(parts) if parts else "nothing to do"
