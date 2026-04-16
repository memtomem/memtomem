"""Context gateway — Skills CRUD, diff, sync, and import."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-skills"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _validate_name(name: str, project_root: Path) -> Path | None:
    """Return the canonical skill dir, or ``None`` if the name is invalid."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    target = canonical_skills_root(project_root) / name
    try:
        if not target.resolve().is_relative_to(canonical_skills_root(project_root).resolve()):
            return None
    except (ValueError, RuntimeError):
        return None
    return target


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
    skill_dir = _validate_name(name, project_root)
    if skill_dir is None:
        raise ValueError(f"Invalid skill name: {name}")

    manifest = skill_dir / SKILL_MANIFEST
    if not manifest.is_file():
        raise KeyError(name)

    content = manifest.read_text(encoding="utf-8")
    mtime = manifest.stat().st_mtime

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

    return {"name": name, "content": content, "mtime": mtime, "files": files}


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
    skill_dir = _validate_name(body.name, project_root)
    if skill_dir is None:
        raise ValueError(f"Invalid skill name: {body.name}")
    if skill_dir.exists():
        raise ValueError(f"Skill '{body.name}' already exists")

    skill_dir.mkdir(parents=True)
    manifest = skill_dir / SKILL_MANIFEST
    manifest.write_text(body.content, encoding="utf-8")
    return {"name": body.name, "canonical_path": str(skill_dir.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class SkillUpdateRequest(BaseModel):
    content: str
    mtime: float


@router.put("/context/skills/{name}")
async def update_skill(
    name: str,
    body: SkillUpdateRequest,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    """Update a canonical skill's SKILL.md (mtime-guarded)."""
    skill_dir = _validate_name(name, project_root)
    if skill_dir is None:
        raise ValueError(f"Invalid skill name: {name}")

    manifest = skill_dir / SKILL_MANIFEST
    if not manifest.is_file():
        raise KeyError(name)

    # mtime guard
    current_mtime = manifest.stat().st_mtime
    if current_mtime != body.mtime:
        return JSONResponse(
            status_code=409,
            content={
                "status": "aborted",
                "reason": "File was modified by another process. Reload and retry.",
                "mtime": current_mtime,
            },
        )

    manifest.write_text(body.content, encoding="utf-8")
    new_mtime = manifest.stat().st_mtime
    return JSONResponse(content={"name": name, "mtime": new_mtime})


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/context/skills/{name}")
async def delete_skill(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Delete a canonical skill, optionally cascading to runtime copies."""
    skill_dir = _validate_name(name, project_root)
    if skill_dir is None:
        raise ValueError(f"Invalid skill name: {name}")
    if not skill_dir.exists():
        raise KeyError(name)

    shutil.rmtree(skill_dir)
    removed = [str(skill_dir.relative_to(project_root))]

    if cascade:
        for gen in SKILL_GENERATORS.values():
            target = gen.target_dir(project_root, name)
            if target.exists():
                shutil.rmtree(target)
                removed.append(str(target.relative_to(project_root)))

    return {"deleted": removed}


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/skills/{name}/diff")
async def diff_skill(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Per-runtime diff for a single skill (returns text content if out of sync)."""
    skill_dir = _validate_name(name, project_root)
    if skill_dir is None:
        raise ValueError(f"Invalid skill name: {name}")

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
    result = generate_all_skills(project_root)
    return {
        "generated": [
            {"runtime": rt, "path": str(p.relative_to(project_root))} for rt, p in result.generated
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
    result = extract_skills_to_canonical(project_root, overwrite=overwrite)
    return {
        "imported": [
            {"name": p.name, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in result.skipped],
    }
