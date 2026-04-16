"""Context gateway — Agents CRUD, diff, sync, import, rendered output, and field map."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-agents"])

# Fields present in the canonical SubAgent that may be dropped per-runtime.
_ALL_OPTIONAL_FIELDS = ("tools", "model", "skills", "isolation", "kind", "temperature")


# ── Helpers ──────────────────────────────────────────────────────────────


def _agents_root(project_root: Path) -> Path:
    return project_root / CANONICAL_AGENT_ROOT


def _validate_name(name: str, project_root: Path) -> Path | None:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    target = _agents_root(project_root) / f"{name}.md"
    try:
        if not target.resolve().is_relative_to(_agents_root(project_root).resolve()):
            return None
    except (ValueError, RuntimeError):
        return None
    return target


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
    agent_path = _validate_name(name, project_root)
    if agent_path is None:
        raise ValueError(f"Invalid agent name: {name}")
    if not agent_path.is_file():
        raise KeyError(name)

    content = agent_path.read_text(encoding="utf-8")
    mtime = agent_path.stat().st_mtime

    fields: dict = {}
    try:
        parsed = parse_canonical_agent(agent_path)
        fields = _agent_to_dict(parsed)
    except AgentParseError:
        pass

    return {"name": name, "content": content, "mtime": mtime, "fields": fields}


# ── Rendered (per-runtime output with dropped fields + field map) ────────


@router.get("/context/agents/{name}/rendered")
async def rendered_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    agent_path = _validate_name(name, project_root)
    if agent_path is None:
        raise ValueError(f"Invalid agent name: {name}")
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
    agent_path = _validate_name(body.name, project_root)
    if agent_path is None:
        raise ValueError(f"Invalid agent name: {body.name}")
    if agent_path.exists():
        raise ValueError(f"Agent '{body.name}' already exists")

    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(body.content, encoding="utf-8")
    return {"name": body.name, "canonical_path": str(agent_path.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class AgentUpdateRequest(BaseModel):
    content: str
    mtime: float


@router.put("/context/agents/{name}")
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    agent_path = _validate_name(name, project_root)
    if agent_path is None:
        raise ValueError(f"Invalid agent name: {name}")
    if not agent_path.is_file():
        raise KeyError(name)

    current_mtime = agent_path.stat().st_mtime
    if current_mtime != body.mtime:
        return JSONResponse(
            status_code=409,
            content={
                "status": "aborted",
                "reason": "File was modified by another process. Reload and retry.",
                "mtime": current_mtime,
            },
        )

    agent_path.write_text(body.content, encoding="utf-8")
    return JSONResponse(content={"name": name, "mtime": agent_path.stat().st_mtime})


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/context/agents/{name}")
async def delete_agent(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(get_project_root),
) -> dict:
    agent_path = _validate_name(name, project_root)
    if agent_path is None:
        raise ValueError(f"Invalid agent name: {name}")
    if not agent_path.is_file():
        raise KeyError(name)

    agent_path.unlink()
    removed = [str(agent_path.relative_to(project_root))]

    if cascade:
        for gen in AGENT_GENERATORS.values():
            target = gen.target_file(project_root, name)
            if target.is_file():
                target.unlink()
                try:
                    removed.append(str(target.relative_to(project_root)))
                except ValueError:
                    removed.append(str(target))

    return {"deleted": removed}


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}/diff")
async def diff_agent(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    agent_path = _validate_name(name, project_root)
    if agent_path is None:
        raise ValueError(f"Invalid agent name: {name}")

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
    result = generate_all_agents(project_root, on_drop=on_drop)

    def _safe_rel(p: Path) -> str:
        try:
            return str(p.relative_to(project_root))
        except ValueError:
            return str(p)

    return {
        "generated": [{"runtime": rt, "path": _safe_rel(p)} for rt, p in result.generated],
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
    result = extract_agents_to_canonical(project_root, overwrite=overwrite)
    return {
        "imported": [
            {"name": p.stem, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in result.skipped],
    }
