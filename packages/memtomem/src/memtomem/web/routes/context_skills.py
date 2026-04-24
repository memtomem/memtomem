"""Context gateway — Skills CRUD, diff, sync, and import."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import validate_name
from memtomem.context.skills import (
    SKILL_GENERATORS,
    SKILL_MANIFEST,
    canonical_skills_root,
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
    list_canonical_skills,
)
from memtomem.web.deps import get_project_root
from memtomem.web.routes._locks import _gateway_lock

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-skills"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _canonical_skill_dir(project_root: Path, raw_name: str) -> Path:
    """Validate the name via core and return the canonical skill directory."""
    name = validate_name(raw_name, kind="skill")
    return canonical_skills_root(project_root) / name


def _safe_rel(p: Path, project_root: Path) -> str:
    try:
        return str(p.relative_to(project_root))
    except ValueError:
        return str(p)


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/skills")
async def list_skills(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """List canonical skills with per-runtime sync status."""
    canonicals = list_canonical_skills(project_root)
    diffs = diff_skills(project_root)

    # Group diff tuples by skill name
    by_name: dict[str, list[dict]] = {}
    for runtime, skill_name, status in diffs:
        by_name.setdefault(skill_name, []).append({"runtime": runtime, "status": status})

    skills: list[dict[str, object]] = []
    for skill_dir in canonicals:
        skills.append(
            {
                "name": skill_dir.name,
                "canonical_path": str(skill_dir.relative_to(project_root)),
                "runtimes": by_name.get(skill_dir.name, []),
            }
        )

    # Also include runtime-only skills (missing canonical)
    canonical_names = {d.name for d in canonicals}
    for skill_name, runtimes in by_name.items():
        if skill_name not in canonical_names:
            skills.append(
                {
                    "name": skill_name,
                    "canonical_path": None,
                    "runtimes": runtimes,
                }
            )

    return {"skills": skills}


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/skills/{name}")
async def read_skill(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Read a canonical skill's SKILL.md content and list auxiliary files."""
    skill_dir = _canonical_skill_dir(project_root, name)

    manifest = skill_dir / SKILL_MANIFEST
    if not manifest.is_file():
        raise KeyError(name)

    content = manifest.read_text(encoding="utf-8")
    # mtime_ns serialized as string (JS bigint-unsafe).
    mtime_ns = manifest.stat().st_mtime_ns

    # List auxiliary files
    files = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and p.name != SKILL_MANIFEST:
            files.append(
                {
                    "path": str(p.relative_to(skill_dir)),
                    "size": p.stat().st_size,
                }
            )

    return {"name": name, "content": content, "mtime_ns": str(mtime_ns), "files": files}


# ── Create ───────────────────────────────────────────────────────────────


class SkillCreateRequest(BaseModel):
    name: str
    content: str


@router.post("/context/skills")
async def create_skill(
    body: SkillCreateRequest,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Create a new canonical skill."""
    skill_dir = _canonical_skill_dir(project_root, body.name)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if skill_dir.exists():
                    raise ValueError(f"Skill '{body.name}' already exists")
                skill_dir.mkdir(parents=True)
                manifest = skill_dir / SKILL_MANIFEST
                atomic_write_text(manifest, body.content)
    except TimeoutError:
        raise HTTPException(503, "Skill create timed out — another sync may be in progress")
    return {"name": body.name, "canonical_path": str(skill_dir.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class SkillUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str


@router.put("/context/skills/{name}")
async def update_skill(
    name: str,
    body: SkillUpdateRequest,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    """Update a canonical skill's SKILL.md (mtime-guarded, atomic, locked)."""
    skill_dir = _canonical_skill_dir(project_root, name)
    manifest = skill_dir / SKILL_MANIFEST
    if not manifest.is_file():
        raise KeyError(name)

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise HTTPException(422, f"Invalid mtime_ns: {body.mtime_ns!r}")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = manifest.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "aborted",
                            "reason": ("File was modified by another process. Reload and retry."),
                            "mtime_ns": str(current_mtime_ns),
                        },
                    )
                atomic_write_text(manifest, body.content)
                new_mtime_ns = manifest.stat().st_mtime_ns
    except TimeoutError:
        raise HTTPException(503, "Skill update timed out — another sync may be in progress")
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/context/skills/{name}")
async def delete_skill(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Delete a canonical skill, optionally cascading to runtime copies.

    Idempotent: missing canonical directory returns ``deleted: []``.
    """
    skill_dir = _canonical_skill_dir(project_root, name)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []

                if skill_dir.exists():
                    try:
                        shutil.rmtree(skill_dir)
                        removed.append(_safe_rel(skill_dir, project_root))
                    except OSError as e:
                        skipped.append(
                            {"path": _safe_rel(skill_dir, project_root), "reason": str(e)}
                        )

                if cascade:
                    for gen in SKILL_GENERATORS.values():
                        target = gen.target_dir(project_root, name)
                        if not target.exists():
                            continue
                        try:
                            shutil.rmtree(target)
                            removed.append(_safe_rel(target, project_root))
                        except OSError as e:
                            skipped.append(
                                {"path": _safe_rel(target, project_root), "reason": str(e)}
                            )
    except TimeoutError:
        raise HTTPException(503, "Skill delete timed out — another sync may be in progress")

    return {"deleted": removed, "skipped": skipped}


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/skills/{name}/diff")
async def diff_skill(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Per-runtime diff for a single skill (returns text content if out of sync)."""
    skill_dir = _canonical_skill_dir(project_root, name)

    canonical_manifest = skill_dir / SKILL_MANIFEST
    canonical_content = None
    if canonical_manifest.is_file():
        canonical_content = canonical_manifest.read_text(encoding="utf-8")

    runtimes = []
    for gen_name, gen in SKILL_GENERATORS.items():
        target = gen.target_dir(project_root, name)
        target_manifest = target / SKILL_MANIFEST
        if canonical_content is None and not target_manifest.is_file():
            continue
        elif canonical_content is not None and not target_manifest.is_file():
            runtimes.append({"runtime": gen_name, "status": "missing target"})
        elif canonical_content is None and target_manifest.is_file():
            runtimes.append(
                {
                    "runtime": gen_name,
                    "status": "missing canonical",
                    "runtime_content": target_manifest.read_text(encoding="utf-8"),
                }
            )
        else:
            runtime_content = target_manifest.read_text(encoding="utf-8")
            if runtime_content == canonical_content:
                runtimes.append({"runtime": gen_name, "status": "in sync"})
            else:
                runtimes.append(
                    {
                        "runtime": gen_name,
                        "status": "out of sync",
                        "runtime_content": runtime_content,
                    }
                )

    return {
        "name": name,
        "canonical_content": canonical_content,
        "runtimes": runtimes,
    }


# ── Sync (fan-out) ──────────────────────────────────────────────────────


@router.post("/context/skills/sync")
async def sync_skills(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Fan out canonical skills to all runtimes."""
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_skills(project_root)
    except TimeoutError:
        raise HTTPException(503, "Skills sync timed out — another sync may be in progress")
    return {
        "generated": [
            {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in result.generated
        ],
        "skipped": [{"runtime": rt, "reason": reason} for rt, reason in result.skipped],
    }


# ── Import (reverse sync) ───────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False


@router.post("/context/skills/import")
async def import_skills(
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Import runtime skills into canonical .memtomem/skills/."""
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_skills_to_canonical(project_root, overwrite=overwrite)
    except TimeoutError:
        raise HTTPException(503, "Skills import timed out — another sync may be in progress")
    return {
        "imported": [
            {"name": p.name, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in result.skipped],
    }
