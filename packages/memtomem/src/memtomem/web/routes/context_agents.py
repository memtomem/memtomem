"""Context gateway — Agents CRUD, diff, sync, import, rendered output, and field map."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.config import TargetScope
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.agents import (
    AGENT_GENERATORS,
    CANONICAL_AGENT_ROOT,
    AgentParseError,
    SubAgent,
    canonical_agent_name,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
    list_canonical_agents,
    parse_canonical_agent,
    resolve_canonical_agent,
)
from memtomem.context.detector import AGENT_DIRS
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.web.deps import get_project_root
from memtomem.web.routes.context_projects import resolve_scope_root
from memtomem.web.routes._locks import _gateway_lock

# Flat list of project-relative runtime scan paths reported on list / import
# responses so the web UI's empty-state hint can name the exact directories
# the detector inspects without hardcoding them client-side.
_AGENT_SCAN_DIRS: list[str] = [d for paths in AGENT_DIRS.values() for d in paths]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-agents"])

# Fields present in the canonical SubAgent that may be dropped per-runtime.
_ALL_OPTIONAL_FIELDS = ("tools", "model", "skills", "isolation", "kind", "temperature")


# ── Helpers ──────────────────────────────────────────────────────────────


def _agents_root(project_root: Path, *, scope: TargetScope = "project_shared") -> Path:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    return canonical_artifact_dir("agents", scope, project_root)


def _canonical_agent_path(
    project_root: Path, raw_name: str, *, scope: TargetScope = "project_shared"
) -> Path:
    """Validate the name via core and return the canonical agent path.

    Raises ``InvalidNameError`` (subclass of ``ValueError`` → 400) when the
    name fails the same checks applied in CLI/MCP write paths.

    ``scope`` selects the canonical residency tier (ADR-0016).
    """
    name = validate_name(raw_name, kind="agent")
    return _agents_root(project_root, scope=scope) / f"{name}.md"


def _resolve_existing_agent(
    project_root: Path, raw_name: str, *, scope: TargetScope = "project_shared"
):
    name = validate_name(raw_name, kind="agent")
    return name, resolve_canonical_agent(project_root, name, scope=scope)


def _reject_non_shared_write(target_scope: TargetScope, action: str) -> None:
    """Reject writes on non-``project_shared`` tiers with HTTP 400.

    See context_skills.py:_reject_non_shared_write for the rationale.
    Mirrored here so each route file stays self-contained.
    """
    if target_scope != "project_shared":
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action} is supported only on project_shared in this release; "
                f"got target_scope={target_scope!r}."
            ),
        )


def _agent_to_dict(agent: SubAgent) -> dict:
    return {
        "name": agent.name,
        "description": agent.description,
        "tools": agent.tools,
        "model": agent.model,
        "skills": agent.skills,
        "isolation": agent.isolation,
        "kind": agent.kind,
        "temperature": agent.temperature,
    }


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/agents")
async def list_agents(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to list. project_local is shown only "
            "when explicitly requested."
        ),
    ),
) -> dict:
    """List canonical agents. Accepts ``?scope_id=`` like list_skills."""
    canonicals = list_canonical_agents(project_root, scope=target_scope)
    diffs = diff_agents(project_root, scope=target_scope)

    by_name: dict[str, list[dict]] = {}
    for runtime, agent_name, status in diffs:
        by_name.setdefault(agent_name, []).append({"runtime": runtime, "status": status})

    agents: list[dict[str, object]] = []
    canonical_names: set[str] = set()
    for agent_path, layout in canonicals:
        name = canonical_agent_name(agent_path, layout)
        canonical_names.add(name)
        agents.append(
            {
                "name": name,
                "canonical_path": _safe_rel(agent_path, project_root),
                "target_scope": target_scope,
                "runtimes": by_name.get(name, []),
            }
        )

    for agent_name, runtimes in by_name.items():
        if agent_name not in canonical_names:
            agents.append(
                {
                    "name": agent_name,
                    "canonical_path": None,
                    "target_scope": target_scope,
                    "runtimes": runtimes,
                }
            )

    return {
        "agents": agents,
        "canonical_root": CANONICAL_AGENT_ROOT,
        "scanned_dirs": _AGENT_SCAN_DIRS,
    }


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}")
async def read_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to read from (ADR-0016).",
    ),
) -> dict:
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)
    if resolved is None:
        raise KeyError(name)
    agent_path, layout = resolved

    content = agent_path.read_text(encoding="utf-8")
    # mtime_ns is serialized as a string because JavaScript Number cannot
    # safely represent integers > 2^53; nanosecond epochs exceed that.
    mtime_ns = agent_path.stat().st_mtime_ns

    fields: dict = {}
    try:
        parsed = parse_canonical_agent(agent_path, layout=layout)
        fields = _agent_to_dict(parsed)
    except AgentParseError:
        pass

    return {"name": name, "content": content, "mtime_ns": str(mtime_ns), "fields": fields}


# ── Rendered (per-runtime output with dropped fields + field map) ────────


@router.get("/context/agents/{name}/rendered")
async def rendered_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to render (ADR-0016).",
    ),
) -> JSONResponse:
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)
    if resolved is None:
        raise KeyError(name)
    agent_path, layout = resolved

    content = agent_path.read_text(encoding="utf-8")
    try:
        parsed = parse_canonical_agent(agent_path, layout=layout)
    except AgentParseError as exc:
        return JSONResponse(status_code=422, content={"detail": f"Parse error: {exc}"})

    diffs = diff_agents(project_root, scope=target_scope)
    status_map: dict[tuple[str, str], str] = {(rt, n): s for rt, n, s in diffs}

    runtimes = []
    field_map: dict[str, dict[str, bool]] = {f: {} for f in _ALL_OPTIONAL_FIELDS}

    for gen_name, gen in AGENT_GENERATORS.items():
        rendered_content, dropped_fields = gen.render(parsed)
        status = status_map.get((gen_name, name), "unknown")
        runtimes.append(
            {
                "runtime": gen_name,
                "content": rendered_content,
                "dropped_fields": dropped_fields,
                "status": status,
            }
        )
        # Build field map
        dropped_set = set(dropped_fields)
        for f in _ALL_OPTIONAL_FIELDS:
            field_map[f][gen_name] = f not in dropped_set

    return JSONResponse(
        content={
            "name": name,
            "canonical_content": content,
            "fields": _agent_to_dict(parsed),
            "runtimes": runtimes,
            "field_map": field_map,
        }
    )


# ── Create ───────────────────────────────────────────────────────────────


class AgentCreateRequest(BaseModel):
    name: str
    content: str


@router.post("/context/agents")
async def create_agent(
    body: AgentCreateRequest,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to create in. Non-shared rejected (#940).",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Create agent")
    name = validate_name(body.name, kind="agent")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if resolve_canonical_agent(project_root, name, scope=target_scope) is not None:
                    raise HTTPException(409, detail=f"Agent '{name}' already exists")
                agent_path = _canonical_agent_path(project_root, name, scope=target_scope)
                atomic_write_text(agent_path, body.content)
    except TimeoutError:
        raise HTTPException(503, "Agent create timed out — another sync may be in progress")
    return {"name": name, "canonical_path": str(agent_path.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class AgentUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str
    # Bypass the mtime guard. The Web UI sets this only after the user
    # explicitly chose "Force save" in the conflict resolution dialog
    # (see issue #763); every force-save emits a WARNING with both mtime
    # values for the audit trail.
    force: bool = False


@router.put("/context/agents/{name}")
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to update. Non-shared rejected (#940).",
    ),
) -> JSONResponse:
    _reject_non_shared_write(target_scope, "Update agent")
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)
    if resolved is None:
        raise KeyError(name)
    agent_path, _layout = resolved

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise HTTPException(422, f"Invalid mtime_ns: {body.mtime_ns!r}")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = agent_path.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    if not body.force:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "status": "aborted",
                                "reason": (
                                    "File was modified by another process. Reload and retry."
                                ),
                                "mtime_ns": str(current_mtime_ns),
                            },
                        )
                    logger.warning(
                        "force-save bypassed mtime check on %s "
                        "(client_mtime_ns=%s server_mtime_ns=%s)",
                        agent_path,
                        body_mtime_ns,
                        current_mtime_ns,
                    )
                atomic_write_text(agent_path, body.content)
                new_mtime_ns = agent_path.stat().st_mtime_ns
    except TimeoutError:
        raise HTTPException(503, "Agent update timed out — another sync may be in progress")
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


# ── Delete ───────────────────────────────────────────────────────────────


def _safe_rel(p: Path, project_root: Path) -> str:
    try:
        return str(p.relative_to(project_root))
    except ValueError:
        return str(p)


@router.delete("/context/agents/{name}")
async def delete_agent(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to delete from. Non-shared rejected (#940).",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Delete agent")
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []

                if resolved is not None:
                    agent_path, _layout = resolved
                    try:
                        agent_path.unlink()
                        removed.append(_safe_rel(agent_path, project_root))
                    except OSError as e:
                        skipped.append(
                            {"path": _safe_rel(agent_path, project_root), "reason": str(e)}
                        )

                if cascade:
                    for gen in AGENT_GENERATORS.values():
                        target = gen.target_file(project_root, name)
                        if not target.is_file():
                            continue
                        try:
                            target.unlink()
                            removed.append(_safe_rel(target, project_root))
                        except OSError as e:
                            skipped.append(
                                {"path": _safe_rel(target, project_root), "reason": str(e)}
                            )
    except TimeoutError:
        raise HTTPException(503, "Agent delete timed out — another sync may be in progress")

    return {"deleted": removed, "skipped": skipped}


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}/diff")
async def diff_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to diff against runtime fan-out (ADR-0016).",
    ),
) -> dict:
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)

    canonical_content = None
    if resolved is not None:
        agent_path, _layout = resolved
        canonical_content = agent_path.read_text(encoding="utf-8")

    runtimes = []
    for gen_name, gen in AGENT_GENERATORS.items():
        target = gen.target_file(project_root, name)
        if canonical_content is None and not target.is_file():
            continue
        elif canonical_content is not None and not target.is_file():
            runtimes.append({"runtime": gen_name, "status": "missing target"})
        elif canonical_content is None and target.is_file():
            runtimes.append(
                {
                    "runtime": gen_name,
                    "status": "missing canonical",
                    "runtime_content": target.read_text(encoding="utf-8"),
                }
            )
        else:
            runtime_content = target.read_text(encoding="utf-8")
            runtimes.append(
                {
                    "runtime": gen_name,
                    "status": "out of sync" if runtime_content != canonical_content else "in sync",
                    "runtime_content": runtime_content,
                }
            )

    return {"name": name, "canonical_content": canonical_content, "runtimes": runtimes}


# ── Sync ─────────────────────────────────────────────────────────────────


class SyncRequest(BaseModel):
    on_drop: str = "warn"


@router.post("/context/agents/sync")
async def sync_agents(
    body: SyncRequest | None = None,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to fan out. Non-shared rejected (#940).",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Sync agents")
    on_drop = body.on_drop if body else "warn"
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_agents(project_root, on_drop=on_drop)
    except TimeoutError:
        raise HTTPException(503, "Agents sync timed out — another sync may be in progress")
    except PrivacyScanError as exc:
        # 422 Unprocessable Entity — request is well-formed but the canonical
        # bytes violate the project_shared privacy gate. ADR-0011 §5: no
        # bypass valve, so the user must remove the secret or migrate the
        # artifact to a writable tier (the message body explains how).
        raise HTTPException(422, exc.message) from exc

    return {
        "generated": [
            {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in result.generated
        ],
        "dropped": [
            {"runtime": rt, "name": name, "fields": fields} for rt, name, fields in result.dropped
        ],
        "skipped": [
            {"runtime": rt, "reason": reason, "reason_code": code}
            for rt, reason, code in result.skipped
        ],
        "canonical_root": CANONICAL_AGENT_ROOT,
    }


# ── Import ───────────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False


@router.post("/context/agents/import")
async def import_agents(
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to import into. Non-shared rejected (#940).",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Import agents")
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_agents_to_canonical(project_root, overwrite=overwrite)
    except TimeoutError:
        raise HTTPException(503, "Agents import timed out — another sync may be in progress")
    return {
        "imported": [
            {
                "name": canonical_agent_name(p, layout),
                "canonical_path": str(p.relative_to(project_root)),
            }
            for p, layout in result.imported
        ],
        "skipped": [
            {"name": name, "reason": reason, "reason_code": code}
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _AGENT_SCAN_DIRS,
    }


@router.post("/context/agents/{name}/import")
async def import_agent(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to import into. Non-shared rejected (#940).",
    ),
) -> dict:
    """Import a single runtime agent into ``.memtomem/agents/``.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime file matches the name (the section
    import would silently report 0 imported, which is the wrong shape of
    feedback for "you clicked a specific item that doesn't exist").
    """
    _reject_non_shared_write(target_scope, "Import agent")
    try:
        validate_name(name, kind="agent name")
    except InvalidNameError as exc:
        raise HTTPException(400, f"Invalid agent name: {exc}")
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_agents_to_canonical(
                    project_root, overwrite=overwrite, only_name=name
                )
    except TimeoutError:
        raise HTTPException(503, "Agent import timed out — another sync may be in progress")
    if not result.imported and not result.skipped:
        raise HTTPException(404, f"No runtime agent named {name!r} to import")
    return {
        "imported": [
            {
                "name": canonical_agent_name(p, layout),
                "canonical_path": str(p.relative_to(project_root)),
            }
            for p, layout in result.imported
        ],
        "skipped": [
            {"name": n, "reason": reason, "reason_code": code} for n, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _AGENT_SCAN_DIRS,
    }
