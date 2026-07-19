"""ADR-0030 PR-C — explicit Pull apply engine (prepare / commit).

The write half of the Preview/Pull model. :func:`preview_pull` shows what a
Pull *would* land; this module *lands* it, enforcing the §5 source-conflict
refusal **in the engine** (not just the CLI) and writing the exact bytes that
were judged.

Two phases so a human approval in between cannot be raced:

* :func:`prepare_pull` — collects candidates **once** (via the shared
  ``pull_preview._collect``), runs the §5 decision, a Gate A CHECK, and the
  pre-lock preflights, and returns a frozen :class:`PullPlan` carrying the
  captured bytes the caller will confirm — or a typed refusal
  :class:`PullApplyResult`.
* :func:`commit_pull` — takes the confirmed plan, acquires the canonical
  name-lock, runs the AUDITED Gate A on the captured bytes, and writes them
  **unchanged**. No second collection, no re-read of the runtime file: the
  bytes the user approved are the bytes written.

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
import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._canonical_txn import (
    SnapshotError,
    canonical_sidecar_lock,
    write_canonical_locked,
)
from memtomem.context._gate_a import GateABlocked, GateStatus, apply_gate_a
from memtomem.context._names import Layout
from memtomem.context._runtime_targets import resolve_import_runtimes
from memtomem.context.pull_preview import (
    PullCandidate,
    _Cand,
    _collect,
    _group_and_resolve,
    _runtime_candidate_path,
    iter_skill_payload_files,
)
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir

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
    gate_status: GateStatus | None = None


class _GateBlock(Exception):
    """Internal: raised by the audited Gate A pre_write to abort the write.

    Carries the :class:`GateABlocked` so ``commit_pull`` maps it to a
    ``gate_blocked`` result. Never escapes this module.
    """

    def __init__(self, blocked: GateABlocked, hint_bypassable: bool) -> None:
        super().__init__(blocked.code)
        self.blocked = blocked
        self.hint_bypassable = hint_bypassable


# ── digests ────────────────────────────────────────────────────────────────


def _payload_digest(payload: list[tuple[str, bytes]]) -> str:
    """Order-independent SHA-256 over a ``(relpath, bytes)`` payload (skills).

    Length-prefixed framing so no ``(rel, data)`` pair can be confused with a
    different split of the same bytes.
    """
    h = hashlib.sha256()
    for rel, data in sorted(payload):
        rel_b = rel.encode("utf-8")
        h.update(len(rel_b).to_bytes(8, "big"))
        h.update(rel_b)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


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
        return _payload_digest(store_payload)
    # Single-file agents/commands: bytes of the one payload entry.
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


def _run_gate_a(
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
) -> None:
    """AUDITED Gate A over the captured surface — raises :class:`_GateBlock`.

    ``record_outcome=True`` (inside ``apply_gate_a``), so the scan records
    exactly once, at write time. Scans every file of the captured full copier
    surface (parity with ``preview_pull``'s ``_gate_landing``); the first
    blocked file aborts.
    """
    for rel, data in captured:
        # For a single-file agent/command ``rel`` is ""; ``src`` is the runtime
        # source file. For a skill tree ``src`` reconstructs each source file
        # under the runtime skill dir for audit fidelity.
        src = src_root if rel == "" else src_root / rel
        outcome = apply_gate_a(
            content_text=data.decode("utf-8", errors="replace"),
            src=src,
            scope=scope,
            force_unsafe_import=force_unsafe_import,
            audit_context=_audit_context(kind, runtime, name, src, dst),
            message_kind=_MESSAGE_KIND[kind],
            imported_so_far=0,
            surface=surface,
            raise_project_shared=False,
        )
        if isinstance(outcome, GateABlocked):
            bypassable = outcome.code == skip_codes.PRIVACY_BLOCKED and scope != "project_shared"
            raise _GateBlock(outcome, hint_bypassable=bypassable)


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
    """
    resolved_root = project_root.resolve() if project_root is not None else None
    if source_runtime is not None:
        # Validate eligibility up front (export-only / unknown → ValueError).
        resolve_import_runtimes(kind, source_runtime)

    collected = _collect(kind, name, scope=scope, project_root=resolved_root)
    distinct, ambiguous, _auto = _group_and_resolve(collected.working)
    working = collected.working
    importable = [c for c in working if c.importable]
    has_landing_error = any(c.content_status == "landing_error" for c in importable)

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
                _source_conflict_reason(kind, name, working, has_landing_error),
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

    # Gate A pre-check from the already-computed (non-recording) status, so a
    # blocked pull is refused BEFORE any confirmation prompt (R1/R2 Minor 2).
    gate = selected.gate_status
    if gate == "blocked":
        return _refuse(
            "gate_blocked",
            skip_codes.PRIVACY_BLOCKED,
            f"Gate A blocked the pull into scope='{scope}' — no force bypass for "
            f"project_shared (ADR-0011 §5). Remove the secret or pull into user / project_local.",
            gate_status="blocked",
            force_bypassable=False,
        )
    if gate == "requires_unsafe_confirmation" and not force_unsafe_import:
        return _refuse(
            "gate_blocked",
            skip_codes.PRIVACY_BLOCKED,
            f"Gate A flagged the '{selected.runtime}' copy — pass --force-unsafe-import "
            f"to pull it into scope='{scope}' after review.",
            gate_status="requires_unsafe_confirmation",
            force_bypassable=True,
        )

    # Store-present preflight (advisory — commit re-checks authoritatively).
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
                f"the Store already has {kind}/{name}; pass --overwrite to replace it "
                f"(the current canonical is snapshotted first).",
            )

    duplicates = tuple(
        c.runtime
        for c in working
        if c is not selected
        and c.importable
        and c.landing_group is not None
        and c.landing_group == selected.landing_group
    )
    assert selected.landing_full is not None  # computable candidates only reach here
    return PullPlan(
        kind=kind,
        name=name,
        scope=scope,
        project_root=resolved_root,
        selected_runtime=selected.runtime,
        captured=tuple(selected.landing_full),
        overwrite=overwrite,
        duplicate_runtimes=duplicates,
        store_present=collected.store_present,
        expected_store_digest=_expected_digest(kind, collected.store_payload),
        gate_status=selected.gate_status,
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


def _source_conflict_reason(
    kind: ArtifactKind, name: str, working: list[_Cand], has_landing_error: bool
) -> str:
    groups: dict[int, list[str]] = {}
    unreadable: list[str] = []
    for c in working:
        if not c.importable:
            continue
        if c.content_status == "landing_error":
            unreadable.append(c.runtime)
        elif c.landing_group is not None:
            groups.setdefault(c.landing_group, []).append(c.runtime)
    parts = [f"{', '.join(rts)} (content #{gid + 1})" for gid, rts in sorted(groups.items())]
    msg = (
        f"multiple distinct contents would land for {kind}/{name}: "
        + "; ".join(parts)
        + " — pass --from <runtime> to choose."
    )
    if unreadable:
        msg += (
            f" (the {', '.join(unreadable)} copy could not be read; auto-selection "
            f"is off until you name a source.)"
        )
    return msg


# ── commit ───────────────────────────────────────────────────────────────────


def commit_pull(
    plan: PullPlan,
    *,
    force_unsafe_import: bool = False,
    surface: str = _SURFACE_DEFAULT,
    lock_timeout: float | None = None,
) -> PullApplyResult:
    """Write ``plan.captured`` under the canonical lock; audited Gate A first.

    ``lock_timeout=None`` blocks (the CLI default, Ctrl-C-able); the async
    Web/MCP surfaces pass a bound and map the ``lock_timeout`` result to a 503.
    """
    if plan.kind == "skills":
        return _commit_skills(
            plan,
            force_unsafe_import=force_unsafe_import,
            surface=surface,
            lock_timeout=lock_timeout,
        )
    return _commit_atomic(
        plan, force_unsafe_import=force_unsafe_import, surface=surface, lock_timeout=lock_timeout
    )


def _commit_atomic(
    plan: PullPlan, *, force_unsafe_import: bool, surface: str, lock_timeout: float | None
) -> PullApplyResult:
    """Commit an agents/commands pull — single captured file via the shared
    locked-write primitive (destination precondition + audited Gate A + write).
    """
    from memtomem.context._atomic_reverse import resolve_artifact_extract_target

    dir_filename = _dir_filename(plan.kind)
    canonical_root = canonical_artifact_dir(plan.kind, plan.scope, plan.project_root)
    captured_bytes = plan.captured[0][1]
    src = _runtime_candidate_path(
        plan.kind, plan.selected_runtime, plan.name, plan.scope, plan.project_root
    )

    def _resolve() -> tuple[Path, Layout]:
        return resolve_artifact_extract_target(
            canonical_root,
            plan.name,
            artifact_label=plan.kind,
            dir_filename=dir_filename,
            logger=logger,
        )

    def _pre_write() -> None:
        # Audited Gate A on the CAPTURED bytes, inside the lock, only when a
        # write is certain (write_canonical_locked calls this on the
        # created/overwritten branches) → records exactly once.
        assert src is not None
        dst_now, _layout = _resolve()
        _run_gate_a(
            plan.kind,
            plan.name,
            plan.selected_runtime,
            plan.scope,
            src,
            dst_now,
            plan.captured,
            force_unsafe_import=force_unsafe_import,
            surface=surface,
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
            pre_write=_pre_write,
        )
    except _GateBlock as gb:
        return _gate_blocked_result(plan, gb)
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

    return _map_write_outcome(plan, outcome, dst, layout)


def _commit_skills(
    plan: PullPlan, *, force_unsafe_import: bool, surface: str, lock_timeout: float | None
) -> PullApplyResult:
    """Commit a skills pull — new-only, captured tree staged then exclusively
    promoted, all under the canonical name-lock (skills do not use
    ``write_canonical_locked``).
    """
    from memtomem.context.skills import (
        _promote_race_conflict,
        _promote_staging,
        _reap_stale_internal_dirs,
        _target_conflict,
    )

    canonical_root = canonical_artifact_dir("skills", plan.scope, plan.project_root)
    dst = canonical_root / plan.name

    try:
        with canonical_sidecar_lock(canonical_root, plan.name, timeout=lock_timeout):
            _reap_stale_internal_dirs(dst)

            # Destination precondition (R3/R4 Major): the state the user
            # previewed must still hold.
            present = dst.is_dir()
            actual_digest: str | None = None
            if present:
                try:
                    actual_digest = _payload_digest(iter_skill_payload_files(dst))
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

            # Audited Gate A on the captured tree, before staging.
            src = _runtime_candidate_path(
                "skills", plan.selected_runtime, plan.name, plan.scope, plan.project_root
            )
            assert src is not None
            try:
                _run_gate_a(
                    "skills",
                    plan.name,
                    plan.selected_runtime,
                    plan.scope,
                    src.parent,  # the runtime skill dir
                    dst,
                    plan.captured,
                    force_unsafe_import=force_unsafe_import,
                    surface=surface,
                )
            except _GateBlock as gb:
                return _gate_blocked_result(plan, gb)

            staging: Path | None = None
            try:
                staging = _stage_captured_tree(plan.captured, dst)
                _promote_staging(staging, dst, replace_existing=False)
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
                if staging is not None and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
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
            f"the Store already has {plan.kind}/{plan.name}; pass --overwrite to replace it.",
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


def _gate_blocked_result(plan: PullPlan, gb: _GateBlock) -> PullApplyResult:
    return PullApplyResult(
        status="gate_blocked",
        kind=plan.kind,
        name=plan.name,
        scope=plan.scope,
        reason=(
            f"Gate A blocked the pull: {gb.blocked.hits_count} privacy pattern hit(s)"
            f"{gb.blocked.hint}"
        ),
        reason_code=gb.blocked.code,
        selected_runtime=plan.selected_runtime,
        gate_hits=gb.blocked.hits_count,
        force_bypassable=gb.hint_bypassable,
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
