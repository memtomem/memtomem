"""Context gateway — backend Sync All with a per-phase report (ADR-0024, #1278).

One mutator: ``POST /api/context/sync-all`` — the backend counterpart of
the dashboard's front-end Sync All orchestration (``ctx-sync-all-btn``
in ``context-gateway.js``, which POSTs the five per-type sync routes
sequentially). ADR-0021 §"Sync orchestration" deferred this endpoint;
ADR-0024 resolves that deferral and records the contracts:

- **One outer lock window.** ``_gateway_lock`` is a non-reentrant
  ``_LoopLocalLock``, so this handler never calls the per-type *route
  handlers* (each acquires the lock). It acquires the lock ONCE and runs
  the five lock-free ``_sync_*_core`` helpers sequentially inside it, in
  the front-end's phase order. Each core keeps its standalone execution
  mode (skills/settings offload to a worker thread for their
  cross-process file locks; commands/agents/mcp-servers stay direct
  calls — see ``_sync_skills_core``).
- **Per-phase report, NOT cross-type all-or-nothing.** A failed phase is
  recorded and the run proceeds — skills use staging-dir promotion and
  the settings / MCP-server result shapes don't fit one atomic engine,
  so there is no cross-type snapshot to roll back to; per-type
  ``project_shared`` atomicity is preserved by the fail-fast phase
  inside ``context._sync_atomic``. (The front-end orchestrator stops at
  the first failure instead; ADR-0024 records the divergence — "effect
  parity" with it is a success-path property.)
- **Phase entries preserve the native per-type bodies verbatim** (the
  exact dict the standalone route returns: ``generated`` / ``dropped`` /
  ``skipped`` / ``canonical_root``, settings' ``results`` +
  ``duplicate_tier_warnings``), plus ``type`` and ``status``. Skip-row
  classification (the benign-code allowlist) deliberately stays
  client-side (#1262): unknown skip codes must stay loud by default, so
  the server reports raw ``reason_code`` rows and never collapses them
  into a verdict.
- **Failure carriers differ by phase kind.** Artifact phases fail by
  exception → ``status: "failed"`` + an ``error`` envelope
  (``{error_kind, message, reason_code?, http_status, …}``, the
  ADR-0023 §10 vocabulary). The settings phase refuses *in-band* (per
  result row, the ``_confirm.py`` hold-out), so its phase status is a
  severity roll-up of the rows — ``error``/``aborted`` → ``failed``
  (rows embedded, no ``error`` key), ``needs_confirmation`` →
  ``needs_confirmation`` — mirroring the JS ladder.
- **Tier policy.** ``project_shared`` only: ``project_local`` has no
  runtime fan-out (ADR-0011 §3) and the user tier is rejected because
  Sync All stays a project-tier action (#1263; the dashboard blocks the
  button there, MCP-server sync is project_shared-only, and the
  per-type routes remain the user-tier path with their host-write
  confirm). The eligibility 409 (``sync_paused`` / ``sync_not_enrolled``)
  fires from ``resolve_writable_scope_root`` before the tier gate.
- **No request body.** Phases run with engine defaults, mirroring the
  front-end's body-less phase POSTs: ``on_drop="warn"`` (strict-drop API
  callers use the per-type routes), and no ``allow_host_writes`` valve —
  on ``project_shared`` every settings generator targets inside the
  project root, so the in-band host-write gate cannot fire here
  (ADR-0024 §Alternatives records both).

Second mutator: ``POST /api/context/sync-all-projects`` (ADR-0025,
#1279) — the cross-project batch. It loops the SAME five-phase run over
every discovered project scope, producing a per-project × per-phase
report. Contracts on top of the single-project route's:

- **Batch reports, never refuses.** Where the single route 409s on an
  ineligible explicit selector, the batch emits a ``skipped`` project
  entry with a ``reason_code`` (``sync_paused`` / ``sync_not_enrolled``
  / ``missing_root`` / ``stale_project``) and proceeds. ``stale_project``
  (root exists, no ``.memtomem/``) is batch-only: bulk-syncing a tree the
  user never initialized would at best no-op every phase and at worst
  seed bookkeeping; the per-type single routes stay ungated on stale.
- **Per-project lock window.** The ``_gateway_lock`` +
  ``asyncio.timeout(300)`` window wraps ONE project's five phases and is
  released between projects — project stores are independent, so
  cross-project interleaving by other mutators is harmless, while a
  batch-wide window would starve them for N×5 phases. A project-level
  timeout / unexpected error converts to a ``failed`` project entry
  (completed phases kept — their writes are real; ``summary`` present
  iff the phase loop completed) and the batch proceeds. There is
  deliberately NO batch-level timeout: it would discard completed
  projects' reports mid-flight.
- **Tier policy unchanged** — ``project_shared`` only, same two 400s.
- Version snapshots carry ``surface="web_context_sync_all_projects"``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from memtomem.config import TargetScope
from memtomem.context.projects import ProjectScope, sync_skip_reason
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_agents import _sync_agents_core
from memtomem.web.routes.context_commands import _sync_commands_core
from memtomem.web.routes.context_gateway import _classify_exception, _redact_message
from memtomem.web.routes.context_mcp_servers import _sync_mcp_servers_core
from memtomem.web.routes.context_projects import _discover_for, resolve_writable_scope_root
from memtomem.web.routes.context_skills import _sync_skills_core
from memtomem.web.routes.context_transfer import _error
from memtomem.web.routes.settings_sync import _sync_settings_core

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-sync-all"])

#: Phase execution order — pinned to the front-end orchestrator's
#: ``_CTX_SYNC_PHASES`` (context-gateway.js) so the dashboard switchover
#: (follow-up) keeps the user-visible progression.
_SYNC_ALL_PHASES: tuple[str, ...] = (
    "skills",
    "commands",
    "agents",
    "mcp-servers",
    "settings",
)

#: Audit attribution forwarded to the versioning layer via the cores'
#: ``surface`` parameter — version snapshots name the actual orchestrator
#: instead of impersonating the standalone per-type routes.
_SYNC_ALL_SURFACE = "web_context_sync_all"

#: Same role for the cross-project batch (ADR-0025): snapshots taken
#: during a batch run name the batch, not the single-project orchestrator.
_SYNC_ALL_PROJECTS_SURFACE = "web_context_sync_all_projects"

#: One outer window = five sequential phases × the standalone routes' 60s
#: budget. The engine-internal cross-process lock budgets
#: (``_SKILLS_LOCK_BUDGET_S`` / ``_SETTINGS_LOCK_BUDGET_S``, 30s) stay far
#: below it, so a timed-out request cannot orphan a worker thread that
#: writes after the 503 went out (#1145 shape).
_SYNC_ALL_TIMEOUT_S = 300


def _phase_error_envelope(exc: HTTPException) -> dict[str, Any]:
    """ADR-0023 §10 object envelope for a failed phase, embedded in the report.

    ``SyncPhaseError`` carries ``error_kind`` / ``reason_code`` from the
    core's translation table; a plain ``HTTPException`` (none today)
    falls back to ``internal``. Dict details (strict-drop's
    ``{reason_code, message, generated}``) keep their extra keys so the
    partial fan-out stays visible inside the phase entry.
    """
    envelope: dict[str, Any] = {
        "error_kind": getattr(exc, "error_kind", None) or "internal",
        "http_status": exc.status_code,
    }
    detail = exc.detail
    if isinstance(detail, dict):
        envelope["message"] = str(detail.get("message", ""))
        envelope.update({k: v for k, v in detail.items() if k != "message"})
    else:
        envelope["message"] = str(detail)
    reason_code = getattr(exc, "reason_code", None)
    if reason_code is not None:
        envelope.setdefault("reason_code", reason_code)
    return envelope


def _settings_severity(results: list[dict]) -> str:
    """Roll the per-result settings statuses up to one phase status.

    Mirrors the front-end severity ladder (``context-gateway.js`` Sync
    All settings leg): any ``error`` or ``aborted`` row → ``failed``;
    else any ``needs_confirmation`` row → ``needs_confirmation``; else
    ``ok`` (``ok``/``skipped`` rows). Per-result granularity stays in
    the embedded ``results``.
    """
    statuses = {r.get("status") for r in results}
    if "error" in statuses or "aborted" in statuses:
        return "failed"
    if "needs_confirmation" in statuses:
        return "needs_confirmation"
    return "ok"


async def _run_phase(
    phase_type: str,
    project_root: Path,
    target_scope: TargetScope,
    *,
    surface: str = _SYNC_ALL_SURFACE,
) -> dict[str, Any]:
    """Run one per-type core and shape its phase entry; never raises HTTP.

    ``except Exception`` (not ``BaseException``) keeps the outer
    ``asyncio.timeout`` cancellation propagating; an unexpected engine
    error (``OSError`` mid fan-out, …) fails only ITS phase — a bare
    propagate would 500 the whole request and discard the completed
    phases' report. Classification + redaction reuse the overview error
    taxonomy. ``surface`` flows to the version-snapshot audit trail —
    the cross-project batch passes its own identity.
    """
    try:
        if phase_type == "skills":
            native = await _sync_skills_core(project_root, target_scope, surface=surface)
        elif phase_type == "commands":
            native = await _sync_commands_core(project_root, target_scope, surface=surface)
        elif phase_type == "agents":
            native = await _sync_agents_core(project_root, target_scope, surface=surface)
        elif phase_type == "mcp-servers":
            native = await _sync_mcp_servers_core(project_root)
        else:  # settings
            # No host-write valve: every settings generator's project_shared
            # target lives inside the project root (resolve_scope_path and
            # siblings), so the in-band needs_confirmation gate cannot fire
            # on the only tier this route accepts. It stays defensive — a
            # future outside-root target surfaces as a needs_confirmation
            # row + phase status, and the standalone settings route (which
            # does take allow_host_writes) is the completion path.
            native = await _sync_settings_core(project_root, target_scope)
    except HTTPException as exc:
        return {"type": phase_type, "status": "failed", "error": _phase_error_envelope(exc)}
    except Exception as exc:
        logger.error("sync-all %s phase failed: %s", phase_type, exc, exc_info=True)
        return {
            "type": phase_type,
            "status": "failed",
            "error": {
                "error_kind": _classify_exception(exc),
                "message": _redact_message(str(exc)),
                "http_status": 500,
            },
        }
    if phase_type == "settings":
        return {"type": phase_type, "status": _settings_severity(native["results"]), **native}
    return {"type": phase_type, "status": "ok", **native}


def _summarize(phases: list[dict[str, Any]]) -> dict[str, Any]:
    """Run-level roll-up: per-status counts + artifact write/skip totals.

    ``status``: ``ok`` (every phase ok) / ``failed`` (every phase failed)
    / ``partial`` (anything else — including a needs_confirmation-only
    run, which is incomplete until confirmed). The totals count rows in
    the phase-native ``generated`` / ``skipped`` lists (the settings
    phase carries neither); they are COUNTS, not classifications — skip
    severity stays with the per-row ``reason_code`` (#1262).
    """
    counts = {"ok": 0, "failed": 0, "needs_confirmation": 0}
    generated_total = 0
    skipped_total = 0
    for phase in phases:
        counts[phase["status"]] += 1
        generated_total += len(phase.get("generated", ()))
        skipped_total += len(phase.get("skipped", ()))
    if counts["failed"] == len(phases):
        status = "failed"
    elif counts["failed"] or counts["needs_confirmation"]:
        status = "partial"
    else:
        status = "ok"
    return {
        "status": status,
        **counts,
        "generated_total": generated_total,
        "skipped_total": skipped_total,
    }


def _reject_ineligible_tier(target_scope: TargetScope) -> None:
    """Shared tier gate for both sync-all routes (ADR-0024 §4 / ADR-0025).

    One source for the two 400 literals — the per-route tests pin them by
    full equality, and the batch deliberately refuses the same tiers for
    the same reasons (a user-tier batch would also multiply the per-type
    host-write confirmation bypass by N projects).
    """
    if target_scope == "project_local":
        raise _error(
            400,
            "validation",
            "Sync all is supported on the project_shared tier; project_local "
            "is a draft tier with no runtime fan-out (ADR-0011 §3).",
        )
    if target_scope == "user":
        raise _error(
            400,
            "validation",
            "Sync all is a project-tier action (#1263); sync skills, commands, "
            "agents, and settings individually on the user tier (mcp-servers "
            "sync is project_shared-only).",
        )


@router.post("/context/sync-all")
async def sync_all_context(
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to fan out. Only project_shared is "
            "supported: project_local has no runtime fan-out (ADR-0011 §3) "
            "and Sync All stays a project-tier action (#1263) — sync "
            "artifact types individually on the user tier."
        ),
    ),
) -> dict:
    """Run every per-type sync phase under one gateway-lock window.

    Returns HTTP 200 with ``{phases: [...], summary: {...}}`` whenever the
    phases ran — mixed results cannot map to one HTTP code, so per-phase
    outcomes live in the report (see the module docstring for the phase
    shape). Non-2xx happens only before/around the run: 409 eligibility
    (from the resolver dependency), 400 tier gate, 503 outer timeout —
    all on the ADR-0023 §10 envelope except the resolver's pre-existing
    409 shape (B-1 #1284 retrofits it).
    """
    _reject_ineligible_tier(target_scope)
    phases: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(_SYNC_ALL_TIMEOUT_S):
            async with _gateway_lock:
                for phase_type in _SYNC_ALL_PHASES:
                    phases.append(await _run_phase(phase_type, project_root, target_scope))
    except TimeoutError:
        raise _error(
            503,
            "busy",
            "Sync all timed out — another sync may be in progress. Phases "
            "that completed before the timeout have already written their "
            "runtime files; re-run to converge.",
        )
    return {"phases": phases, "summary": _summarize(phases)}


# ── Cross-project batch (ADR-0025, #1279) ────────────────────────────────


#: Web remediation prose per ``sync_skip_reason`` code. The CODE derivation
#: is shared with the CLI (``context.projects.sync_skip_reason``) so the
#: surfaces cannot drift on which scopes execute; the messages are
#: surface-appropriate (portal here, ``mm`` verbs on the CLI).
_SKIP_MESSAGES: dict[str, str] = {
    "missing_root": (
        "project root no longer exists on disk; re-register it or remove "
        "the entry from the Projects portal."
    ),
    "sync_paused": (
        "sync enrollment is paused for this project; resume it from the "
        "Projects portal to include it in batch sync."
    ),
    "sync_not_enrolled": (
        "discovery-only project (never enrolled); register it from the "
        "Projects portal to include it in batch sync."
    ),
    "stale_project": (
        "project has no .memtomem/ store (never initialized); run "
        "`mm context init` there before syncing."
    ),
}


def _project_skip(scope: ProjectScope) -> tuple[str, str] | None:
    """``(reason_code, message)`` when *scope* must be skipped, else None."""
    code = sync_skip_reason(scope)
    if code is None:
        return None
    return code, _SKIP_MESSAGES[code]


def _summarize_projects(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch-level roll-up over the per-project entries.

    ``status``: ``failed`` (every EXECUTED project failed) / ``ok`` (no
    executed project failed or partial — an all-skipped batch is ``ok``
    with ``executed: 0`` visible: skipping paused projects is the designed
    outcome, and unattended callers need the no-op run to read as success)
    / ``partial`` otherwise. Totals aggregate the embedded phase lists —
    counts, not classifications (#1262); a failed project's completed
    phases still count (their writes are real).
    """
    counts = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0}
    generated_total = 0
    skipped_rows_total = 0
    for entry in entries:
        counts[entry["status"]] += 1
        for phase in entry.get("phases", ()):
            generated_total += len(phase.get("generated", ()))
            skipped_rows_total += len(phase.get("skipped", ()))
    executed = len(entries) - counts["skipped"]
    if executed and counts["failed"] == executed:
        status = "failed"
    elif counts["failed"] or counts["partial"]:
        status = "partial"
    else:
        status = "ok"
    return {
        "status": status,
        "projects_total": len(entries),
        "executed": executed,
        **counts,
        "generated_total": generated_total,
        "skipped_rows_total": skipped_rows_total,
    }


@router.post("/context/sync-all-projects")
async def sync_all_projects_context(
    request: Request,
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to fan out in every project. Only "
            "project_shared is supported — same gates as /context/sync-all."
        ),
    ),
) -> dict:
    """Run the five-phase sync for every eligible discovered project.

    Returns HTTP 200 with ``{projects: [...], summary: {...}}`` whenever
    the loop ran; non-2xx only for the pre-run tier gate (400) and CSRF
    (403). Per-project entries embed the single-project report verbatim
    (``phases`` + ``summary``) under the project identity; ineligible
    scopes become ``skipped`` entries (batch reports, never refuses —
    the single route's eligibility 409 has no batch analogue). One
    project's failure — engine error, lock timeout — converts to a
    ``failed`` entry and the loop proceeds (``error`` envelope attached,
    completed ``phases`` kept, ``summary`` present iff the phase loop
    completed). Lock + timeout window is PER PROJECT — see the module
    docstring.
    """
    _reject_ineligible_tier(target_scope)
    entries: list[dict[str, Any]] = []
    for scope in _discover_for(request):
        base: dict[str, Any] = {
            "project_scope_id": scope.scope_id,
            "label": scope.label,
            "root": str(scope.root) if scope.root is not None else None,
        }
        skip = _project_skip(scope)
        if skip is not None:
            reason_code, message = skip
            entries.append(
                {**base, "status": "skipped", "reason_code": reason_code, "message": message}
            )
            continue
        assert scope.root is not None  # _project_skip returned a missing_root row otherwise
        phases: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(_SYNC_ALL_TIMEOUT_S):
                async with _gateway_lock:
                    for phase_type in _SYNC_ALL_PHASES:
                        phases.append(
                            await _run_phase(
                                phase_type,
                                scope.root,
                                target_scope,
                                surface=_SYNC_ALL_PROJECTS_SURFACE,
                            )
                        )
        except TimeoutError:
            entries.append(
                {
                    **base,
                    "status": "failed",
                    "phases": phases,
                    "error": {
                        "error_kind": "busy",
                        "http_status": 503,
                        "message": (
                            "sync timed out for this project — another sync may "
                            "be in progress. Phases that completed before the "
                            "timeout have already written their runtime files; "
                            "re-run to converge."
                        ),
                    },
                }
            )
            continue
        except Exception as exc:  # defensive — _run_phase contains engine errors
            logger.error(
                "sync-all-projects %s failed outside the phase runner: %s",
                scope.scope_id,
                exc,
                exc_info=True,
            )
            entries.append(
                {
                    **base,
                    "status": "failed",
                    "phases": phases,
                    "error": {
                        "error_kind": _classify_exception(exc),
                        "message": _redact_message(str(exc)),
                        "http_status": 500,
                    },
                }
            )
            continue
        project_summary = _summarize(phases)
        entries.append(
            {
                **base,
                "status": project_summary["status"],
                "phases": phases,
                "summary": project_summary,
            }
        )
    return {"projects": entries, "summary": _summarize_projects(entries)}
