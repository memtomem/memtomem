"""Context gateway — Agents CRUD, diff, sync, import, rendered output, and field map."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import validate_name
from memtomem.context.agents import (
    AGENT_GENERATORS,
    CANONICAL_AGENT_ROOT,
    AgentParseError,
    SubAgent,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
    list_canonical_agents,
    parse_canonical_agent,
)
from memtomem.web.deps import get_project_root
from memtomem.web.routes._locks import _gateway_lock

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-agents"])

# Fields present in the canonical SubAgent that may be dropped per-runtime.
_ALL_OPTIONAL_FIELDS = ("tools", "model", "skills", "isolation", "kind", "temperature")


# ── Helpers ──────────────────────────────────────────────────────────────


def _agents_root(project_root: Path) -> Path:
    return project_root / CANONICAL_AGENT_ROOT


def _canonical_agent_path(project_root: Path, raw_name: str) -> Path:
    """Validate the name via core and return the canonical agent path.

    Raises ``InvalidNameError`` (subclass of ``ValueError`` → 400) when the
    name fails the same checks applied in CLI/MCP write paths.
    """
    name = validate_name(raw_name, kind="agent")
    return _agents_root(project_root) / f"{name}.md"


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
    project_root: Path = Depends(get_project_root),
) -> dict:
    canonicals = list_canonical_agents(project_root)
    diffs = diff_agents(project_root)

    by_name: dict[str, list[dict]] = {}
    for runtime, agent_name, status in diffs:
        by_name.setdefault(agent_name, []).append({"runtime": runtime, "status": status})

    agents: list[dict[str, object]] = []
    for agent_path in canonicals:
        name = agent_path.stem
        agents.append(
            {
                "name": name,
                "canonical_path": str(agent_path.relative_to(project_root)),
                "runtimes": by_name.get(name, []),
            }
        )

    canonical_names = {p.stem for p in canonicals}
    for agent_name, runtimes in by_name.items():
        if agent_name not in canonical_names:
            agents.append({"name": agent_name, "canonical_path": None, "runtimes": runtimes})

    return {"agents": agents}


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}")
async def read_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    agent_path = _canonical_agent_path(project_root, name)
    if not agent_path.is_file():
        raise KeyError(name)

    content = agent_path.read_text(encoding="utf-8")
    # mtime_ns is serialized as a string because JavaScript Number cannot
    # safely represent integers > 2^53; nanosecond epochs exceed that.
    mtime_ns = agent_path.stat().st_mtime_ns

    fields: dict = {}
    try:
        parsed = parse_canonical_agent(agent_path)
        fields = _agent_to_dict(parsed)
    except AgentParseError:
        pass

    return {"name": name, "content": content, "mtime_ns": str(mtime_ns), "fields": fields}


# ── Rendered (per-runtime output with dropped fields + field map) ────────


@router.get("/context/agents/{name}/rendered")
async def rendered_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    agent_path = _canonical_agent_path(project_root, name)
    if not agent_path.is_file():
        raise KeyError(name)

    content = agent_path.read_text(encoding="utf-8")
    try:
        parsed = parse_canonical_agent(agent_path)
    except AgentParseError as exc:
        return JSONResponse(status_code=422, content={"detail": f"Parse error: {exc}"})

    diffs = diff_agents(project_root)
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
) -> dict:
    agent_path = _canonical_agent_path(project_root, body.name)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if agent_path.exists():
                    raise ValueError(f"Agent '{body.name}' already exists")
                atomic_write_text(agent_path, body.content)
    except TimeoutError:
        raise HTTPException(503, "Agent create timed out — another sync may be in progress")
    return {"name": body.name, "canonical_path": str(agent_path.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class AgentUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str


@router.put("/context/agents/{name}")
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    agent_path = _canonical_agent_path(project_root, name)
    if not agent_path.is_file():
        raise KeyError(name)

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise HTTPException(422, f"Invalid mtime_ns: {body.mtime_ns!r}")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = agent_path.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "aborted",
                            "reason": ("File was modified by another process. Reload and retry."),
                            "mtime_ns": str(current_mtime_ns),
                        },
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
) -> dict:
    agent_path = _canonical_agent_path(project_root, name)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []

                if agent_path.is_file():
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
) -> dict:
    agent_path = _canonical_agent_path(project_root, name)

    canonical_content = None
    if agent_path.is_file():
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
) -> dict:
    on_drop = body.on_drop if body else "warn"
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_agents(project_root, on_drop=on_drop)
    except TimeoutError:
        raise HTTPException(503, "Agents sync timed out — another sync may be in progress")

    return {
        "generated": [
            {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in result.generated
        ],
        "dropped": [
            {"runtime": rt, "name": name, "fields": fields} for rt, name, fields in result.dropped
        ],
        "skipped": [{"runtime": rt, "reason": reason} for rt, reason in result.skipped],
    }


# ── Import ───────────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False


@router.post("/context/agents/import")
async def import_agents(
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
) -> dict:
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_agents_to_canonical(project_root, overwrite=overwrite)
    except TimeoutError:
        raise HTTPException(503, "Agents import timed out — another sync may be in progress")
    return {
        "imported": [
            {"name": p.stem, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in result.skipped],
    }
