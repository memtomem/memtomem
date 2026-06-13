"""Context gateway — Skills CRUD, diff, sync, and import."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context._runtime_targets import KNOWN_RUNTIMES, runtime_fanout_root
from memtomem.context.detector import SKILL_DIRS
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.skills import (
    CANONICAL_SKILL_ROOT,
    SKILL_GENERATORS,
    SKILL_MANIFEST,
    ExtractResult,
    canonical_skills_root,
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
    list_canonical_skills,
)
from memtomem.config import TargetScope
from memtomem.web.routes._confirm import host_write_gate
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._sync_phase import SyncPhaseError
from memtomem.web.routes.context_gateway import delete_skip_entry, sanitize_diff_reason
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


def _reject_project_local_write(target_scope: TargetScope, action: str) -> None:
    """Reject writes on the ``project_local`` tier with HTTP 400.

    Reads honor every tier (the canonical content differs per scope).
    Writes accept ``project_shared`` (ungated, today's default) and —
    since #1263 — ``user``, whose host-path writes complete only through
    the ``allow_host_writes`` disclose-then-confirm round-trip
    (:func:`memtomem.web.routes._confirm.host_write_gate`).
    ``project_local`` stays rejected: it is a gitignored draft tier with
    no runtime fan-out (ADR-0011 §3), so sync would be a no-op and its
    authoring policy is still deliberately deferred. Rejecting the
    explicit param is preferred over silently overriding the client's
    selection — silent override is exactly the cross-tier crossover
    (ADR-0011) P1 flagged ("mutates a same-named project_shared artifact
    while the UI is showing a user/local row").
    """
    if target_scope == "project_local":
        raise _error(
            400,
            "validation",
            (
                f"{action} is supported on the project_shared and user tiers; "
                f"project_local is a draft tier with no runtime fan-out "
                f"(ADR-0011 §3)."
            ),
            reason_code="project_local_unsupported",
        )


def _user_scan_dirs() -> list[str]:
    """User-tier runtime roots the import scan reads (absolute, expanded).

    The project-relative ``_SKILL_SCAN_DIRS`` hint would lie on
    ``target_scope=user`` — the engine's user-tier import reads
    ``~/.claude/skills`` etc. via ``runtime_fanout_root``, not the
    project's runtime dirs.
    """
    dirs: list[str] = []
    for runtime in KNOWN_RUNTIMES:
        root = runtime_fanout_root("skills", runtime, "user", None)
        if root is not None:
            dirs.append(str(root))
    return sorted(set(dirs))


def _scanned_dirs_for(target_scope: TargetScope) -> list[str]:
    return _user_scan_dirs() if target_scope == "user" else _SKILL_SCAN_DIRS


def _user_sync_host_targets(project_root: Path) -> list[str]:
    """Pending user-tier fan-out destinations for the sync confirm gate.

    Upper bound on the confirmed sync's writes. Skill fan-out is keyed by
    the canonical DIRECTORY name (skills are opaque to the canonical
    layer — no parsed-frontmatter rename axis, unlike agents/commands),
    so the names here are exact; the engine's per-destination preflight
    (target conflicts, unreadable canonicals/overrides, privacy blocks,
    lock timeouts) can only shrink the actual write set below this
    disclosure, never move it elsewhere. Empty when there are no
    user-tier canonicals — the gate stays open and the engine returns
    its normal ``no canonical skills`` skip.
    """
    targets: list[str] = []
    for skill_dir in list_canonical_skills(project_root, scope="user"):
        for gen in SKILL_GENERATORS.values():
            dst = gen.target_dir(project_root, skill_dir.name, scope="user")
            if dst is not None:
                targets.append(str(dst))
    return sorted(targets)


def _safe_rel(p: Path, project_root: Path) -> str:
    """Project-relative path as a POSIX string for API payloads.

    ``.as_posix()`` (not ``str``) so canonical/runtime paths come back
    ``/``-separated on every platform — the Web UI and diff payloads pin POSIX
    separators (#1256). Falls back to the absolute POSIX path for user-tier
    locations outside ``project_root``. Parity with ``context_agents`` /
    ``context_commands`` (#1264); this route was just never covered by #1256's
    diff tests, so the ``str()`` form lingered latent (#1325).
    """
    try:
        return p.relative_to(project_root).as_posix()
    except ValueError:
        return p.as_posix()


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
    for row in diffs:
        entry: dict[str, object] = {"runtime": row[0], "status": row[2]}
        reason = sanitize_diff_reason(getattr(row, "reason", None), project_root)
        if reason:
            entry["reason"] = reason
        by_name.setdefault(row[1], []).append(entry)

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

    Display-only normalization: one leading UTF-8 BOM is stripped so a
    Windows-authored SKILL.md doesn't fall past the anchored regex and show
    the literal frontmatter fence as its description (#1229). The skill
    *content* served to the editor stays byte-faithful — skills fan out and
    diff byte-exact by design.
    """
    content = content.removeprefix("﻿")
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
        raise _error(404, "missing", f"skill {name!r} not found")

    content = manifest.read_text(encoding="utf-8")
    # mtime_ns serialized as string (JS bigint-unsafe).
    mtime_ns = manifest.stat().st_mtime_ns

    # List auxiliary files
    files = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and p.name != SKILL_MANIFEST:
            files.append(
                {
                    # ``.as_posix()`` (not ``str``) — same POSIX-separator
                    # contract as ``_safe_rel``; ``str(PurePath)`` is
                    # backslash-joined on Windows (#1325).
                    "path": p.relative_to(skill_dir).as_posix(),
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
    # #1263 host-write opt-in: required true for target_scope=user (the
    # canonical lands under ~/.memtomem/, outside any project root). The
    # first POST without it returns the needs_confirmation envelope.
    allow_host_writes: bool = False


@router.post("/context/skills")
async def create_skill(
    body: SkillCreateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to create in. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    """Create a new canonical skill."""
    _reject_project_local_write(target_scope, "Create skill")
    skill_dir = _canonical_skill_dir(project_root, body.name, scope=target_scope)

    # Unlocked pre-checks so a request that cannot succeed is refused
    # (409) rather than confirmed, and a no-op never prompts. The locked
    # re-check below stays authoritative for create races.
    if skill_dir.exists():
        raise _error(
            409, "conflict", f"Skill '{body.name}' already exists", reason_code="already_exists"
        )
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action="Create skill",
        host_targets=[str(skill_dir / SKILL_MANIFEST)],
    )
    if gate is not None:
        return gate

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if skill_dir.exists():
                    # 409 Conflict, matching create_agent / create_command.
                    # A bare ValueError here maps to HTTP 400 via the app's
                    # value_error_handler, diverging from the sibling routes
                    # (and from the Web UI's 409 conflict-resolution flow).
                    raise _error(
                        409,
                        "conflict",
                        f"Skill '{body.name}' already exists",
                        reason_code="already_exists",
                    )
                skill_dir.mkdir(parents=True)
                manifest = skill_dir / SKILL_MANIFEST
                atomic_write_text(manifest, body.content)
    except TimeoutError:
        raise _error(503, "busy", "Skill create timed out — another sync may be in progress")
    return {"name": body.name, "canonical_path": _safe_rel(skill_dir, project_root)}


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
    # #1263 host-write opt-in for target_scope=user (see SkillCreateRequest).
    allow_host_writes: bool = False


_MTIME_CONFLICT_REASON = "File was modified by another process. Reload and retry."


def _mtime_conflict_response(current_mtime_ns: int) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": _MTIME_CONFLICT_REASON,
            "mtime_ns": str(current_mtime_ns),
            "error_kind": "conflict",
            "reason_code": "stale_mtime",
        },
    )


@router.put("/context/skills/{name}")
async def update_skill(
    name: str,
    body: SkillUpdateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to update. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> JSONResponse:
    """Update a canonical skill's SKILL.md (mtime-guarded, atomic, locked)."""
    _reject_project_local_write(target_scope, "Update skill")
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)
    manifest = skill_dir / SKILL_MANIFEST
    if not manifest.is_file():
        raise _error(404, "missing", f"skill {name!r} not found")

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise _error(422, "validation", f"Invalid mtime_ns: {body.mtime_ns!r}")

    # Unlocked pre-check: a stale-mtime request is refused (same 409 shape
    # as the locked check) BEFORE the host-write gate, so the user is never
    # asked to confirm a write that would only abort. The locked re-check
    # below stays authoritative; force=True falls through so the locked
    # path keeps emitting its audit WARNING exactly once.
    pre_mtime_ns = manifest.stat().st_mtime_ns
    if pre_mtime_ns != body_mtime_ns and not body.force:
        return _mtime_conflict_response(pre_mtime_ns)
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action="Update skill",
        host_targets=[str(manifest)],
    )
    if gate is not None:
        return JSONResponse(content=gate)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = manifest.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    if not body.force:
                        return _mtime_conflict_response(current_mtime_ns)
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
        raise _error(503, "busy", "Skill update timed out — another sync may be in progress")
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/context/skills/{name}")
async def delete_skill(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(resolve_scope_root_cascade_gated),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to delete from. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    allow_host_writes: bool = Query(
        False,
        description=(
            "#1263 host-write opt-in for target_scope=user. Query parameter "
            "(not a body field) because DELETE bodies are client-hostile; "
            "the needs_confirmation envelope names the same flag."
        ),
    ),
) -> dict:
    """Delete a canonical skill, optionally cascading to runtime copies.

    Idempotent: missing canonical directory returns ``deleted: []``.
    """
    _reject_project_local_write(target_scope, "Delete skill")
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)

    # Pending deletions, computed unlocked: the canonical dir plus — on
    # cascade — the runtime copies AT THIS TIER (scope= is load-bearing:
    # a user-tier cascade must resolve ~/.claude/... copies, never the
    # project's). Idempotent no-ops (nothing exists) skip the gate.
    pending: list[Path] = [skill_dir] if skill_dir.exists() else []
    if cascade:
        for gen in SKILL_GENERATORS.values():
            target = gen.target_dir(project_root, name, scope=target_scope)
            if target is not None and target.exists():
                pending.append(target)
    gate = host_write_gate(
        target_scope,
        allow_host_writes,
        action="Delete skill",
        host_targets=[str(p) for p in pending],
    )
    if gate is not None:
        return gate

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
                        skipped.append(delete_skip_entry(skill_dir, e, project_root))

                if cascade:
                    for gen in SKILL_GENERATORS.values():
                        target = gen.target_dir(project_root, name, scope=target_scope)
                        if target is None:
                            continue
                        if not target.exists():
                            continue
                        try:
                            shutil.rmtree(target)
                            removed.append(_safe_rel(target, project_root))
                        except OSError as e:
                            skipped.append(delete_skip_entry(target, e, project_root))
    except TimeoutError:
        raise _error(503, "busy", "Skill delete timed out — another sync may be in progress")

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
        # Resolve the runtime side at the same tier as the canonical side —
        # the engine diff (diff_skills) already does; without scope= this
        # always probed project_shared paths and the detail panel
        # contradicted the list view on user/project_local tiers (#1229).
        # NO_FANOUT tiers (e.g. project_local) return None and are skipped.
        target = gen.target_dir(project_root, name, scope=target_scope)
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


class SyncRequest(BaseModel):
    """Optional body for the sync routes (#1263).

    Absent body keeps the historical no-body POST working; the only field
    is the user-tier host-write opt-in.
    """

    allow_host_writes: bool = False


async def _sync_skills_core(
    project_root: Path,
    target_scope: TargetScope,
    *,
    surface: str = "web_context_skills_sync",
) -> dict:
    """Lock-free skills sync core — the caller MUST hold ``_gateway_lock``.

    Shared by the standalone route below and ``POST /context/sync-all``
    (#1278), which runs every per-type core under ONE outer lock
    acquisition — the lock is a non-reentrant ``_LoopLocalLock``, so the
    core must never acquire it itself.

    Offloads to a worker thread — the engine acquires cross-process
    destination sidecar locks (skills hold them across the whole staging
    swap, _locks.py), which would otherwise block the event loop thread,
    stalling every request AND preventing the caller's ``asyncio.timeout``
    from firing (its expiry callback runs on the very loop that is
    blocked) — the exact shape #1145 fixed for settings. The engine's own
    ``_SKILLS_LOCK_BUDGET_S`` (30s, below every caller's timeout) bounds
    the lock waits, so a timed-out request cannot orphan a worker thread
    that writes after the 503 already went out.

    Engine errors are raised as :class:`SyncPhaseError` — the standalone
    route's historical status/detail pair (privacy 422 keeps its STRING
    detail, issue-pinned) plus the envelope attributes sync-all renders.
    """
    try:
        result = await asyncio.to_thread(
            generate_all_skills,
            project_root,
            scope=target_scope,
            surface=surface,
        )
    except PrivacyScanError as exc:
        raise SyncPhaseError(
            422, exc.message, error_kind="validation", reason_code="privacy_blocked"
        ) from exc
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


@router.post("/context/skills/sync")
async def sync_skills(
    body: SyncRequest | None = None,
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to fan out. user fans out to the host "
            "~/.claude-family roots behind the allow_host_writes confirm "
            "round-trip (#1263); project_local rejected — no runtime fan-out "
            "per ADR-0011 §3."
        ),
    ),
) -> dict:
    """Fan out canonical skills to all runtimes."""
    _reject_project_local_write(target_scope, "Sync skills")
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes if body else False,
        action="Sync skills",
        host_targets=_user_sync_host_targets(project_root),
    )
    if gate is not None:
        return gate
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return await _sync_skills_core(project_root, target_scope)
    except TimeoutError:
        raise _error(503, "busy", "Skills sync timed out — another sync may be in progress")


# ── Import (reverse sync) ───────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False
    # #1263 host-write opt-in for target_scope=user (the canonical
    # destination is ~/.memtomem/skills/, outside any project root).
    allow_host_writes: bool = False


def _import_payload(
    result: ExtractResult,
    project_root: Path,
    target_scope: TargetScope,
    dry_run: bool | None,
) -> dict:
    """Wire shape shared by both import routes (and the gate's nested plan).

    ``dry_run=None`` omits the key — the single-import response never
    carried one. ``_safe_rel`` (not bare ``relative_to``) because user-tier
    canonical paths live under ``~/.memtomem/`` and would otherwise raise
    out of the response encoder.
    """
    payload: dict = {
        "imported": [
            {"name": p.name, "canonical_path": _safe_rel(p, project_root)} for p in result.imported
        ],
        "skipped": [
            {"name": name, "reason": reason, "reason_code": code}
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _scanned_dirs_for(target_scope),
    }
    if dry_run is not None:
        payload["dry_run"] = dry_run
    return payload


@router.post("/context/skills/import")
async def import_skills(
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to import into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the import without writing to canonical (rank-10): runs the "
            "full scan + privacy walk + dedup and returns the would-import / would-"
            "skip counts, leaving disk untouched. Returned regardless of "
            "confirmation flags (mirrors the transfer route's dry_run)."
        ),
    ),
) -> dict:
    """Import runtime skills into the scoped canonical directory."""
    _reject_project_local_write(target_scope, "Import skills")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False

    async def _run(dry: bool) -> ExtractResult:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Thread offload (#1247 id 18): the import engine now blocks
                # on the destination sidecar flock (budget-bounded), and
                # ``asyncio.timeout`` cannot fire while the loop thread
                # itself is blocked — same shape as the sync route above.
                return await asyncio.to_thread(
                    extract_skills_to_canonical,
                    project_root,
                    overwrite=overwrite,
                    dry_run=dry,
                    scope=target_scope,
                    surface="web_context_skills_import",
                )

    try:
        if not dry_run and target_scope == "user" and not allow_host_writes:
            # The gate needs the pending destination paths, which only the
            # engine's scan knows — preview via dry_run, disclose, and nest
            # the plan (A-5 transfer-route parity). An empty would-import
            # set keeps the gate open (the confirmed run writes nothing);
            # the dry-run→apply gap is the same accepted TOCTOU window the
            # transfer route documents.
            preview = await _run(dry=True)
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action="Import skills",
                host_targets=[str(p) for p in preview.imported],
                plan=_import_payload(preview, project_root, target_scope, dry_run=True),
            )
            if gate is not None:
                return gate
        result = await _run(dry=dry_run)
    except TimeoutError:
        raise _error(503, "busy", "Skills import timed out — another sync may be in progress")
    return _import_payload(result, project_root, target_scope, dry_run=dry_run)


@router.post("/context/skills/{name}/import")
async def import_skill(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to import into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    """Import a single runtime skill into the scoped canonical directory.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime directory matches the name (the
    section import would silently report 0 imported, which is the wrong
    shape of feedback for "you clicked a specific item that doesn't exist")
    — pinned on the gate's dry-run preview too, so a misnamed user-tier
    import 404s instead of asking for confirmation.
    """
    _reject_project_local_write(target_scope, "Import skill")
    try:
        validate_name(name, kind="skill name")
    except InvalidNameError as exc:
        raise _error(400, "validation", f"Invalid skill name: {exc}")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False

    async def _run(dry: bool) -> ExtractResult:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Thread offload (#1247 id 18): see import_skills above.
                return await asyncio.to_thread(
                    extract_skills_to_canonical,
                    project_root,
                    overwrite=overwrite,
                    only_name=name,
                    dry_run=dry,
                    scope=target_scope,
                    surface="web_context_skills_import",
                )

    try:
        if target_scope == "user" and not allow_host_writes:
            preview = await _run(dry=True)
            if not preview.imported and not preview.skipped:
                raise _error(404, "missing", f"No runtime skill named {name!r} to import")
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action="Import skill",
                host_targets=[str(p) for p in preview.imported],
                plan=_import_payload(preview, project_root, target_scope, dry_run=None),
            )
            if gate is not None:
                return gate
        result = await _run(dry=False)
    except TimeoutError:
        raise _error(503, "busy", "Skill import timed out — another sync may be in progress")
    if not result.imported and not result.skipped:
        raise _error(404, "missing", f"No runtime skill named {name!r} to import")
    return _import_payload(result, project_root, target_scope, dry_run=None)
