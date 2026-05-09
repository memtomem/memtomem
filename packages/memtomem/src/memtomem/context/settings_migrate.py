"""Move memtomem-managed hook entries between settings tiers (ADR-0010 §4).

Companion module to :mod:`memtomem.context.settings_doctor`. The
detector reports tiers holding canonical-matched hook entries; this
module moves those entries from the source tier to the target tier.

Granularity is the **inner hook entry** (one ``{type, command, …}``
record under one ``(event, matcher)`` rule), not the whole rule. A
user who hand-authored a non-memtomem inner entry under the same
matcher keeps it where it is; only the canonical-signature-matched
entries move.

Write order is **target first, then source** (issue #872 acceptance
"`user → project_local` migration moves the entries cleanly"). If
target write succeeds but source write fails, the user sees a
transient cross-tier duplicate that the next ``settings-migrate`` run
heals idempotently — the target already has the entries, so target is
a no-op and source is then cleaned. If target write fails first, the
source is never touched.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from memtomem.context._atomic import atomic_write_text
from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    resolve_scope_path,
)
from memtomem.context.settings_doctor import (
    HookSignature,
    _normalize_command,
    _normalize_matcher,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrateMove:
    """One inner hook entry being moved from source to target.

    ``signature`` is the canonical-signature key used for matching.
    ``rule_to_write_at_target`` is the canonical ``{matcher, hooks}``
    rule that :func:`apply_migration` appends to target; the canonical
    inner shape (not the source-tier variant) is used so target ends
    up byte-clean. The three states a move can land in are decided by
    :func:`_classify_target` and surface here as a pair of booleans:

    * ``already_at_target=True``, ``conflict_at_target=False`` —
      target already carries an inner **byte-equal** to the canonical
      one. Target write is skipped; source is still cleaned.
    * ``already_at_target=False``, ``conflict_at_target=False`` —
      target either has no rule under ``(event, matcher)`` or has one
      that we can extend safely. Target gets the canonical rule
      appended; source is cleaned.
    * ``conflict_at_target=True`` — target has a rule under
      ``(event, matcher)`` whose inner hooks differ from canonical
      (different ``timeout``, extra keys, or a wholly different
      command). ``apply`` does **not** write target (would create a
      same-matcher duplicate) and does **not** clean source (would
      leave target permanently drifted from the canonical contract).
      ``conflict_reason`` carries the human-readable explanation.
    """

    signature: HookSignature
    rule_to_write_at_target: dict
    already_at_target: bool
    conflict_at_target: bool = False
    conflict_reason: str = ""


@dataclass(frozen=True)
class MigratePlan:
    """The full set of moves for one source → target migration."""

    source_scope: str
    target_scope: str
    source_path: Path
    target_path: Path
    moves: tuple[MigrateMove, ...]

    @property
    def is_noop(self) -> bool:
        """True when :func:`apply_migration` will write nothing.

        Two situations land here:

        * No moves at all — source has no canonical-matched entries.
        * Every move is a conflict — :attr:`applicable_moves` is empty,
          so apply writes nothing, but the user **does** have unresolved
          drift to surface. Callers that need to distinguish "genuinely
          nothing to do" from "all-conflict drift" should pair this
          property with a non-empty conflict check (the CLI does this
          at the ``--apply`` exit-1 gate).
        """
        return not self.applicable_moves

    @property
    def applicable_moves(self) -> tuple[MigrateMove, ...]:
        """Moves that will actually mutate disk on apply (no conflicts)."""
        return tuple(m for m in self.moves if not m.conflict_at_target)


@dataclass
class MigrateResult:
    """Outcome of an apply step."""

    plan: MigratePlan
    target_written: bool = False
    source_written: bool = False
    warnings: list[str] = field(default_factory=list)


# ── Planning ────────────────────────────────────────────────────────


def _safe_load_json_dict(path: Path) -> dict | None:
    """Read a settings.json file; ``None`` on missing / unreadable / non-dict.

    Self-contained dup of :func:`settings_doctor._load_settings_dict` so
    this module does not depend on that helper's private location.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _signature_for_inner(event: str, matcher: str, inner: dict) -> HookSignature | None:
    """Return the canonical signature for one inner hook entry, or None."""
    if not isinstance(inner, dict):
        return None
    command = _normalize_command(inner.get("command", ""))
    if not command:
        return None
    return HookSignature(
        event=event,
        matcher=_normalize_matcher(matcher),
        command_shape=command,
    )


def _index_canonical_inners(
    canonical_hooks: dict,
) -> dict[HookSignature, dict]:
    """Map canonical signature → the canonical inner hook entry (dict).

    The canonical entry is what gets appended to target on apply, so
    target ends up byte-clean rather than carrying the user's
    whitespace variant from source.
    """
    out: dict[HookSignature, dict] = {}
    if not isinstance(canonical_hooks, dict):
        return out
    for event, rules in canonical_hooks.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = rule.get("matcher", "")
            inner_list = rule.get("hooks", [])
            if not isinstance(inner_list, list):
                continue
            for inner in inner_list:
                sig = _signature_for_inner(event, matcher, inner)
                if sig is None:
                    continue
                out.setdefault(sig, inner)
    return out


def _target_rule_lookup(
    target_hooks: dict,
) -> dict[tuple[str, str], list[dict]]:
    """Index target rules by ``(event, matcher_normalized)``."""
    out: dict[tuple[str, str], list[dict]] = {}
    if not isinstance(target_hooks, dict):
        return out
    for event, rules in target_hooks.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = _normalize_matcher(rule.get("matcher", ""))
            out.setdefault((event, matcher), []).append(rule)
    return out


def _classify_target(
    target_index: dict[tuple[str, str], list[dict]],
    sig: HookSignature,
    canonical_inner: dict,
) -> tuple[str, str]:
    """Classify how target relates to a canonical inner under ``(event, matcher)``.

    Three outcomes drive the planner:

    * ``("missing", "")`` — no rule under ``(event, matcher)`` exists at
      target. Safe: the canonical rule will be appended.
    * ``("exact", "")`` — a rule under ``(event, matcher)`` carries an
      inner **byte-equal** to ``canonical_inner``. Safe: target write is
      skipped, source can still be cleaned (the canonical entry is
      already exactly there).
    * ``("conflict", reason)`` — a rule under ``(event, matcher)``
      exists but **no** inner is byte-equal to ``canonical_inner``.
      Refuse: blindly appending the canonical rule would create a
      second same-matcher rule (Claude Code merges them additively, so
      both would fire); skipping target while cleaning source would
      drift away from the canonical contract since target has a
      different inner shape (e.g. different ``timeout`` or extra keys).
      The user resolves manually.

    The ``signature``-level command-shape match (used by the detector
    for "is this a memtomem-managed entry" classification) is too loose
    here: a same-command-but-different-timeout inner at target is a
    drift we must surface, not silently treat as already-present.
    """
    rules = target_index.get((sig.event, sig.matcher), [])
    if not rules:
        return ("missing", "")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        for inner in rule.get("hooks", []):
            if isinstance(inner, dict) and inner == canonical_inner:
                return ("exact", "")
    label = f"{sig.event}:{sig.matcher}" if sig.matcher else sig.event
    return (
        "conflict",
        (
            f"target tier already has a rule under '{label}' whose inner "
            f"hooks differ from the canonical entry. Resolve manually "
            f"(remove the conflicting rule, then re-run migrate) before "
            f"the source can be cleaned."
        ),
    )


def plan_migration(
    project_root: Path,
    *,
    source_scope: str,
    target_scope: str,
) -> MigratePlan:
    """Compute the set of moves for ``source → target`` (no I/O writes).

    The planner is read-only — it inspects the canonical source, the
    source tier, and the target tier, and returns a :class:`MigratePlan`
    whose ``moves`` describe each canonical-matched inner-hook entry in
    source and how it would land at target.

    Raises :class:`ValueError` when ``source_scope == target_scope`` or
    when their resolved paths point at the same file (symlink) — both
    are user errors, not no-ops; surface them loudly.
    """
    if source_scope == target_scope:
        raise ValueError(
            f"settings-migrate source and target scopes must differ; "
            f"got source={source_scope!r}, target={target_scope!r}."
        )

    source_path = resolve_scope_path(project_root, source_scope)
    target_path = resolve_scope_path(project_root, target_scope)
    try:
        if source_path.resolve(strict=False) == target_path.resolve(strict=False):
            raise ValueError(
                f"settings-migrate source and target resolve to the same "
                f"file ({source_path}); refusing to migrate in-place."
            )
    except (OSError, RuntimeError):
        pass

    canonical = _safe_load_json_dict(project_root / CANONICAL_SETTINGS_FILE)
    canonical_inners = _index_canonical_inners(canonical.get("hooks", {}) if canonical else {})
    if not canonical_inners:
        return MigratePlan(
            source_scope=source_scope,
            target_scope=target_scope,
            source_path=source_path,
            target_path=target_path,
            moves=(),
        )

    source = _safe_load_json_dict(source_path)
    if source is None:
        return MigratePlan(
            source_scope=source_scope,
            target_scope=target_scope,
            source_path=source_path,
            target_path=target_path,
            moves=(),
        )

    target = _safe_load_json_dict(target_path) or {}
    target_index = _target_rule_lookup(target.get("hooks", {}))

    # Walk source tier; for each inner entry whose signature is in
    # canonical_inners, build a MigrateMove. Multiple source inners
    # under the same (event, matcher) sharing one signature collapse
    # to one move (idempotent canonical contribution).
    source_hooks = source.get("hooks", {})
    seen_sigs: set[HookSignature] = set()
    moves: list[MigrateMove] = []
    if isinstance(source_hooks, dict):
        for event, rules in source_hooks.items():
            if not isinstance(event, str) or not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                matcher = rule.get("matcher", "")
                inner_list = rule.get("hooks", [])
                if not isinstance(inner_list, list):
                    continue
                for inner in inner_list:
                    sig = _signature_for_inner(event, matcher, inner)
                    if sig is None or sig not in canonical_inners:
                        continue
                    if sig in seen_sigs:
                        continue
                    seen_sigs.add(sig)
                    canonical_inner = canonical_inners[sig]
                    rule_to_write = {
                        "matcher": sig.matcher,
                        "hooks": [canonical_inner],
                    }
                    status, reason = _classify_target(target_index, sig, canonical_inner)
                    moves.append(
                        MigrateMove(
                            signature=sig,
                            rule_to_write_at_target=rule_to_write,
                            already_at_target=(status == "exact"),
                            conflict_at_target=(status == "conflict"),
                            conflict_reason=reason,
                        )
                    )

    return MigratePlan(
        source_scope=source_scope,
        target_scope=target_scope,
        source_path=source_path,
        target_path=target_path,
        moves=tuple(moves),
    )


# ── Apply ───────────────────────────────────────────────────────────


def _strip_source_inner_entries(
    source: dict,
    sigs: set[HookSignature],
) -> dict:
    """Return a new source dict with the matching inner entries dropped.

    Empty rules / events are pruned. Non-hooks top-level keys (e.g.
    ``permissions``) are preserved verbatim.
    """
    out = dict(source)
    hooks = source.get("hooks", {})
    if not isinstance(hooks, dict):
        return out

    new_hooks: dict[str, list] = {}
    for event, rules in hooks.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            new_hooks[event] = rules  # preserve unknown shape verbatim
            continue
        new_rules: list[dict] = []
        for rule in rules:
            if not isinstance(rule, dict):
                new_rules.append(rule)
                continue
            matcher = rule.get("matcher", "")
            inner_list = rule.get("hooks", [])
            if not isinstance(inner_list, list):
                new_rules.append(rule)
                continue
            kept_inners: list = []
            for inner in inner_list:
                sig = _signature_for_inner(event, matcher, inner)
                if sig is not None and sig in sigs:
                    continue
                kept_inners.append(inner)
            if not kept_inners and isinstance(inner_list, list) and inner_list:
                # Whole rule's inner hooks all moved out → drop the rule.
                continue
            new_rule = dict(rule)
            new_rule["hooks"] = kept_inners
            new_rules.append(new_rule)
        if new_rules:
            new_hooks[event] = new_rules
    out["hooks"] = new_hooks
    return out


def _add_target_rules(
    target: dict,
    moves_to_write: list[MigrateMove],
) -> dict:
    """Append canonical rules for the given moves to target's hooks record.

    Moves where ``already_at_target`` is True are skipped — target
    already carries an inner hook with the same ``command_shape`` under
    the same ``(event, matcher)``. We do **not** also dedupe on rule
    identity here because the rule we're adding is the canonical
    `{matcher, hooks: [canonical_inner]}` shape; the
    ``already_at_target`` check above guarantees we don't double-fire.
    """
    out = dict(target)
    hooks = dict(out.get("hooks", {})) if isinstance(out.get("hooks"), dict) else {}
    for move in moves_to_write:
        if move.already_at_target:
            continue
        event = move.signature.event
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = []
        existing.append(move.rule_to_write_at_target)
        hooks[event] = existing
    out["hooks"] = hooks
    return out


def _write_json(path: Path, data: dict) -> None:
    """Atomic write matching :func:`memtomem.context.settings._write_json`.

    :func:`atomic_write_text` already ensures ``path.parent`` exists.
    """
    payload = json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    atomic_write_text(path, payload, mode=0o600)


def apply_migration(plan: MigratePlan) -> MigrateResult:
    """Apply *plan* to disk.

    Write order: **target first, then source.** A crash between the two
    leaves the user with a transient duplicate that the next plan run
    detects and heals (idempotent — target's inner hook now matches the
    canonical signature, so ``already_at_target`` is True and only the
    source clean-up step runs).
    """
    result = MigrateResult(plan=plan)
    if plan.is_noop:
        return result

    applicable = list(plan.applicable_moves)

    # --- Target write ---------------------------------------------------
    target_existing = _safe_load_json_dict(plan.target_path) or {}
    target_new_inners = [m for m in applicable if not m.already_at_target]
    if target_new_inners:
        target_merged = _add_target_rules(target_existing, target_new_inners)
        _write_json(plan.target_path, target_merged)
        result.target_written = True

    # --- Source write ---------------------------------------------------
    source_existing = _safe_load_json_dict(plan.source_path)
    if source_existing is None:
        # Source vanished between plan and apply (rare). Source is already
        # clean from this command's POV; leave as-is.
        return result

    sigs_to_drop = {m.signature for m in applicable}
    source_new = _strip_source_inner_entries(source_existing, sigs_to_drop)
    if source_new != source_existing:
        _write_json(plan.source_path, source_new)
        result.source_written = True

    return result


# ── Reporting ───────────────────────────────────────────────────────


def format_plan_summary(plan: MigratePlan) -> str:
    """One-line summary used by the CLI summary footer."""
    if not plan.moves:
        return "0 entries to migrate (source is already clean)."
    moves_apply = sum(1 for m in plan.moves if not m.conflict_at_target)
    moves_already = sum(1 for m in plan.moves if m.already_at_target and not m.conflict_at_target)
    moves_conflict = sum(1 for m in plan.moves if m.conflict_at_target)
    parts: list[str] = []
    fresh = moves_apply - moves_already
    if fresh:
        parts.append(f"{fresh} to add at target")
    if moves_already:
        parts.append(f"{moves_already} already at target (source clean-up only)")
    if moves_conflict:
        parts.append(f"{moves_conflict} skipped (conflict)")
    return ", ".join(parts) if parts else "0 entries to migrate."


__all__ = [
    "MigrateMove",
    "MigratePlan",
    "MigrateResult",
    "apply_migration",
    "format_plan_summary",
    "plan_migration",
]
