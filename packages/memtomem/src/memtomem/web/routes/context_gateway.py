"""Context gateway overview — aggregate sync status across all artifact types."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from memtomem.privacy import scan as _privacy_scan
from memtomem.config import TargetScope
from memtomem.web.deps import get_hooks_target_scope, get_project_root

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


def _detected_runtimes(project_root: Path, scope: str) -> list[dict]:
    """OR-aggregate runtime availability across the four declared surfaces.

    Per ADR-0009 §1 the dashboard chip strip emits one entry per declared
    runtime root with an aggregate ``available`` flag that is ``True`` when
    *any* surface (skills / sub-agents / commands / settings) resolves a
    target on disk. The universe of declared runtimes is the union of
    suffix-stripped keys across ``detector.SKILL_DIRS`` / ``AGENT_DIRS`` /
    ``COMMAND_DIRS`` and ``SETTINGS_GENERATORS``. Skill / agent / command
    availability is directory-probe based (the registry protocols expose
    only ``target_dir()`` / ``target_file()``); settings availability uses
    the generator's ``is_available()`` (the protocol exposes that one
    method because user-scope ``~/.claude/`` cannot be discovered by a
    project-root walk).
    """
    from memtomem.context import detector
    from memtomem.context.settings import SETTINGS_GENERATORS

    universe: set[str] = set()
    for key in detector.SKILL_DIRS:
        universe.add(key.removesuffix("_skills"))
    for key in detector.AGENT_DIRS:
        universe.add(key.removesuffix("_agents"))
    for key in detector.COMMAND_DIRS:
        universe.add(key.removesuffix("_commands"))
    for name in SETTINGS_GENERATORS:
        universe.add(name.removesuffix("_settings"))

    detected: set[str] = set()
    for d in detector.detect_skill_dirs(project_root):
        detected.add(d.agent.removesuffix("_skills"))
    for d in detector.detect_agent_dirs(project_root):
        detected.add(d.agent.removesuffix("_agents"))
    for d in detector.detect_command_dirs(project_root):
        detected.add(d.agent.removesuffix("_commands"))
    for d in detector.detect_settings_files(project_root, scope):
        detected.add(d.agent.removesuffix("_settings"))

    return [{"name": name, "available": name in detected} for name in sorted(universe)]


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
    project_root: Path = Depends(get_project_root),
    scope: str = Depends(get_hooks_target_scope),
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
        settings_diff = diff_settings(project_root, scope=scope)
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

    # detected_runtimes is independent of ``target_scope`` (its surfaces are
    # detected per ADR-0009 §1) but uses the resolved hooks scope for the
    # settings probe so ``detect_settings_files`` walks the same tier that
    # ``diff_settings`` did above. Failures here must not collapse the four
    # tile envelopes — a permission glitch on one detector path would leave
    # the chip strip empty, but the rest of the dashboard stays usable.
    try:
        runtimes = _detected_runtimes(project_root, scope)
    except Exception:
        logger.exception("detect_runtimes failed")
        runtimes = []
    return {"target_scope": target_scope, **result, "detected_runtimes": runtimes}
