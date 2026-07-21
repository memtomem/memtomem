"""Context gateway overview — aggregate sync status across all artifact types."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, field_validator

from memtomem.config import TargetScope
from memtomem.context import override as _override
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, validate_name
from memtomem.context._runtime_targets import IMPORT_SOURCE_RUNTIMES, resolve_import_runtimes
from memtomem.context.error_redact import redact_engine_reason, scrub_absolute_paths
from memtomem.context.projects import sync_skip_reason
from memtomem.context.pull_apply import PullApplyResult, PullPlan, commit_pull, prepare_pull
from memtomem.context.pull_preview import preview_pull, probe_pull_drift
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir
from memtomem.context.status import (
    ProjectStatus,
    classify_status,
    collect_project_status,
    summarize_diff_with_canonical,
    summarize_settings_statuses,
)
from memtomem.wiki.store import WikiStore
from memtomem.web.routes._confirm import host_write_gate, needs_confirmation_envelope
from memtomem.web.routes._errors import _classify_exception, _error, _redact_message
from memtomem.web.routes.context_projects import _discover_for, resolve_scope_root
from memtomem.web.schemas.context import (
    ContextOverviewResponse,
    ContextPullApplyNeedsConfirmation,
    ContextPullApplyResponse,
    ContextPullPreviewResponse,
    ContextRuntimesResponse,
    ContextStatusAllResponse,
    ContextStatusGlobalResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-gateway"])

# ``_classify_exception`` / ``_redact_message`` are re-exported from the
# ``_errors`` leaf (B-1 #1284) so ``context_transfer`` / ``context_sync_all``
# can keep importing them from this module without the old import cycle.


# The count derivations (``summarize_diff_statuses`` /
# ``summarize_diff_with_canonical`` / ``summarize_settings_statuses``) live
# in ``memtomem.context.status`` since #1280 so the CLI batch verb and every
# web aggregate share one keying rule.


def sanitize_diff_reason(message: str | None, project_root: Path) -> str | None:
    """Display-sanitize an engine diff-row ``reason`` for the wire (#1229 U7).

    Engine reasons are raw exception text with absolute source paths embedded
    in arbitrary message strings (not bare paths), so plain
    ``Path.relative_to`` doesn't apply. Delegate to the neutral context leaf so
    Web and MCP share the same component-boundary matching, HOME collapse,
    secret-shape whole-replace and truncation contract.

    Strip BOTH the given root and its ``.resolve()``'d form (#1412): engine
    paths are resolved (``canonical_mcp_server_root`` etc.), but the route may
    receive an unresolved/symlinked ``project_root`` (macOS ``/tmp``→
    ``/private/tmp``, a symlinked home, a case-variant mount). Stripping only
    one form leaves the absolute resolved path in the reason — the same
    canonical-path disclosure #1412 closes on the parse-error 422s, here on the
    list/diff ``reason`` surface. A sibling whose name starts with the root is
    scrubbed, never turned into a relative-looking suffix (#1889).
    """
    return redact_engine_reason(message, project_root)


def _safe_rel(p: Path, project_root: Path) -> str:
    """Project-relative path as a POSIX string for API payloads.

    ``.as_posix()`` (not ``str``) so ``canonical_path`` / ``path`` fields come
    back ``/``-separated on every platform — the Web UI and diff payloads pin
    POSIX separators (#1256; the ``PureWindowsPath`` guard is #1325). Falls back
    to the absolute POSIX path for user-tier locations outside ``project_root``.
    Shared by the skills / commands / agents / mcp-servers routes so the
    path-sanitization boundary cannot drift per kind (same rationale as
    ``sanitize_diff_reason``) — the per-kind copies DID drift: #1412 hardened
    only the mcp-servers variant, leaving the others latent (#1264 parity).

    ``p`` is a ``.resolve()``'d canonical/runtime path (``canonical_artifact_dir``
    resolves), but the route may receive an unresolved/symlinked ``project_root``
    (macOS ``/tmp``→``/private/tmp``, a symlinked home, a case-variant mount),
    so ``relative_to`` against the bare root raises ``ValueError`` and the
    fallback would emit the ABSOLUTE resolved path to the loopback dashboard
    (#1412, the same disclosure as the parse-error reason). Try the resolved
    root too before falling back. ``resolve()`` lives only on concrete ``Path``
    objects — the cross-platform ``PureWindowsPath`` tests (#1325) drive this
    with a pure path, so the resolved attempt is guarded and skipped there.
    """
    roots = [project_root]
    try:
        resolved_root = project_root.resolve()
    except (AttributeError, OSError):
        resolved_root = project_root
    if resolved_root != project_root:
        roots.append(resolved_root)
    for root in roots:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            continue
    return p.as_posix()


def read_text_lenient(path: Path) -> str | None:
    """Best-effort text read for diff payload previews (#1229 U7).

    ``None`` on any OSError (permission, race-deleted) — a per-name diff
    endpoint must keep diagnosing instead of 500ing while the engine diff
    reports the same file as a typed row; non-UTF-8 bytes decode with
    U+FFFD replacement (the #1233 lenient-read contract).
    """
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def expected_vs_runtime_row(
    *,
    kind: str,
    gen_name: str,
    render: Callable[[], str],
    target: Path,
    name: str,
    project_root: Path,
    scope: TargetScope,
) -> dict[str, object]:
    """Both-sides-exist diff row on the engine's comparison basis (#1247 id 30).

    The per-item diff panes used to compare the RAW canonical text against the
    runtime file, while the list badge (engine ``diff_commands`` /
    ``diff_agents``) compares **vendor override bytes → else rendered output**
    — so a TOML/YAML-rendering runtime (gemini commands, codex/kimi agents)
    showed a permanent "out of sync" pane under an "in sync" badge. Shared by
    the commands and agents per-name diff routes so the comparison basis
    cannot drift per kind (same rationale as ``sanitize_diff_reason``).

    ``render`` is lazy so an override-carrying runtime never pays for (or
    crashes on) a render it doesn't use. Error parity with the engine: an
    unreadable override or runtime file means parity can't be asserted —
    report drift, never mask it; the unreadable side's content key is omitted.
    ``expected_content`` is what sync would write — the pane's diff baseline.
    """
    vendor = GENERATOR_VENDOR.get(gen_name)
    override_path = (
        _override.resolve(project_root, kind, name, vendor, scope=scope)
        if vendor is not None
        else None
    )
    expected_bytes: bytes | None
    if override_path is not None:
        try:
            expected_bytes = override_path.read_bytes()
        except OSError:
            expected_bytes = None
    else:
        expected_bytes = render().encode("utf-8")

    runtime_bytes: bytes | None
    try:
        runtime_bytes = target.read_bytes()
    except OSError:
        runtime_bytes = None

    entry: dict[str, object] = {"runtime": gen_name}
    if expected_bytes is None or runtime_bytes is None:
        entry["status"] = "out of sync"
    else:
        entry["status"] = "in sync" if expected_bytes == runtime_bytes else "out of sync"
    if expected_bytes is not None:
        entry["expected_content"] = expected_bytes.decode("utf-8", errors="replace")
    if runtime_bytes is not None:
        entry["runtime_content"] = runtime_bytes.decode("utf-8", errors="replace")
    return entry


def delete_skip_entry(path: Path, exc: OSError, project_root: Path) -> dict[str, str]:
    """``skipped[]`` row for a failed delete leg (#1247 id 49).

    ``str(OSError)`` embeds absolute paths — including ``$HOME``-rooted
    cascade targets — so the reason crosses the wire through
    ``sanitize_diff_reason`` like every other route-surfaced exception text.
    Shared by the skills/commands/agents delete routes (6 call sites) so the
    sanitization boundary cannot drift per kind.
    """
    try:
        rel = str(path.relative_to(project_root))
    except ValueError:
        rel = str(path)
    return {
        "path": rel,
        "reason": sanitize_diff_reason(str(exc), project_root) or "",
    }


def _compute_last_synced_at(project_root: Path, target_scope: TargetScope) -> str | None:
    """Return ISO8601 UTC timestamp of the most recently touched canonical artifact.

    ADR-0009 §1.c: the dashboard freshness indicator is sourced from
    **canonical-source mtime** rather than a persisted sync-event log —
    cheapest option, matches Health Report semantics, avoids a new
    write path. The cost is that the timestamp doesn't disambiguate
    "edits" from "explicit syncs"; the ADR accepts this for the v1
    "5 min ago" surface.

    Aggregates across skills + commands + agents canonical files for the
    requested ``target_scope`` (so a tier switch on the dashboard refreshes
    the freshness signal alongside the per-tile counts). Settings is
    intentionally excluded — its additive merge has no canonical-residency
    that maps to "what would Sync All push," so its mtime would muddle the
    freshness reading rather than sharpen it.

    Returns ``None`` when no canonical files exist in the requested scope —
    a fresh / empty project legitimately has no last-sync, and the
    dashboard suppresses the line in that case (clearer than rendering
    epoch-zero or "never").
    """
    from memtomem.context.agents import list_canonical_agents
    from memtomem.context.commands import list_canonical_commands
    from memtomem.context.mcp_servers import list_canonical_mcp_servers
    from memtomem.context.skills import SKILL_MANIFEST, list_canonical_skills

    latest: float | None = None

    def _bump(path: Path) -> None:
        nonlocal latest
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if latest is None or mtime > latest:
            latest = mtime

    try:
        for skill_dir in list_canonical_skills(project_root, scope=target_scope):
            # ``list_canonical_skills`` returns the skill directory; the
            # mtime that tracks "last sync" is the manifest file written by
            # ``sync_skills`` / extract. The directory's own mtime advances
            # on any auxiliary-file write too, which would over-trigger.
            _bump(skill_dir / SKILL_MANIFEST)
    except Exception:
        logger.exception("list_canonical_skills failed during last_synced_at")

    try:
        for path, _layout in list_canonical_commands(project_root, scope=target_scope):
            # ``list_canonical_commands`` returns the manifest file path
            # directly (flat: ``<name>.md``; dir: ``<name>/command.md``).
            _bump(path)
    except Exception:
        logger.exception("list_canonical_commands failed during last_synced_at")

    try:
        for path, _layout in list_canonical_agents(project_root, scope=target_scope):
            _bump(path)
    except Exception:
        logger.exception("list_canonical_agents failed during last_synced_at")

    if target_scope == "project_shared":
        try:
            for path in list_canonical_mcp_servers(project_root):
                _bump(path)
        except Exception:
            logger.exception("list_canonical_mcp_servers failed during last_synced_at")

    if latest is None:
        return None

    # ISO8601 UTC with a trailing ``Z`` — matches the audit-catalog freshness
    # surface and avoids the ``+00:00`` representation that confuses naive
    # JS ``new Date(...)`` parsing across older browsers.
    from datetime import datetime, timezone

    return datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_detected_runtimes(project_root: Path) -> list[dict[str, object]]:
    from memtomem.context.runtime_coverage import compute_runtime_coverage

    return compute_runtime_coverage(project_root)


def _error_payload(exc: BaseException, *, shape: str = "total") -> dict:
    """Build the per-surface error envelope.

    ``shape="total"`` matches skills/commands/agents (count-based summary).
    ``shape="status"`` matches settings (status-based summary).
    ``error: True`` and ``total: 0`` are preserved for backwards compatibility
    so existing front-end and external callers keep working.
    """
    kind = _classify_exception(exc)
    message = _redact_message(str(exc))
    if shape == "status":
        return {"status": "error", "error_kind": kind, "error_message": message}
    return {"total": 0, "error": True, "error_kind": kind, "error_message": message}


@router.get(
    "/context/overview",
    response_model=ContextOverviewResponse,
    # exclude_unset: detected_runtimes entries conditionally OMIT
    # installed/memtomem_registered (runtime_coverage.py) — absent ≠ null.
    response_model_exclude_unset=True,
)
async def context_overview(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to summarize. project_local is shown only "
            "when explicitly requested."
        ),
    ),
) -> dict:
    """Aggregate sync status across skills, commands, agents, and settings."""
    from memtomem.context.agents import canonical_agent_name, diff_agents, list_canonical_agents
    from memtomem.context.commands import (
        canonical_command_name,
        diff_commands,
        list_canonical_commands,
    )
    from memtomem.context.settings import diff_settings
    from memtomem.context.mcp_servers import diff_mcp_servers, list_canonical_mcp_servers
    from memtomem.context.skills import diff_skills, list_canonical_skills

    def _collect() -> dict:
        result: dict[str, dict[str, int | bool | str]] = {}

        try:
            result["skills"] = summarize_diff_with_canonical(
                diff_skills(project_root, scope=target_scope),
                {p.name for p in list_canonical_skills(project_root, scope=target_scope)},
            )
        except Exception as exc:
            logger.exception("diff_skills failed")
            result["skills"] = _error_payload(exc, shape="total")

        try:
            result["commands"] = summarize_diff_with_canonical(
                diff_commands(project_root, scope=target_scope),
                {
                    canonical_command_name(p, layout)
                    for p, layout in list_canonical_commands(project_root, scope=target_scope)
                },
            )
        except Exception as exc:
            logger.exception("diff_commands failed")
            result["commands"] = _error_payload(exc, shape="total")

        try:
            result["agents"] = summarize_diff_with_canonical(
                diff_agents(project_root, scope=target_scope),
                {
                    canonical_agent_name(p, layout)
                    for p, layout in list_canonical_agents(project_root, scope=target_scope)
                },
            )
        except Exception as exc:
            logger.exception("diff_agents failed")
            result["agents"] = _error_payload(exc, shape="total")

        try:
            if target_scope == "project_shared":
                result["mcp_servers"] = summarize_diff_with_canonical(
                    diff_mcp_servers(project_root),
                    {p.stem for p in list_canonical_mcp_servers(project_root)},
                )
            else:
                result["mcp_servers"] = {"total": 0, "local_draft": 0}
        except Exception as exc:
            logger.exception("diff_mcp_servers failed")
            result["mcp_servers"] = _error_payload(exc, shape="total")

        try:
            settings_diff = diff_settings(project_root, scope=target_scope)
            # In-band `error` is a COUNT (per-file failures already classified
            # by diff_settings — no error_kind, which would conflate distinct
            # per-file causes), NOT the bool flag `_error_payload(shape="status")`
            # emits when the whole call raises. The two shapes are on disjoint
            # code paths. The frontend uses truthiness on `d.error` (any
            # positive int OR the bool `true` reaches the danger render at
            # context-gateway.js:136-145), so `error: 0` correctly skips the
            # danger branch and `error: >=1` reaches it — both shapes work.
            result["settings"] = summarize_settings_statuses(
                [r.status for r in settings_diff.values()]
            )
        except Exception as exc:
            logger.exception("diff_settings failed")
            result["settings"] = _error_payload(exc, shape="status")

        # Wiki-install staleness axis (0629 backlog c/d): the count of
        # installed assets whose lockfile pin sits behind wiki HEAD ("update
        # available"). This is the single-project lockfile↔wiki axis — none of
        # the canonical→runtime tiles above carry it. The cross-project roll-up
        # of the same drift lives on /context/status-all, which the Projects
        # portal consumes for its per-project drift badge (#1649); this overview
        # axis stays the single-project view. ``None`` (not zeros) on failure:
        # the badge is a pure header enhancement, but "0 behind" is a
        # clean-state claim we can't back when the classifier itself raised.
        wiki_installs: dict[str, int] | None
        try:
            if target_scope == "project_shared":
                _wiki_head, status_rows = classify_status(project_root)
                tracked = [
                    row
                    for row in status_rows
                    if row.tier == "project_shared" and row.state != "untracked"
                ]
                wiki_installs = {
                    "total": len(tracked),
                    "behind": sum(1 for row in tracked if row.state == "behind"),
                }
            else:
                # Wiki installs are lockfile-tracked project_shared snapshots
                # only — mirror the mcp_servers single-tier placeholder.
                wiki_installs = {"total": 0, "behind": 0}
        except Exception:
            logger.exception("classify_status failed")
            wiki_installs = None

        detected_runtimes_unavailable = False
        try:
            detected_runtimes = _compute_detected_runtimes(project_root)
        except Exception:
            logger.exception("_compute_detected_runtimes failed")
            detected_runtimes = []
            detected_runtimes_unavailable = True

        try:
            last_synced_at = _compute_last_synced_at(project_root, target_scope)
        except Exception:
            logger.exception("_compute_last_synced_at failed")
            last_synced_at = None

        return {
            "target_scope": target_scope,
            "project_root": str(project_root),
            "detected_runtimes": detected_runtimes,
            "last_synced_at": last_synced_at,
            "wiki_installs": wiki_installs,
            **result,
            # Appended last: the wire goldens pin key positions, so additive
            # fields never displace existing keys (#1692 PR 5 precedent).
            "detected_runtimes_unavailable": detected_runtimes_unavailable,
        }

    # The whole aggregation — per-kind diffs (filesystem scans), the
    # classify_status git probes (#1145 discipline), and the runtime/mtime
    # sweeps — runs off the event loop in one hop, mirroring status-all
    # (#1280). Per-kind failures are converted to error payloads inside the
    # closure, so this await only raises on a defect in the shaping itself.
    # No gateway lock: read-only path (#1518).
    return await asyncio.to_thread(_collect)


@router.get("/context/runtimes", response_model=ContextRuntimesResponse)
async def context_runtimes(
    project_root: Path = Depends(resolve_scope_root),
) -> dict:
    """Read-only provider-client registration status (ADR-0021 §B).

    Reports per-client install + ``memtomem``/``mms`` registration for the
    in-scope provider clients (Claude, Antigravity, Codex, Kimi). This is the
    client/provider axis — distinct from ``overview.detected_runtimes`` (the
    artifact fan-out chip strip). Read-only; returns no raw config contents
    (the registry's trust boundary returns only booleans, location kinds, and
    ``$HOME``-collapsed paths).

    Availability envelope (#1692): per-client read failures ride in each
    entry's ``error_kind`` (``probe_runtime`` never raises), so
    ``runtimes_status: "unavailable"`` fires only when the probe machinery
    itself raised — the two signals are disjoint, and ``unavailable`` only
    ever coexists with ``runtimes: []``. The warning item mirrors the
    ``GET /api/context/projects`` registry envelope (#1699), minus
    ``skipped_rows`` (no row concept here).
    """
    from memtomem.context.runtime_registry import probe_all_runtimes

    warnings: list[dict[str, object]] = []
    try:
        runtimes = [s.to_dict() for s in probe_all_runtimes(project_root)]
        runtimes_status = "ok"
    except Exception as exc:
        logger.exception("probe_all_runtimes failed")
        runtimes = []
        runtimes_status = "unavailable"
        warnings.append(
            {
                "reason_code": "status_unavailable",
                "error_kind": _classify_exception(exc),
                "message": _redact_message(str(exc)),
                "retryable": True,
            }
        )
    return {
        "project_root": str(project_root),
        "runtimes": runtimes,
        "runtimes_status": runtimes_status,
        "warnings": warnings,
    }


# ── GET /context/status-all — cross-project drift aggregation (#1280) ────

#: Remediation prose per ``sync_skip_reason`` code. The CODE derivation is
#: shared with the batch-sync surfaces (``context.projects.sync_skip_reason``)
#: so batch views cannot drift on WHICH scopes are reported; the prose is
#: surface-local (read-context wording — these rows explain why a project is
#: absent from a status report, not why a write was withheld).
_STATUS_ALL_SKIP_MESSAGES: dict[str, str] = {
    "missing_root": (
        "project root no longer exists on disk; re-register it or remove "
        "the entry from the Projects portal."
    ),
    "sync_paused": (
        "sync enrollment is paused for this project; resume it from the "
        "Projects portal to include it in batch views."
    ),
    "sync_not_enrolled": (
        "discovery-only project (never enrolled); register it from the "
        "Projects portal to include it in batch views."
    ),
    "stale_project": (
        "project has no .memtomem/ store (never initialized); run "
        "`mm context init` there before it can be reported."
    ),
}


def _status_all_entry(
    base: dict[str, Any], status: ProjectStatus, project_root: Path
) -> dict[str, Any]:
    """Serialize one executed project's ``ProjectStatus`` for the wire.

    Entry ``status`` vocabulary: ``error`` (a corrupt / version-mismatched
    lockfile OR any per-kind diff probe that raised, #1692 — the aggregate is
    kept but cannot be trusted as complete, the single-status CLI's exit-1
    condition), else ``drift``/``ok`` from the shared predicate. Row ``reason``
    strings can embed wiki paths and raw
    exception text (this is the first surface serializing ``StatusRow``),
    so they pass through ``sanitize_diff_reason`` — the established
    display-sanitize boundary; ``lockfile_error`` gets the same treatment.
    Failed diff kinds become ``_error_payload`` envelopes under their kind
    key, exactly like the single-project overview — INCLUDING the shape
    split: settings uses the status-based envelope, the four artifact
    kinds use the count-based one (Codex impl-review fold — one shape for
    all five would hand clients two incompatible settings error
    envelopes across the two routes).
    """
    diff_counts: dict[str, Any] = dict(status.diff_counts)
    for kind, exc in status.diff_errors.items():
        diff_counts[kind] = _error_payload(exc, shape="status" if kind == "settings" else "total")
    # A failed lockfile read OR any per-kind diff probe that raised is an
    # ``error`` — the check could not establish drift, so it must not read as
    # Sync-remediable ``drift`` (#1692). The failing kind's error envelope is
    # already in ``diff_counts[kind]`` above; the entry-level ``error`` object
    # stays reserved for a total collector crash (see ``context_status_all``).
    if status.lockfile_error or status.diff_errors:
        entry_status = "error"
    elif status.drift:
        entry_status = "drift"
    else:
        entry_status = "ok"
    return {
        **base,
        "status": entry_status,
        "wiki_head": status.wiki_head,
        "lockfile_error": (
            sanitize_diff_reason(status.lockfile_error, project_root)
            if status.lockfile_error
            else None
        ),
        "state_counts": status.state_counts,
        "diff_counts": diff_counts,
        "rows": [
            {
                "asset_type": row.asset_type,
                "name": row.name,
                "pin_commit": row.pin_commit,
                "installed_at": row.installed_at,
                "state": row.state,
                "dirty_file_count": row.dirty_file_count,
                "reason": sanitize_diff_reason(row.reason, project_root),
                "tier": row.tier,
            }
            for row in status.rows
        ],
    }


@router.get("/context/status-all", response_model=ContextStatusAllResponse)
async def context_status_all(
    request: Request,
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to aggregate in every project. Only "
            "project_shared is supported: the user tier is one global store "
            "(meaningless per project) and project_local has no fan-out to "
            "drift — mirror of the batch-sync tier gate."
        ),
    ),
) -> dict:
    """Aggregate per-project drift status across every discovered project.

    The read half of ADR-0025's batch story (#1280): one call answers
    "which of my projects have drifted" instead of N ``/context/overview``
    fetches. Read-only — per-root filesystem/git reads, NO gateway lock,
    and each project's collection runs off the event loop. Returns 200
    whenever the loop ran; non-2xx only for the tier gate (400).

    Per-project entries: ``skipped`` (shared ``sync_skip_reason`` codes +
    surface-local prose), ``error`` (corrupt lockfile, a per-kind diff probe
    that raised (#1692), or the collector itself raised — A-9's failed-entry
    envelope shape), else ``ok``/``drift`` via the shared
    ``ProjectStatus.drift`` predicate. ``summary`` is counts
    only — deliberately NO roll-up status string: A-9's ``ok|partial|
    failed`` describe mutation success, while fleet health here is just
    ``drifted + errors == 0``, derivable; a third vocabulary would invite
    drift.
    """
    if target_scope != "project_shared":
        # ADR-0023 §10 object envelope via the shared ``_errors._error``
        # constructor (B-1 #1284 consolidated the inline tier-gate dict that
        # used to dodge the context_transfer↔context_gateway import cycle).
        raise _error(
            400,
            "validation",
            "status-all aggregates the project_shared tier only: the user "
            "tier is one global store (not per-project) and project_local "
            "has no runtime fan-out to drift (ADR-0011 §3); use the "
            "single-project routes for those views.",
        )
    wiki = WikiStore.at_default()
    entries: list[dict[str, Any]] = []
    counts = {"drifted": 0, "clean": 0, "errors": 0, "skipped": 0}
    for scope in _discover_for(request):
        base: dict[str, Any] = {
            "project_scope_id": scope.scope_id,
            "label": scope.label,
            "root": str(scope.root) if scope.root is not None else None,
        }
        code = sync_skip_reason(scope)
        if code is not None:
            counts["skipped"] += 1
            entries.append(
                {
                    **base,
                    "status": "skipped",
                    "reason_code": code,
                    "message": _STATUS_ALL_SKIP_MESSAGES[code],
                }
            )
            continue
        assert scope.root is not None  # sync_skip_reason returned missing_root otherwise
        try:
            status = await asyncio.to_thread(
                collect_project_status,
                scope.root,
                wiki=wiki,
                target_scope=target_scope,
            )
        except Exception as exc:  # defensive — collect contains per-kind failures
            logger.error(
                "status-all %s failed outside the collector: %s",
                scope.scope_id,
                exc,
                exc_info=True,
            )
            counts["errors"] += 1
            entries.append(
                {
                    **base,
                    "status": "error",
                    "error": {
                        "error_kind": _classify_exception(exc),
                        "message": _redact_message(str(exc)),
                        "http_status": 500,
                    },
                }
            )
            continue
        entry = _status_all_entry(base, status, scope.root)
        key = {"error": "errors", "drift": "drifted", "ok": "clean"}[entry["status"]]
        counts[key] += 1
        entries.append(entry)
    return {
        "target_scope": target_scope,
        "projects": entries,
        "summary": {
            "projects_total": len(entries),
            "executed": len(entries) - counts["skipped"],
            **counts,
        },
    }


def redact_wire_reason(reason: str | None, project_root: Path) -> str | None:
    """``sanitize_diff_reason`` plus the residual absolute-path backstop.

    The two-stage form for any wire field carrying **raw engine exception
    text**, as opposed to a path the route itself resolved. Root-relative
    stripping is the readable half — a reason under the project keeps its
    familiar short form — and the scrub is the fail-safe half for everything
    that lands outside both roots, which is exactly what an ``OSError`` from a
    runtime dir symlinked onto a shared volume produces.

    The backstop is :func:`memtomem.context.error_redact.scrub_absolute_paths`
    rather than a local regex. This module used to carry its own copy with its
    own "keep in sync" comment; they were byte-identical, and one shared
    function is the only version of "in sync" that cannot drift. Note the scrub
    also eats a root-stripped RELATIVE remainder, which is deliberate — see
    that function, and ``test_context_status_global.py::
    test_route_redacts_error_reason``, which pins it.

    Deliberately NOT named ``redact_engine_reason``: ``error_redact`` already
    exports a function by that name with a different signature
    (``*project_roots``), so two import sites would mean two different things
    (PR review). This one is the web wire boundary; that one is the neutral
    engine-side twin.
    """
    cleaned = sanitize_diff_reason(reason, project_root)
    if cleaned is None:
        return None
    return scrub_absolute_paths(cleaned)


def _redact_pull_reason(reason: str | None, project_root: Path) -> str | None:
    """Redact a pull-preview candidate ``reason`` for the wire (defense in depth)."""
    return redact_wire_reason(reason, project_root)


@router.get(
    "/context/{kind}/{name}/pull-preview",
    response_model=ContextPullPreviewResponse,
)
async def context_pull_preview(
    kind: str,
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Destination canonical tier the Pull would land in. "
            "project_local is rejected — it has no runtime fan-out to pull "
            "FROM (ADR-0011 §3, ADR-0030 §11)."
        ),
    ),
) -> dict:
    """Read-only Pull preview: what would a Pull of ``name`` land, per runtime.

    ADR-0030 PR-B. For each runtime that has the artifact on disk, reports the
    two-axis ``content_status`` / ``gate_status`` (:mod:`context.pull_preview`)
    plus the §5 ambiguity signal. No writes, no privacy-counter mutation — the
    engine runs off the event loop (per-tree filesystem reads). Every path in
    the engine's ``reason`` text is display-sanitized (``sanitize_diff_reason``)
    before it reaches the wire; the two status axes are closed ``Literal`` sets.
    """
    if kind not in IMPORT_SOURCE_RUNTIMES:
        raise _error(
            400,
            "validation",
            f"kind {kind!r} has no Pull sources; choose one of: "
            f"{', '.join(IMPORT_SOURCE_RUNTIMES)}.",
        )
    if target_scope == "project_local":
        raise _error(
            400,
            "validation",
            "project_local has no runtime fan-out to pull from (ADR-0011 §3); "
            "pull into user or project_shared instead.",
        )
    try:
        validated = validate_name(name, kind=f"{kind[:-1]} name")
    except InvalidNameError as exc:
        raise _error(400, "validation", str(exc))

    artifact_kind = cast(ArtifactKind, kind)
    preview = await asyncio.to_thread(
        preview_pull,
        artifact_kind,
        validated,
        scope=target_scope,
        project_root=project_root,
    )
    return {
        "kind": preview.kind,
        "name": preview.name,
        "target_scope": preview.scope,
        "store_present": preview.store_present,
        "candidates": [
            {
                "runtime": c.runtime,
                "content_status": c.content_status,
                "gate_status": c.gate_status,
                "importable": c.importable,
                "landing_group": c.landing_group,
                "override_warning": c.override_warning,
                "reason": _redact_pull_reason(c.reason, project_root),
            }
            for c in preview.candidates
        ],
        "distinct_landing_count": preview.distinct_landing_count,
        "ambiguous": preview.ambiguous,
        "auto_source": preview.auto_source,
    }


# ── GET /context/status-global — user-tier portal (ADR-0030 §9 + §1, PR-F) ──


@router.get("/context/status-global", response_model=ContextStatusGlobalResponse)
async def context_status_global() -> dict:
    """User-tier global portal status — ADR-0030 §9 + §1 (PR-F).

    The user tier is one global ``~/.memtomem`` Store with no per-project
    fan-out, so it does NOT ride the ``project_shared``-only
    ``/context/status-all`` fleet endpoint — it gets this parameterless sibling
    (no ``target_scope`` query to abuse; user-only by construction). Reports the
    global-library inventory counts, host runtime coverage, and a READ-ONLY
    pull-direction drift summary (does any runtime copy differ from the Store?).
    No writes, no privacy-counter mutation; the probe runs off the event loop
    (per-tree filesystem reads). Every error ``reason`` is display-sanitized
    (``_redact_pull_reason``) — no absolute paths on the wire.
    """
    from memtomem.context.runtime_coverage import compute_runtime_coverage

    home = Path.home()
    summary, coverage = await asyncio.gather(
        asyncio.to_thread(probe_pull_drift, scope="user", project_root=None),
        asyncio.to_thread(compute_runtime_coverage, home),
    )

    # Inventory counts fall out of the drift rows (one row per Store artifact) —
    # no second Store walk.
    store = {
        "skills": sum(1 for r in summary.rows if r.kind == "skills"),
        "agents": sum(1 for r in summary.rows if r.kind == "agents"),
        "commands": sum(1 for r in summary.rows if r.kind == "commands"),
    }
    return {
        "scope": "user",
        "store": store,
        "runtime_coverage": [
            {
                "name": str(entry["name"]),
                "available": bool(entry["available"]),
                # compute_runtime_coverage omits these when the registry probe
                # found no client; this module forbids optional keys.
                "installed": entry.get("installed"),
                "memtomem_registered": entry.get("memtomem_registered"),
            }
            for entry in coverage
        ],
        "pull_drift": {
            "has_pull_drift": summary.has_pull_drift,
            "total": summary.total,
            "differs": summary.differs,
            "errors": summary.errors,
            "identical": summary.identical,
            "rows": [
                {
                    "kind": r.kind,
                    "name": r.name,
                    "verdict": r.verdict,
                    "runtimes": list(r.runtimes),
                    "reason": _redact_pull_reason(r.reason, home),
                }
                for r in summary.rows
            ],
        },
    }


# ── POST /context/{kind}/{name}/pull (ADR-0030 PR-D) ─────────────────────────

#: Web ingress surface tag for the deferred privacy counter — ``prepare_pull``
#: carries it onto the ``PullPlan`` so ``commit_pull`` records the counter under
#: THIS name (never the CLI's ``cli_context_pull``) when the write lands.
_PULL_SURFACE = "web_context_pull"

#: Whole-call lock budget for a Pull commit — the async surface bound the engine
#: docstring requires (a ``lock_timeout`` result maps to 503). Mirrors the other
#: locked context routes' 30s budget (``_INSTALL_LOCK_BUDGET_S`` /
#: ``_TRANSFER_LOCK_BUDGET_S``), which stays under the client's request window.
_PULL_LOCK_BUDGET_S = 30.0


class PullApplyRequest(BaseModel):
    """Body for ``POST /context/{kind}/{name}/pull`` — the source-selectable Pull.

    ``source_runtime`` names the runtime to pull from (required when candidates
    diverge — otherwise the engine refuses with ``source_conflict``).

    Destination consent (mirrors the CLI's explicit ``--scope`` + confirm and
    the web's #1263 host-write gate — a CSRF token proves request origin, not
    the user's approval to write): a ``project_shared`` landing (git-tracked)
    needs ``confirm_project_shared=true``; a ``user`` landing goes through
    ``host_write_gate`` on ``allow_host_writes`` (the write lands on host paths
    outside any project). An unconfirmed Pull that WOULD write returns the
    ``needs_confirmation`` envelope and writes nothing.

    ``force_unsafe_import`` is the Gate A bypass valve: it only bypasses a
    *bypassable* tier (``user``) — ``project_shared`` hard-refuses regardless
    (ADR-0011 §5), enforced in the engine. It is validated ``literal-true``: a
    coercible ``"true"`` / ``"yes"`` / ``1`` does NOT enable the security bypass
    (the web force-unsafe transport contract; mirrors
    ``IndexRequest._only_literal_true``)."""

    source_runtime: str | None = None
    overwrite: bool = False
    confirm_project_shared: bool = False
    allow_host_writes: bool = False
    force_unsafe_import: bool = False

    @field_validator("force_unsafe_import", mode="before")
    @classmethod
    def _only_literal_true(cls, v: object) -> bool:
        # Only a JSON literal ``true`` enables the Gate A bypass; Pydantic's
        # default bool would coerce ``"true"`` / ``"yes"`` / ``1``. Fail closed
        # on any non-boolean (mirrors ``IndexRequest._only_literal_true`` and the
        # "literal true only" contract for every web force-unsafe valve).
        return v is True


def _pull_apply_payload(result: PullApplyResult, project_root: Path) -> dict:
    """Project a :class:`PullApplyResult` onto the ``ContextPullApplyResponse``
    wire dict (field/insertion order = the model's declared order).

    Every ``reason`` — top-level and per-candidate — is display-sanitized
    (``_redact_pull_reason``); the engine's ``gate_blocked`` reason is already
    path-free (runtime + scope only) but is redacted anyway as a backstop. The
    absolute destination ``dst`` is never sent raw: ``canonical_path`` goes
    through the SAME ``_redact_pull_reason`` (not bare ``sanitize_diff_reason``)
    so its residual absolute-path backstop masks a resolved / symlinked ``$HOME``
    whose spelling doesn't match the cached one (the canonical-path-leak rule —
    user-tier ``dst`` is a resolved host path). No raw bytes exist on this model
    at all."""
    canonical_path = (
        _redact_pull_reason(str(result.dst), project_root) if result.dst is not None else None
    )
    return {
        "status": result.status,
        "kind": result.kind,
        "name": result.name,
        "target_scope": result.scope,
        "reason": _redact_pull_reason(result.reason, project_root) or "",
        "reason_code": result.reason_code,
        "selected_runtime": result.selected_runtime,
        "write_outcome": result.write_outcome,
        "duplicate_runtimes": list(result.duplicate_runtimes),
        "canonical_path": canonical_path,
        "candidates": [
            {
                "runtime": c.runtime,
                "content_status": c.content_status,
                "gate_status": c.gate_status,
                "importable": c.importable,
                "landing_group": c.landing_group,
                "override_warning": c.override_warning,
                "reason": _redact_pull_reason(c.reason, project_root),
            }
            for c in result.candidates
        ],
        "distinct_landing_count": result.distinct_landing_count,
        "gate_status": result.gate_status,
        "gate_hits": result.gate_hits,
        "force_bypassable": result.force_bypassable,
    }


def _finalize_pull(result: PullApplyResult, project_root: Path) -> dict:
    """Map a :class:`PullApplyResult` onto its HTTP surface.

    Domain decisions — the ``applied`` write and every refusal that hands the
    picker something to act on (``source_conflict`` with its candidate rows,
    ``gate_blocked`` with ``force_bypassable``, ``canonical_exists`` →
    ``overwrite``, …) — are result-coded 200 bodies. The five statuses with
    genuine HTTP meaning become error envelopes instead (the client ``api()``
    helper drops structured detail on non-2xx, but these carry none the picker
    needs): ``lock_timeout`` → 503 (transient, retry), ``plan_stale`` → 409
    (destination changed under the lock — re-preview),
    ``swap_recovery_pending`` → 409 (an interrupted directory swap the engine
    refuses to resolve on its own, ADR-0030 §10), and the two fail-closed
    write failures ``snapshot_failed`` / ``write_failed`` → 500. Every message
    is display-sanitized (an OSError reason may embed a path)."""
    status = result.status
    if status == "lock_timeout":
        raise _error(
            503,
            "busy",
            "the canonical store is locked by another write; retry shortly.",
            reason_code=result.reason_code,
        )
    if status == "plan_stale":
        raise _error(
            409,
            "conflict",
            _redact_pull_reason(result.reason, project_root)
            or "the Store changed since the preview; re-preview and retry.",
            reason_code=result.reason_code,
        )
    if status == "swap_recovery_pending":
        # 409, not 500: nothing failed infrastructurally and the requested
        # operation did not run — the artifact is wedged in a state only an
        # operator can adjudicate, which is a conflict about the resource, not
        # a server fault. The reason carries the CONDITION and the instruction
        # to inspect; the paths themselves are redacted to ``'<path>'`` here as
        # on every other wire field, and the CLI is where they survive verbatim
        # (PR review — an earlier version of this comment promised paths this
        # surface deliberately does not emit).
        raise _error(
            409,
            "conflict",
            _redact_pull_reason(result.reason, project_root)
            or "an interrupted directory swap left this artifact in a state "
            "that needs manual inspection.",
            reason_code=result.reason_code,
        )
    if status in ("snapshot_failed", "write_failed"):
        raise _error(
            500,
            "internal",
            _redact_pull_reason(result.reason, project_root) or "the pull could not be written.",
            reason_code=result.reason_code,
        )
    return _pull_apply_payload(result, project_root)


@router.post(
    "/context/{kind}/{name}/pull",
    response_model=ContextPullApplyResponse | ContextPullApplyNeedsConfirmation,
)
async def context_pull_apply(
    kind: str,
    name: str,
    body: PullApplyRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        ...,
        description=(
            "Destination canonical tier the Pull lands in — REQUIRED (no "
            "default: an implicit project_shared would silently write to the "
            "git-tracked tier). project_local is rejected (no runtime fan-out to "
            "pull FROM — ADR-0011 §3); the explicit tier is the Gate B scope "
            "choice, gated by confirm_project_shared / allow_host_writes."
        ),
    ),
) -> dict:
    """Execute a source-selectable Pull of ``name`` into the canonical Store.

    ADR-0030 PR-D — the web sibling of ``mm context pull --apply`` (PR-C) over
    the SAME :mod:`context.pull_apply` engine (``prepare_pull`` →
    ``commit_pull``). The engine is result-coded on purpose, so every domain
    decision — the ``applied`` write and every refusal (``source_conflict``
    carrying its candidate rows, ``gate_blocked`` carrying ``force_bypassable``,
    …) — returns HTTP 200 with a :class:`ContextPullApplyResponse` the picker
    branches on; only the five HTTP-semantic statuses escape as error codes (see
    :func:`_finalize_pull`).

    Destination consent runs ONLY once ``prepare_pull`` yields a committable
    plan (a write is imminent) — so refusals and the byte-identical no-op never
    prompt: ``project_shared`` needs ``confirm_project_shared``; ``user`` runs
    the #1263 ``host_write_gate``. Reasons/paths are display-sanitized and no raw
    artifact bytes reach the wire.
    """
    if kind not in IMPORT_SOURCE_RUNTIMES:
        raise _error(
            400,
            "validation",
            f"kind {kind!r} has no Pull sources; choose one of: "
            f"{', '.join(IMPORT_SOURCE_RUNTIMES)}.",
        )
    if target_scope == "project_local":
        raise _error(
            400,
            "validation",
            "project_local has no runtime fan-out to pull from (ADR-0011 §3); "
            "pull into user or project_shared instead.",
        )
    try:
        validated = validate_name(name, kind=f"{kind[:-1]} name")
    except InvalidNameError as exc:
        raise _error(400, "validation", str(exc))

    # An ineligible/unknown --from is a request-shape error, not an engine
    # outcome — reject at the boundary with the engine's own wording (parity
    # with the CLI's up-front ``resolve_import_runtimes`` guard) rather than
    # letting ``prepare_pull`` raise a bare ValueError into a 500.
    artifact_kind = cast(ArtifactKind, kind)
    if body.source_runtime is not None:
        try:
            resolve_import_runtimes(artifact_kind, body.source_runtime)
        except ValueError as exc:
            raise _error(400, "validation", str(exc))

    outcome = await asyncio.to_thread(
        prepare_pull,
        artifact_kind,
        validated,
        scope=target_scope,
        project_root=project_root,
        source_runtime=body.source_runtime,
        overwrite=body.overwrite,
        force_unsafe_import=body.force_unsafe_import,
        surface=_PULL_SURFACE,
    )
    if isinstance(outcome, PullApplyResult):
        # A refusal, or the byte-identical no-op (status == "applied") — neither
        # writes, so no destination-consent gate applies.
        return _finalize_pull(outcome, project_root)

    # A committable plan → a write IS imminent. Gate destination consent now
    # (never before: a source_conflict / nothing_importable must not prompt).
    plan: PullPlan = outcome
    if plan.scope == "project_shared" and not body.confirm_project_shared:
        return needs_confirmation_envelope(
            f"Pull {kind}/{name} from {plan.selected_runtime} writes to the "
            f"git-tracked project_shared canonical (history is forever). "
            f"Re-send with confirm_project_shared=true after confirming.",
            confirm="confirm_project_shared",
            host_targets=[],
        )
    if plan.scope == "user":
        target = canonical_artifact_dir(plan.kind, "user", plan.project_root) / plan.name
        gate = host_write_gate(
            plan.scope,
            body.allow_host_writes,
            action=f"Pull {kind}/{name} from {plan.selected_runtime}",
            host_targets=[str(target)],
        )
        if gate is not None:
            return gate

    result = await asyncio.to_thread(commit_pull, plan, lock_timeout=_PULL_LOCK_BUDGET_S)
    return _finalize_pull(result, project_root)
