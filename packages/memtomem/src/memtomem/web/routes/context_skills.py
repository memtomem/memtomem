"""Context gateway — Skills CRUD, diff, sync, and import."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.detector import SKILL_DIRS
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.skills import (
    CANONICAL_SKILL_ROOT,
    SKILL_GENERATORS,
    SKILL_MANIFEST,
    canonical_skills_root,
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
    list_canonical_skills,
)
from memtomem.config import TargetScope
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_projects import (
    resolve_scope_root,
    resolve_scope_root_cascade_gated,
    resolve_writable_scope_root,
)

# Flat list of project-relative runtime scan paths reported on list / import
# responses so the web UI's empty-state hint can name the exact directories
# the detector inspects without hardcoding them client-side.
_SKILL_SCAN_DIRS: list[str] = [d for paths in SKILL_DIRS.values() for d in paths]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-skills"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _canonical_skill_dir(
    project_root: Path, raw_name: str, *, scope: TargetScope = "project_shared"
) -> Path:
    """Validate the name via core and return the canonical skill directory.

    ``scope`` selects the canonical residency tier (ADR-0011 PR-E3 / ADR-0016).
    Default ``project_shared`` preserves pre-ADR-0011 behavior.
    """
    name = validate_name(raw_name, kind="skill")
    return canonical_skills_root(project_root, scope=scope) / name


def _reject_non_shared_write(target_scope: TargetScope, action: str) -> None:
    """Reject writes on non-``project_shared`` tiers with HTTP 400.

    Reads honor every tier (the canonical content differs per scope), but
    write/sync/import endpoints stay on ``project_shared`` for v1 — the
    multi-scope write contract is intentionally deferred to a follow-up so
    each tier's authoring policy (draft vs runtime fan-out vs user-share)
    can be designed deliberately. Passing the param explicitly + rejecting
    here is preferred over silently overriding the client's selection,
    because silent override is exactly the cross-tier crossover (ADR-0011) P1
    flagged ("mutates a same-named project_shared artifact while the UI is
    showing a user/local row").
    """
    if target_scope != "project_shared":
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action} is supported only on project_shared in this release; "
                f"got target_scope={target_scope!r}."
            ),
        )


def _safe_rel(p: Path, project_root: Path) -> str:
    try:
        return str(p.relative_to(project_root))
    except ValueError:
        return str(p)


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/skills")
async def list_skills(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to list. project_local is shown only "
            "when explicitly requested."
        ),
    ),
) -> dict:
    """List canonical skills with per-runtime sync status.

    Without a project selector, lists for the server cwd (legacy single-project
    path). With ``?project_scope_id=`` from ``GET /api/context/projects``
    (or its permanent ``?scope_id=`` alias), lists for that scope's root.
    The detail and write routes use the same scope resolver, so the Web UI's
    active-project switcher can manage any registered project without
    restarting ``mm web``.
    """
    canonicals = list_canonical_skills(project_root, scope=target_scope)
    diffs = diff_skills(project_root, scope=target_scope)

    # Group diff tuples by skill name
    by_name: dict[str, list[dict]] = {}
    for runtime, skill_name, status in diffs:
        by_name.setdefault(skill_name, []).append({"runtime": runtime, "status": status})

    skills: list[dict[str, object]] = []
    for skill_dir in canonicals:
        skills.append(
            {
                "name": skill_dir.name,
                "canonical_path": _safe_rel(skill_dir, project_root),
                "target_scope": target_scope,
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
                    "target_scope": target_scope,
                    "runtimes": runtimes,
                }
            )

    return {
        "skills": skills,
        "canonical_root": CANONICAL_SKILL_ROOT,
        "scanned_dirs": _SKILL_SCAN_DIRS,
    }


# ── Read ─────────────────────────────────────────────────────────────────


_SKILL_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_SKILL_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def _parse_skill_description(content: str) -> str:
    """Pull ``description:`` out of SKILL.md frontmatter.

    Skills don't have a structured parser (unlike agents/commands) — they
    are intentionally opaque to the canonical layer. For the detail
    panel header (#962) we just need the description string for display,
    so a flat-YAML scrape mirrors the same shape ``context/agents.py``
    uses. Falls back to the first non-blank body line so the UI never
    renders a blank meta header for a description-less skill.
    """
    m = _SKILL_FRONT_MATTER_RE.match(content)
    if m:
        for line in m.group(1).splitlines():
            kv = _SKILL_KEY_VALUE_RE.match(line)
            if kv and kv.group(1).lower() == "description":
                value = kv.group(2).strip().strip("\"'")
                if value:
                    return value
        body = content[m.end() :]
    else:
        body = content
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


@router.get("/context/skills/{name}")
async def read_skill(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to read from (ADR-0016).",
    ),
) -> dict:
    """Read a canonical skill's SKILL.md content and list auxiliary files."""
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)

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

    # Issue #962 detail meta header — surface fields the JS used to either
    # ignore (target_scope) or have to re-derive (layout, description).
    # ``layout`` is always ``"dir"`` for canonical skills since they live
    # under ``<name>/SKILL.md``; commit the field on the response anyway
    # so the JS meta-header renderer stays type-agnostic across skills /
    # agents / commands.
    fields = {"description": _parse_skill_description(content)}
    return {
        "name": name,
        "content": content,
        "mtime_ns": str(mtime_ns),
        "files": files,
        "target_scope": target_scope,
        "layout": "dir",
        "fields": fields,
    }


# ── Create ───────────────────────────────────────────────────────────────


class SkillCreateRequest(BaseModel):
    name: str
    content: str


@router.post("/context/skills")
async def create_skill(
    body: SkillCreateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to create in. Non-shared tiers rejected (ADR-0011).",
    ),
) -> dict:
    """Create a new canonical skill."""
    _reject_non_shared_write(target_scope, "Create skill")
    skill_dir = _canonical_skill_dir(project_root, body.name, scope=target_scope)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if skill_dir.exists():
                    # 409 Conflict, matching create_agent / create_command.
                    # A bare ValueError here maps to HTTP 400 via the app's
                    # value_error_handler, diverging from the sibling routes
                    # (and from the Web UI's 409 conflict-resolution flow).
                    raise HTTPException(409, detail=f"Skill '{body.name}' already exists")
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
    # Bypass the mtime guard. The Web UI sets this only after the user
    # explicitly chose "Force save" in the conflict resolution dialog
    # (see issue #763); every force-save emits a WARNING with both mtime
    # values for the audit trail.
    force: bool = False


@router.put("/context/skills/{name}")
async def update_skill(
    name: str,
    body: SkillUpdateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to update. Non-shared tiers rejected (ADR-0011).",
    ),
) -> JSONResponse:
    """Update a canonical skill's SKILL.md (mtime-guarded, atomic, locked)."""
    _reject_non_shared_write(target_scope, "Update skill")
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)
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
                        manifest,
                        body_mtime_ns,
                        current_mtime_ns,
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
    project_root: Path = Depends(resolve_scope_root_cascade_gated),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to delete from. Non-shared tiers rejected (ADR-0011).",
    ),
) -> dict:
    """Delete a canonical skill, optionally cascading to runtime copies.

    Idempotent: missing canonical directory returns ``deleted: []``.
    """
    _reject_non_shared_write(target_scope, "Delete skill")
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)

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
                        if target is None:
                            continue
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
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to diff against runtime fan-out (ADR-0016).",
    ),
) -> dict:
    """Per-runtime diff for a single skill (returns text content if out of sync)."""
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)

    canonical_manifest = skill_dir / SKILL_MANIFEST
    canonical_content = None
    if canonical_manifest.is_file():
        canonical_content = canonical_manifest.read_text(encoding="utf-8")

    runtimes = []
    for gen_name, gen in SKILL_GENERATORS.items():
        target = gen.target_dir(project_root, name)
        if target is None:
            continue
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
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to fan out. Non-shared rejected — "
            "project_local has no runtime fan-out per ADR-0011 §3, and user-tier "
            "sync is deferred to a follow-up (ADR-0016)."
        ),
    ),
) -> dict:
    """Fan out canonical skills to all runtimes."""
    _reject_non_shared_write(target_scope, "Sync skills")
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_skills(project_root)
    except TimeoutError:
        raise HTTPException(503, "Skills sync timed out — another sync may be in progress")
    except PrivacyScanError as exc:
        raise HTTPException(422, exc.message) from exc
    return {
        "generated": [
            {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in result.generated
        ],
        "skipped": [
            {"runtime": rt, "reason": reason, "reason_code": code}
            for rt, reason, code in result.skipped
        ],
        "canonical_root": CANONICAL_SKILL_ROOT,
    }


# ── Import (reverse sync) ───────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False


@router.post("/context/skills/import")
async def import_skills(
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to import into. Non-shared tiers rejected (ADR-0011).",
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the import without writing to canonical (rank-10): runs the "
            "full scan + privacy walk + dedup and returns the would-import / would-"
            "skip counts, leaving disk untouched."
        ),
    ),
) -> dict:
    """Import runtime skills into canonical .memtomem/skills/."""
    _reject_non_shared_write(target_scope, "Import skills")
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_skills_to_canonical(
                    project_root, overwrite=overwrite, dry_run=dry_run
                )
    except TimeoutError:
        raise HTTPException(503, "Skills import timed out — another sync may be in progress")
    return {
        "imported": [
            {"name": p.name, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [
            {"name": name, "reason": reason, "reason_code": code}
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _SKILL_SCAN_DIRS,
        "dry_run": dry_run,
    }


@router.post("/context/skills/{name}/import")
async def import_skill(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to import into. Non-shared tiers rejected (ADR-0011).",
    ),
) -> dict:
    """Import a single runtime skill into ``.memtomem/skills/``.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime directory matches the name (the
    section import would silently report 0 imported, which is the wrong
    shape of feedback for "you clicked a specific item that doesn't exist").
    """
    _reject_non_shared_write(target_scope, "Import skill")
    try:
        validate_name(name, kind="skill name")
    except InvalidNameError as exc:
        raise HTTPException(400, f"Invalid skill name: {exc}")
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_skills_to_canonical(
                    project_root, overwrite=overwrite, only_name=name
                )
    except TimeoutError:
        raise HTTPException(503, "Skill import timed out — another sync may be in progress")
    if not result.imported and not result.skipped:
        raise HTTPException(404, f"No runtime skill named {name!r} to import")
    return {
        "imported": [
            {"name": p.name, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [
            {"name": n, "reason": reason, "reason_code": code} for n, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _SKILL_SCAN_DIRS,
    }
