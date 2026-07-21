"""ADR-0030 PR-C — explicit Pull apply engine (prepare / commit).

The write half of the Preview/Pull model. :func:`preview_pull` shows what a
Pull *would* land; this module *lands* it, enforcing the §5 source-conflict
refusal **in the engine** (not just the CLI) and writing the exact bytes that
were judged.

Two phases so a human approval in between cannot be raced:

* :func:`prepare_pull` — collects candidates **once** (via the shared
  ``pull_preview._collect``), runs the §5 decision, the pre-lock preflights,
  and ONE aggregate Gate A decision over the captured bytes, and returns a
  frozen :class:`PullPlan` carrying those bytes — or a typed refusal
  :class:`PullApplyResult`. A Gate A block (including a forced ``project_shared``
  bypass attempt) is recorded and refused here, before any confirmation prompt;
  a pass/bypassed decision travels in the plan, its privacy counter deferred.
* :func:`commit_pull` — takes the confirmed plan, acquires the canonical
  name-lock, re-checks the destination precondition, writes the captured bytes
  **unchanged**, and records the deferred pass/bypassed counter once — only on
  a successful write (the ``mem_batch_add`` "no pass on a rejected pull"
  invariant). No second collection, no re-read of the runtime file: the bytes
  the user approved are the bytes written.

Why the §5 decision runs OUTSIDE the lock (validated in design review): the
divergence §5 guards against is a runtime-SOURCE race, and runtime writers are
permanently unlockable (ADR-0030 §6). ``canonical_sidecar_lock`` guards the
canonical DESTINATION; extending it backward over the source read would protect
the wrong resource. The real guarantee is judged-bytes == written-bytes
(capture in ``_collect``, never re-read). Canonical-destination races are
handled under the lock by ``write_canonical_locked`` (re-resolve + the
``expected_state`` precondition + snapshot-first overwrite), so the residual is
a correct late refusal (``canonical_exists`` / ``plan_stale``) or a
correctly-snapshotted overwrite — never a wrong-content write or lost update.
Later runtime divergence is ordinary post-pull drift (the §1 probe's job).

Surfaces: the CLI ``mm context pull`` (PR-C) is the first consumer; the Web
Pull flow (PR-D) and MCP ``mem_context_pull`` (PR-H) reuse the same
result-coded contract.
"""

from __future__ import annotations

import hashlib
import errno
import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memtomem import privacy
from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._canonical_txn import (
    SnapshotError,
    canonical_sidecar_lock,
    write_canonical_locked,
)
from memtomem.context._gate_a import GateStatus
from memtomem.context._dir_swap import SwapRecoveryError, marker_owns_transient
from memtomem.context._names import Layout, validate_name
from memtomem.context._runtime_targets import resolve_import_runtimes
from memtomem.context.pull_preview import (
    PullCandidate,
    _Cand,
    _collect,
    _group_and_resolve,
    _runtime_candidate_path,
)
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir
from memtomem.context.skill_payload import iter_skill_payload_files, payload_digest

logger = logging.getLogger(__name__)

_SURFACE_DEFAULT = "cli_context_pull"

# Per-kind singular noun for Gate A messages / audit (mirrors the extract
# engines' ``message_kind``).
_MESSAGE_KIND = {"skills": "skill", "agents": "agent", "commands": "command"}


PullApplyStatus = Literal[
    "applied",  # write happened (created/overwritten) or a byte-identical no-op
    "source_conflict",  # §5: >1 distinct landing (or importable landing_error), no --from
    "nothing_importable",  # no importable+computable candidate / --from names an absent copy
    "selected_landing_error",  # the SELECTED --from candidate's bytes could not be computed
    "canonical_exists",  # dst present, no --overwrite
    "skills_overwrite_unsupported",  # skills dst present (tree snapshots deferred, PR-G)
    "snapshot_requires_dir_layout",  # agents/commands flat-layout overwrite refused
    "target_conflict",  # skills dst holds non-skill content
    "gate_blocked",  # Gate A refused (project_shared hard, or bypassable tier w/o force)
    "lock_timeout",  # canonical (or version) lock unavailable within the budget
    "snapshot_failed",  # pre-overwrite snapshot failed (fail-closed)
    "write_failed",  # unexpected OSError writing the captured bytes (e.g. ENOTSUP promote)
    "plan_stale",  # destination changed between prepare and commit
    "swap_recovery_pending",  # an interrupted directory swap needs an operator (ADR-0030 §10)
]


@dataclass(frozen=True)
class PullApplyResult:
    """Outcome of a Pull apply (``commit_pull``) or a ``prepare_pull`` refusal.

    Result-coded (not exception-raised) so the Web/MCP surfaces get a stable
    ``reason_code`` and the ``source_conflict`` payload (``candidates`` grouped
    by ``landing_group``) travels with it; the CLI prints ``reason`` verbatim.
    """

    status: PullApplyStatus
    kind: ArtifactKind
    name: str
    scope: TargetScope
    reason: str
    # None only for ``applied``; every refusal carries a stable code.
    reason_code: skip_codes.SkipCode | None = None
    # applied payload -------------------------------------------------------
    selected_runtime: str | None = None
    dst: Path | None = None
    layout: Layout | None = None
    write_outcome: str | None = None  # created / overwritten / identical
    # Other runtimes whose copy is byte-identical over the full surface — a
    # disclosure that the auto-selected priority-first source had duplicates.
    duplicate_runtimes: tuple[str, ...] = ()
    # refusal payload -------------------------------------------------------
    candidates: tuple[PullCandidate, ...] = ()  # source_conflict rendering
    distinct_landing_count: int = 0
    gate_status: GateStatus | None = None
    gate_hits: int | None = None
    force_bypassable: bool = False  # gate_blocked: True ⇒ --force-unsafe-import applies


@dataclass(frozen=True)
class PullPlan:
    """A prepared, confirmable Pull. Carries the CAPTURED bytes so ``commit_pull``
    writes exactly what the user approved — no re-read of the runtime file.
    """

    kind: ArtifactKind
    name: str
    scope: TargetScope
    # RESOLVED at prepare time so commit resolves the destination from this,
    # never the commit-time cwd (which would break plan identity).
    project_root: Path | None
    selected_runtime: str
    # The would-land FULL copier surface as sorted (posix_relpath, bytes).
    captured: tuple[tuple[str, bytes], ...]
    overwrite: bool
    duplicate_runtimes: tuple[str, ...]
    # Prepare-time destination state, re-checked under the lock at commit as the
    # ``expected_state`` precondition (present + content digest).
    store_present: bool
    expected_store_digest: str | None
    # The passed/bypassed Gate A decision, deferred so its privacy counter
    # records once, in commit, only when the write actually lands.
    gate: _GateProceed | None = None
    # The ingress surface from prepare, carried so commit records the deferred
    # counter under the SAME surface (a Web/MCP prepare must not be attributed
    # to the CLI at commit time).
    surface: str = _SURFACE_DEFAULT


# ── digests ────────────────────────────────────────────────────────────────
#
# The skills tree digest itself lives in ``skill_payload.payload_digest`` — the
# canonical ADR-0030 §10 serialization, shared with the snapshot/version
# identity that PR-G3 adds. Only the kind dispatch below is local.


def _expected_digest(
    kind: ArtifactKind, store_payload: list[tuple[str, bytes]] | None
) -> str | None:
    """The prepare-time Store digest a commit re-checks under the lock.

    Agents/commands hash the single canonical file's bytes (matching
    ``_canonical_txn._single_file_digest``); skills hash the payload tree
    (matching the under-lock recompute in ``_commit_skills``).
    """
    if store_payload is None:
        return None
    if kind == "skills":
        return payload_digest(store_payload)
    # Single-file agents/commands: bytes of the one payload entry. ``_read_store``
    # guarantees a present (non-None) payload is non-empty for these kinds
    # (``[("", bytes)]``); guard it explicitly (not ``assert`` — stripped under
    # -O) so a future contract change fails loud here, not as a silent
    # IndexError.
    if not store_payload:
        raise RuntimeError("agents/commands store_payload must be non-empty when present")
    return hashlib.sha256(store_payload[0][1]).hexdigest()


# ── gate ────────────────────────────────────────────────────────────────────


def _audit_context(
    kind: ArtifactKind, runtime: str, name: str, src: Path, dst: Path
) -> dict[str, object]:
    """The per-kind Gate A audit dict, matching the extract engines' shapes."""
    if kind == "skills":
        return {"source_file": str(src), "skill_name": name, "kind": "skills"}
    if kind == "agents":
        return {"source": str(src), "target": str(dst), "kind": "agents", "agent_name": name}
    return {
        "source": str(src),
        "target": str(dst),
        "kind": "commands",
        "runtime": runtime,
        "command_name": name,
    }


@dataclass(frozen=True)
class _GateProceed:
    """Gate passed (clean, or a bypassed force) — the write may proceed.

    The privacy counter is NOT recorded here: a Pull that the user then
    declines, or that ``commit_pull`` refuses for a destination reason, must
    not leave a ``pass`` record (the ``mem_batch_add`` "no pass on rejected
    batch" invariant, ``privacy.enforce_write_guard`` docstring). Recording
    happens once, in ``commit_pull``, only when the write actually lands.
    """

    outcome: str  # "pass" | "bypassed"
    hits: int
    content_chars: int
    audit_context: dict[str, object]


@dataclass(frozen=True)
class _GateBlocked:
    """Gate refused the Pull — recorded HERE (a block is terminal, and a forced
    project_shared bypass ATTEMPT must be audited even before the confirm
    prompt)."""

    code: skip_codes.SkipCode
    hits: int
    force_bypassable: bool


def _evaluate_gate(
    kind: ArtifactKind,
    name: str,
    runtime: str,
    scope: TargetScope,
    src_root: Path,
    dst: Path,
    captured: tuple[tuple[str, bytes], ...],
    *,
    force_unsafe_import: bool,
    surface: str,
) -> _GateProceed | _GateBlocked:
    """Decide the aggregate Gate A outcome for the whole Pull (one transaction).

    Scans every file of the captured full copier surface WITHOUT recording
    (``record_outcome=False``), then commits ONE decision — the ``mem_batch_add``
    idiom, so a multi-file skill is one audit event, not one per file, and a
    declined/refused Pull leaves no ``pass`` record. A block (including a forced
    ``project_shared`` bypass attempt, which is a hard refusal) is recorded and
    audited immediately; a proceed (``pass`` / ``bypassed``) defers its record
    to ``commit_pull`` on a successful write.
    """
    results: list[privacy.WriteGuardResult] = []
    total_chars = 0
    for rel, data in captured:
        # Single-file agent/command: ``rel`` is "" and ``src`` is the runtime
        # source file; a skill tree reconstructs each source path for audit.
        src = src_root if rel == "" else src_root / rel
        text = data.decode("utf-8", errors="replace")
        total_chars += len(text)
        results.append(
            privacy.enforce_write_guard(
                text,
                surface=surface,
                force_unsafe=force_unsafe_import,
                scope=scope,
                audit_context=_audit_context(kind, runtime, name, src, dst),
                record_outcome=False,
            )
        )
    decisions = {r.decision for r in results}
    total_hits = sum(len(r.hits) for r in results)
    audit_ctx = _audit_context(kind, runtime, name, src_root, dst)
    # Severity order (mirrors enforce_write_guard's own branch order):
    # blocked_project_shared > blocked > bypassed > pass.
    if "blocked_project_shared" in decisions:
        privacy.record("blocked_project_shared", surface)
        privacy.emit_bypass_audit(
            surface=surface,
            content_chars=total_chars,
            hits=total_hits,
            audit_context={**audit_ctx, "blocked_scope": "project_shared"},
        )
        return _GateBlocked(skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED, total_hits, False)
    if "blocked" in decisions:
        privacy.record("blocked", surface)
        # A plain block is force-bypassable only on the non-git-tracked tiers;
        # project_shared has no bypass (ADR-0011 §5).
        return _GateBlocked(skip_codes.PRIVACY_BLOCKED, total_hits, scope != "project_shared")
    outcome = "bypassed" if "bypassed" in decisions else "pass"
    return _GateProceed(outcome, total_hits, total_chars, audit_ctx)


def _record_gate_success(gate: _GateProceed, surface: str) -> None:
    """Record the deferred proceed outcome once the write has landed."""
    privacy.record(gate.outcome, surface)
    if gate.outcome == "bypassed":
        privacy.emit_bypass_audit(
            surface=surface,
            content_chars=gate.content_chars,
            hits=gate.hits,
            audit_context=gate.audit_context,
        )


# ── prepare ──────────────────────────────────────────────────────────────────


def prepare_pull(
    kind: ArtifactKind,
    name: str,
    *,
    scope: TargetScope,
    project_root: Path | None,
    source_runtime: str | None = None,
    overwrite: bool = False,
    force_unsafe_import: bool = False,
    surface: str = _SURFACE_DEFAULT,
) -> PullPlan | PullApplyResult:
    """Collect once, decide §5, gate-check, preflight → a :class:`PullPlan` or refusal.

    Read-only. ``source_runtime`` must already be validated
    (``resolve_import_runtimes`` — the CLI does this to raise a crisp error);
    passing an ineligible one here raises ``ValueError``. Returns a
    :class:`PullPlan` to confirm-and-commit, or a :class:`PullApplyResult`
    (a refusal, or the ``applied``/``identical`` no-op when the Store already
    holds the selected content).

    ``name`` is validated HERE rather than being assumed validated by the
    caller. Every shipping surface does validate (``context_gateway`` routes,
    ``mem_context_pull``, the CLI), but this engine builds destination paths as
    ``canonical_root / name`` — and a separator-carrying name like
    ``../other/x`` yields a perfectly ordinary *basename* while pointing the
    parent somewhere else entirely, so a downstream basename check cannot catch
    it. The commit path recovers interrupted swaps, which renames and deletes
    trees; a defense that only holds while every caller remembers to validate
    is not a defense for that. :func:`commit_pull` re-checks for the same
    reason (a :class:`PullPlan` is constructible directly).
    """
    validate_name(name, kind=f"{kind[:-1]} name")
    resolved_root = project_root.resolve() if project_root is not None else None
    if source_runtime is not None:
        # Validate eligibility up front (export-only / unknown → ValueError).
        resolve_import_runtimes(kind, source_runtime)

    # scan_gate=False: prepare runs its own single audited Gate A decision over
    # the selected candidate (``_evaluate_gate``); scanning every candidate in
    # the collect would double-scan the selected payload (code-review Major).
    collected = _collect(kind, name, scope=scope, project_root=resolved_root, scan_gate=False)
    distinct, ambiguous, _auto = _group_and_resolve(collected.working)
    working = collected.working
    importable = [c for c in working if c.importable]

    def _refuse(
        status: PullApplyStatus,
        code: skip_codes.SkipCode,
        reason: str,
        **extra: object,
    ) -> PullApplyResult:
        return PullApplyResult(
            status=status,
            kind=kind,
            name=name,
            scope=scope,
            reason=reason,
            reason_code=code,
            **extra,  # type: ignore[arg-type]
        )

    selected: _Cand
    if source_runtime is None:
        computable = [c for c in importable if c.landing_full is not None]
        # ORDER (R3 Major 3): an importable landing_error makes the pull
        # ambiguous even as the sole candidate — check before nothing_importable.
        if ambiguous:  # distinct > 1 OR has_landing_error
            return _refuse(
                "source_conflict",
                skip_codes.SOURCE_CONFLICT,
                _source_conflict_reason(kind, name, working),
                candidates=tuple(_public(c) for c in working),
                distinct_landing_count=distinct,
            )
        if not computable:
            return _refuse(
                "nothing_importable",
                skip_codes.NOTHING_IMPORTABLE,
                f"no runtime has an importable {_MESSAGE_KIND[kind]} '{name}' to pull.",
            )
        # distinct == 1, not ambiguous → auto-select the priority-first of group 0.
        selected = next(c for c in working if c.landing_group == 0)
    else:
        sel = next((c for c in importable if c.runtime == source_runtime), None)
        if sel is None:
            return _refuse(
                "nothing_importable",
                skip_codes.NOTHING_IMPORTABLE,
                f"runtime '{source_runtime}' has no {_MESSAGE_KIND[kind]} '{name}' to pull.",
            )
        if sel.content_status == "landing_error":
            return _refuse(
                "selected_landing_error",
                skip_codes.SELECTED_LANDING_ERROR,
                f"the '{source_runtime}' copy could not be read: {sel.reason}",
            )
        selected = sel

    # Identical no-op (R4 Major 1) — the Store already holds this exact content,
    # so there is nothing to pull (whether or not --overwrite was passed). This
    # must precede the store-present / gate checks (a no-op writes nothing).
    if selected.content_status == "identical":
        return PullApplyResult(
            status="applied",
            kind=kind,
            name=name,
            scope=scope,
            reason=f"{kind}/{name} is already identical to the '{selected.runtime}' copy; nothing to pull.",
            selected_runtime=selected.runtime,
            write_outcome="identical",
        )

    # Store-present preflight (advisory — commit re-checks authoritatively).
    # Ordered BEFORE the Gate A scan so a canonical_exists / skills-overwrite
    # refusal never scans-and-records an ingress (mirrors the extract engines'
    # exists-before-gate order).
    if collected.store_present:
        if kind == "skills":
            # Skills overwrite lands with tree snapshots (ADR-0030 §10, PR-G);
            # until then only a `new` skills pull is allowed, whether or not
            # --overwrite was passed (--overwrite cannot help here).
            return _refuse(
                "skills_overwrite_unsupported",
                skip_codes.SKILLS_OVERWRITE_UNSUPPORTED,
                f"the Store already has skill '{name}'; overwriting skills lands with "
                f"tree snapshots (ADR-0030 §10), not yet supported — delete the "
                f"canonical skill first, then pull.",
            )
        if not overwrite:
            return _refuse(
                "canonical_exists",
                skip_codes.CANONICAL_EXISTS,
                f"the Store already has {kind}/{name}; a plain pull will not replace it "
                f"(an overwrite snapshots the current canonical first).",
            )

    # Gate A — ONE audited decision for the whole Pull, over the captured bytes
    # (so the bytes that were judged are the bytes committed). A block is
    # recorded here and refused before any confirmation prompt (R1/R2 Minor 2);
    # a proceed defers its counter record to commit-on-success.
    if selected.landing_full is None:
        # Unreachable: only computable candidates (landing_full set) reach here —
        # the §5 selection above rejects landing_error / not_importable rows.
        raise RuntimeError(f"selected candidate {selected.runtime} has no captured content")
    captured = tuple(selected.landing_full)
    src = _runtime_candidate_path(kind, selected.runtime, name, scope, resolved_root)
    src_root = src.parent if kind == "skills" else src  # type: ignore[union-attr]
    dst = canonical_artifact_dir(kind, scope, resolved_root) / name
    gate = _evaluate_gate(
        kind,
        name,
        selected.runtime,
        scope,
        src_root,
        dst,
        captured,
        force_unsafe_import=force_unsafe_import,
        surface=surface,
    )
    if isinstance(gate, _GateBlocked):
        if gate.force_bypassable:
            reason = (
                f"Gate A flagged the '{selected.runtime}' copy; it was not pulled into "
                f"scope='{scope}'."
            )
        else:
            reason = (
                # No tier retry is offered: ``project_local`` has no runtime
                # fan-out (ADR-0011 §3) and ``user`` resolves its sources from
                # ``$HOME``, so neither re-attempts THIS copy. "Remove the
                # secret" is the whole remediation, and it is surface-neutral.
                f"Gate A blocked the pull into scope='{scope}' — no force bypass for "
                f"project_shared (ADR-0011 §5). Remove the secret from the source "
                f"first."
            )
        return _refuse(
            "gate_blocked",
            gate.code,
            reason,
            gate_hits=gate.hits,
            force_bypassable=gate.force_bypassable,
        )

    duplicates = tuple(
        c.runtime
        for c in working
        if c is not selected
        and c.importable
        and c.landing_group is not None
        and c.landing_group == selected.landing_group
    )
    return PullPlan(
        kind=kind,
        name=name,
        scope=scope,
        project_root=resolved_root,
        selected_runtime=selected.runtime,
        captured=captured,
        overwrite=overwrite,
        duplicate_runtimes=duplicates,
        store_present=collected.store_present,
        expected_store_digest=_expected_digest(kind, collected.store_payload),
        gate=gate,
        surface=surface,
    )


def _public(cand: _Cand) -> PullCandidate:
    """Project the internal working row onto the public candidate row."""
    return PullCandidate(
        runtime=cand.runtime,
        content_status=cand.content_status,
        gate_status=cand.gate_status,
        importable=cand.importable,
        landing_group=cand.landing_group,
        override_warning=cand.override_warning,
        reason=cand.reason,
    )


def _source_conflict_reason(kind: ArtifactKind, name: str, working: list[_Cand]) -> str:
    groups: dict[int, list[str]] = {}
    unreadable: list[str] = []
    for c in working:
        if not c.importable:
            continue
        if c.content_status == "landing_error":
            unreadable.append(c.runtime)
        elif c.landing_group is not None:
            groups.setdefault(c.landing_group, []).append(c.runtime)
    if len(groups) >= 2:
        parts = [f"{', '.join(rts)} (content #{gid + 1})" for gid, rts in sorted(groups.items())]
        msg = (
            f"multiple distinct contents would land for {kind}/{name}: "
            + "; ".join(parts)
            + ". Name a source runtime."
        )
        if unreadable:
            msg += (
                f" (the {', '.join(unreadable)} copy could not be read; auto-selection "
                f"is off until you name a source.)"
            )
        return msg
    # Fewer than two computable groups: the ambiguity is driven purely by an
    # incomputable candidate (an unreadable copy might be the divergent one), so
    # auto-selection is off (fail-closed, ADR-0030 §5). Don't claim "multiple
    # distinct contents" when the distinct count is zero or one.
    return (
        f"the {', '.join(unreadable)} copy of {kind}/{name} could not be read, so "
        f"auto-selection is off. Name a source runtime explicitly."
    )


# ── commit ───────────────────────────────────────────────────────────────────


def commit_pull(
    plan: PullPlan,
    *,
    lock_timeout: float | None = None,
) -> PullApplyResult:
    """Write ``plan.captured`` under the canonical lock (Gate A already decided).

    The Gate A decision was made and any refusal audited in ``prepare_pull``;
    here we take the canonical name-lock, re-check the destination precondition,
    write the captured bytes, and record the passed/bypassed privacy counter
    (under ``plan.surface`` — the surface prepare used) once the write actually
    lands. ``lock_timeout=None`` blocks (the CLI default, Ctrl-C-able); the
    async Web/MCP surfaces pass a bound and map the ``lock_timeout`` result to a
    503.

    Re-validates ``plan.name`` before either branch derives a path from it.
    ``prepare_pull`` already did, but a :class:`PullPlan` is a plain frozen
    dataclass a caller can build directly, and everything below joins the name
    onto a canonical root — including the swap recovery the skills branch runs,
    which renames and removes directories. The check is cheap and it is the
    last point where an escaping name is still only a string.
    """
    validate_name(plan.name, kind=f"{plan.kind[:-1]} name")
    if plan.kind == "skills":
        return _commit_skills(plan, lock_timeout=lock_timeout)
    return _commit_atomic(plan, lock_timeout=lock_timeout)


def _commit_atomic(plan: PullPlan, *, lock_timeout: float | None) -> PullApplyResult:
    """Commit an agents/commands pull — single captured file via the shared
    locked-write primitive (destination precondition + snapshot-first write)."""
    from memtomem.context._atomic_reverse import resolve_artifact_extract_target

    dir_filename = _dir_filename(plan.kind)
    canonical_root = canonical_artifact_dir(plan.kind, plan.scope, plan.project_root)
    captured_bytes = plan.captured[0][1]

    def _resolve() -> tuple[Path, Layout]:
        return resolve_artifact_extract_target(
            canonical_root,
            plan.name,
            artifact_label=plan.kind,
            dir_filename=dir_filename,
            logger=logger,
        )

    try:
        outcome, dst, layout = write_canonical_locked(
            canonical_root,
            plan.name,
            captured_bytes,
            resolve_target=_resolve,
            overwrite=plan.overwrite,
            snapshot_note=f"pre-overwrite snapshot (pull from {plan.selected_runtime})",
            lock_timeout=lock_timeout,
            expected_state=(plan.store_present, plan.expected_store_digest),
        )
    except TimeoutError:
        return _lock_timeout_result(plan)
    except SnapshotError as exc:
        return _refusal_for(
            plan,
            "snapshot_failed",
            skip_codes.SNAPSHOT_FAILED,
            f"could not snapshot the current canonical before overwrite: {exc}",
        )
    except OSError as exc:
        return _refusal_for(
            plan, "write_failed", skip_codes.WRITE_FAILED, f"could not write the canonical: {exc}"
        )

    if outcome in ("created", "overwritten") and plan.gate is not None:
        # Record the passed/bypassed privacy counter once — only now that the
        # write has actually landed (never on a stale/exists refusal).
        _record_gate_success(plan.gate, plan.surface)
    return _map_write_outcome(plan, outcome, dst, layout)


def _commit_skills(plan: PullPlan, *, lock_timeout: float | None) -> PullApplyResult:
    """Commit a skills pull — new-only, captured tree staged then exclusively
    promoted, all under the canonical name-lock (skills do not use
    ``write_canonical_locked``; Gate A was decided in ``prepare_pull``)."""
    from memtomem.context.skills import (
        _promote_race_conflict,
        _promote_staging,
        _recover_and_reap_internal_dirs,
        _target_conflict,
    )

    canonical_root = canonical_artifact_dir("skills", plan.scope, plan.project_root)
    dst = canonical_root / plan.name

    try:
        with canonical_sidecar_lock(canonical_root, plan.name, timeout=lock_timeout):
            _recover_and_reap_internal_dirs(dst)

            # Destination precondition (R3/R4 Major): the state the user
            # previewed must still hold.
            present = dst.is_dir()
            actual_digest: str | None = None
            if present:
                try:
                    actual_digest = payload_digest(iter_skill_payload_files(dst))
                except OSError:
                    actual_digest = None
            if (present, actual_digest) != (plan.store_present, plan.expected_store_digest):
                return _refusal_for(
                    plan,
                    "plan_stale",
                    skip_codes.PLAN_STALE,
                    f"the Store copy of skill '{plan.name}' changed since the preview — re-run.",
                )

            conflict = _target_conflict(dst)
            if conflict is not None:
                return _refusal_for(
                    plan, "target_conflict", skip_codes.TARGET_CONFLICT, str(conflict)
                )

            staging: Path | None = None
            try:
                staging = _stage_captured_tree(plan.captured, dst)
                _promote_staging(staging, dst, replace_existing=False, reap_move_aside=True)
            except OSError as exc:
                if _promote_race_conflict(exc):
                    return _refusal_for(
                        plan, "target_conflict", skip_codes.TARGET_CONFLICT, str(exc)
                    )
                return _refusal_for(
                    plan,
                    "write_failed",
                    skip_codes.WRITE_FAILED,
                    f"could not install the captured skill tree: {exc}",
                )
            finally:
                # No .staging-* leak on any promote error (R3 Major 4). On the
                # success path the tree was renamed into dst, so this is a no-op.
                #
                # ADR-0030 §10 / _dir_swap §4.1: a transient a live swap marker
                # claims is removed only by the successful forward path or by
                # recovery — never by a caller's cleanup. This staging tree uses
                # the same ``.staging-<name>-<pid>-<hex>.tmp`` grammar the swap
                # does, so once a swap is in flight here, deleting it would
                # collapse the fail-closed "all three present" recovery row into
                # the "dst + old" row, whose action then deletes ``old`` — the
                # only copy of the artifact. Unreachable until the overwrite
                # transaction lands (nothing writes a marker yet); wired now
                # because the failure it prevents is silent and permanent.
                if staging is not None and staging.exists() and not marker_owns_transient(staging):
                    shutil.rmtree(staging, ignore_errors=True)

            # Write landed — record the passed/bypassed privacy counter once.
            if plan.gate is not None:
                _record_gate_success(plan.gate, plan.surface)
    except SwapRecoveryError as exc:
        # The prelude refused to resolve an interrupted directory swap
        # (ADR-0030 §10). Its own status, deliberately NOT ``target_conflict``
        # (nothing the user put at the destination caused this, and "remove it
        # and re-run" is the wrong advice) and not ``write_failed`` (nothing
        # was written — the artifact is wedged, and a 500 would report an
        # infrastructure failure for a state that needs an operator's eyes).
        # Must precede any broad ``OSError`` clause added later: this is one.
        return _refusal_for(
            plan, "swap_recovery_pending", skip_codes.SWAP_RECOVERY_PENDING, str(exc)
        )
    except TimeoutError:
        return _lock_timeout_result(plan)

    return _applied_result(plan, "created", dst, "dir")


def _stage_captured_tree(
    captured: tuple[tuple[str, bytes], ...], dst: Path, *, mode: int = 0o644
) -> Path:
    """Mirror the CAPTURED skill surface into a same-fs staging dir under
    ``dst.parent`` (for the exclusive ``_promote_staging`` rename).

    Writes the judged bytes — unlike ``skills._stage_skill``, which re-reads the
    runtime source and would reintroduce the TOCTOU this engine closes. Mode
    ``0o644`` matches the copier's content mode. Self-cleans a partial tree on
    any failure (mirrors ``_stage_skill``), so the caller's staging path is
    never leaked before it is returned.
    """
    from memtomem.context.skills import SKILL_MANIFEST
    from memtomem.context._atomic import atomic_write_bytes

    if not any(rel == SKILL_MANIFEST for rel, _ in captured):
        # A captured tree without a manifest is not a valid skill — refuse to
        # stage it rather than promote an invalid canonical (parity with
        # ``_stage_skill``; also guarded at capture time in ``_read_landing``).
        raise FileNotFoundError(f"captured skill missing {SKILL_MANIFEST}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{os.getpid()}-{secrets.token_hex(3)}"
    staging = dst.parent / f".staging-{dst.name}-{suffix}.tmp"
    if staging.exists():
        # ADR-0030 §4.1, same guard as ``skills._stage_skill``: a collision
        # needs pid reuse AND a 3-byte hex collision, but "the leftover tree is
        # from us" is the inference the marker retires — the directory swap
        # shares this basename grammar, so a claimed transient could be sitting
        # here, and deleting one collapses a recoverable state into one whose
        # next recovery removes the only copy. This function sits BETWEEN the
        # two sites that already got the guard (``_stage_skill`` and this
        # caller's own ``finally``) on the Pull commit path — the one path this
        # PR teaches to recover — so the asymmetry was at the most exposed
        # site (PR review).
        if marker_owns_transient(staging):
            raise SwapRecoveryError(
                errno.EBUSY,
                "a pending directory swap already claims this staging path; "
                "resolve the interrupted swap before staging again",
                str(staging),
            )
        shutil.rmtree(staging)
    try:
        staging.mkdir()
        for rel, data in captured:
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, data, mode)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


# ── result mapping ───────────────────────────────────────────────────────────


def _map_write_outcome(plan: PullPlan, outcome: str, dst: Path, layout: Layout) -> PullApplyResult:
    if outcome in ("created", "overwritten", "identical"):
        return _applied_result(plan, outcome, dst, layout)
    if outcome == "stale":
        return _refusal_for(
            plan,
            "plan_stale",
            skip_codes.PLAN_STALE,
            f"the Store copy of {plan.kind}/{plan.name} changed since the preview — re-run.",
        )
    if outcome == "exists":
        return _refusal_for(
            plan,
            "canonical_exists",
            skip_codes.CANONICAL_EXISTS,
            f"the Store already has {plan.kind}/{plan.name}; a plain pull will not replace it.",
        )
    if outcome == "flat_refused":
        return _refusal_for(
            plan,
            "snapshot_requires_dir_layout",
            skip_codes.SNAPSHOT_REQUIRES_DIR_LAYOUT,
            "cannot overwrite a flat-layout canonical (no version store to snapshot into) — "
            "run `mm context migrate` to convert it to directory layout first.",
        )
    raise RuntimeError(f"unhandled canonical write outcome: {outcome!r}")


def _applied_result(
    plan: PullPlan, write_outcome: str, dst: Path, layout: Layout
) -> PullApplyResult:
    return PullApplyResult(
        status="applied",
        kind=plan.kind,
        name=plan.name,
        scope=plan.scope,
        reason=f"pulled {plan.kind}/{plan.name} from {plan.selected_runtime} into {plan.scope}.",
        selected_runtime=plan.selected_runtime,
        dst=dst,
        layout=layout,
        write_outcome=write_outcome,
        duplicate_runtimes=plan.duplicate_runtimes,
    )


def _refusal_for(
    plan: PullPlan, status: PullApplyStatus, code: skip_codes.SkipCode, reason: str
) -> PullApplyResult:
    return PullApplyResult(
        status=status,
        kind=plan.kind,
        name=plan.name,
        scope=plan.scope,
        reason=reason,
        reason_code=code,
        selected_runtime=plan.selected_runtime,
    )


def _lock_timeout_result(plan: PullPlan) -> PullApplyResult:
    return _refusal_for(
        plan,
        "lock_timeout",
        skip_codes.LOCK_TIMEOUT,
        "another process held the canonical destination lock (or its version store) "
        "past the acquisition budget — re-run the pull to retry.",
    )


def _dir_filename(kind: ArtifactKind) -> str:
    if kind == "agents":
        from memtomem.context.agents import AGENT_DIR_FILENAME

        return AGENT_DIR_FILENAME
    from memtomem.context.commands import COMMAND_DIR_FILENAME

    return COMMAND_DIR_FILENAME
