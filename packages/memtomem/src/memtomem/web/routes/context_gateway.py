"""Context gateway overview — aggregate sync status across all artifact types."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from memtomem.privacy import scan as _privacy_scan
from memtomem.config import TargetScope
from memtomem.web.routes.context_projects import resolve_scope_root

try:
    import tomllib
except ImportError:  # pragma: no cover — py<3.11 fallback, repo targets py312
    tomllib = None  # type: ignore[assignment]

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — PyYAML may be absent on minimal installs
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-gateway"])

_HOME = str(Path.home())
_ERROR_MESSAGE_LIMIT = 200
_SECRET_REDACTED_MARKER = "<redacted: secret-shape>"


def _count_statuses(triples: list[tuple[str, str, str]]) -> dict:
    """Summarise ``(runtime, name, status)`` triples into per-status counts."""
    names: set[str] = set()
    counts: dict[str, int] = {}
    for _runtime, name, status in triples:
        names.add(name)
        key = status.replace(" ", "_")
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(names), **counts}


def _count_context_statuses(
    triples: list[tuple[str, str, str]],
    canonical_names: set[str],
) -> dict:
    """Summarise runtime diffs plus canonical-only draft rows.

    ``project_local`` agents / skills / commands have no runtime fan-out, so
    their diff list can be empty even when canonical drafts exist. Count the
    canonical names explicitly so overview totals match list views.
    """
    result = _count_statuses(triples)
    runtime_names = {name for _runtime, name, _status in triples}
    canonical_only = canonical_names - runtime_names
    if canonical_only:
        result["total"] = len(runtime_names | canonical_names)
        result["local_draft"] = len(canonical_only)
    return result


def _classify_exception(exc: BaseException) -> str:
    """Map an exception to one of {parse, permission, missing, internal}.

    Order matters: ``PermissionError`` and ``FileNotFoundError`` are both
    ``OSError`` subclasses, so they must be checked before bare ``OSError``.
    Generic ``OSError`` is ``internal`` rather than ``permission``/``missing``
    because ``errno`` may be ``EIO``/``EMFILE``/``ELOOP`` etc.
    """
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return "missing"
    if isinstance(exc, ModuleNotFoundError):
        return "missing"
    if isinstance(exc, UnicodeDecodeError):
        return "parse"
    if isinstance(exc, json.JSONDecodeError):
        return "parse"
    if tomllib is not None and isinstance(exc, tomllib.TOMLDecodeError):
        return "parse"
    if yaml is not None and isinstance(exc, yaml.YAMLError):
        return "parse"
    return "internal"


def _redact_message(message: str) -> str:
    """Collapse ``$HOME`` → ``~``, drop secret-shape messages, then truncate.

    The ``internal`` classification is a catch-all for unexpected
    exceptions, so ``str(exc)`` may incidentally contain provider tokens,
    PEM headers, or ``api_key=...`` fragments pulled from a config parse
    or a third-party library's error. Truncation alone leaves the first
    200 chars verbatim, which is not enough at this trust boundary.

    We reuse the LTM secret-class scanner from ``memtomem.privacy``. If
    *any* hit is detected, the whole message is replaced with a fixed
    marker. Span-splicing was considered and rejected: several patterns
    (notably ``api_key=...``) match the assignment anchor only, so the
    secret *value* would survive a span splice. Whole-message replace
    matches the convention already established in
    ``privacy._sanitize_audit_value``. The ``error_kind`` field still
    tells the operator which category the failure fell into.
    """
    redacted = message.replace(_HOME, "~") if _HOME else message
    if _privacy_scan(redacted):
        return _SECRET_REDACTED_MARKER
    if len(redacted) > _ERROR_MESSAGE_LIMIT:
        redacted = redacted[:_ERROR_MESSAGE_LIMIT]
    return redacted


def sanitize_diff_reason(message: str | None, project_root: Path) -> str | None:
    """Display-sanitize an engine diff-row ``reason`` for the wire (#1229 U7).

    Engine reasons are raw exception text with absolute source paths
    EMBEDDED in arbitrary message strings (not bare paths), so plain
    ``Path.relative_to`` doesn't apply: strip the project-root prefix
    wherever it appears inside the message, then apply the same
    HOME-collapse + secret-shape whole-replace + truncation contract as
    the overview ``error_message`` field. Shared by every context_* list
    and per-name diff route so the sanitization boundary cannot drift
    per kind.
    """
    if not message:
        return None
    root = str(project_root)
    cleaned = message.replace(root + os.sep, "").replace(root, ".")
    return _redact_message(cleaned)


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


@router.get("/context/overview")
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

    result: dict[str, dict[str, int | bool | str]] = {}

    try:
        result["skills"] = _count_context_statuses(
            diff_skills(project_root, scope=target_scope),
            {p.name for p in list_canonical_skills(project_root, scope=target_scope)},
        )
    except Exception as exc:
        logger.exception("diff_skills failed")
        result["skills"] = _error_payload(exc, shape="total")

    try:
        result["commands"] = _count_context_statuses(
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
        result["agents"] = _count_context_statuses(
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
            result["mcp_servers"] = _count_context_statuses(
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
        statuses = [r.status for r in settings_diff.values()]
        # `total` counts only **applicable** generators (runtime installed +
        # canonical source present). `skipped` items are N/A — including them
        # would make the dashboard read "1/2 synced" even when the second slot
        # is "no Codex installed", which misleads the user about actionable work.
        total_applicable = sum(1 for s in statuses if s != "skipped")
        # diff_settings emits 5 status values (settings.py:386-404):
        # `in sync`, `out of sync`, `missing target`, `error`, `skipped`.
        # All four non-skipped categories must be represented as count
        # fields so `in_sync + out_of_sync + missing_target + error ==
        # total_applicable` holds — that contract lets future consumers
        # render per-status segments without the count silently dropping
        # entries on the floor. `missing target` is the common first-use
        # state (existing is None — settings.py:403-404), parallel to
        # how skills/commands/agents already emit `missing_target`.
        in_sync = sum(1 for s in statuses if s == "in sync")
        out_of_sync = sum(1 for s in statuses if s == "out of sync")
        missing_target = sum(1 for s in statuses if s == "missing target")
        error_count = sum(1 for s in statuses if s == "error")
        if all(s in ("in sync", "skipped") for s in statuses):
            status = "in_sync"
        elif any(s == "error" for s in statuses):
            # In-band error: per-file failure already classified by diff_settings.
            # No error_kind here — adding one would conflate distinct per-file causes.
            status = "error"
        else:
            status = "out_of_sync"
        # `error` is a count here (parallel to `out_of_sync` / `in_sync` /
        # `missing_target`), NOT the bool flag `_error_payload(shape="total")`
        # emits when the whole call raises. The two shapes are on disjoint
        # code paths. The frontend uses truthiness on `d.error` (any
        # positive int OR the bool `true` reaches the danger render at
        # context-gateway.js:136-145), so `error: 0` correctly skips the
        # danger branch and `error: >=1` reaches it — both shapes work.
        result["settings"] = {
            "total": total_applicable,
            "in_sync": in_sync,
            "out_of_sync": out_of_sync,
            "missing_target": missing_target,
            "error": error_count,
            "status": status,
        }
    except Exception as exc:
        logger.exception("diff_settings failed")
        result["settings"] = _error_payload(exc, shape="status")

    try:
        detected_runtimes = _compute_detected_runtimes(project_root)
    except Exception:
        logger.exception("_compute_detected_runtimes failed")
        detected_runtimes = []

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
        **result,
    }


@router.get("/context/runtimes")
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
    """
    from memtomem.context.runtime_registry import probe_all_runtimes

    try:
        runtimes = [s.to_dict() for s in probe_all_runtimes(project_root)]
    except Exception:
        logger.exception("probe_all_runtimes failed")
        runtimes = []
    return {"project_root": str(project_root), "runtimes": runtimes}
