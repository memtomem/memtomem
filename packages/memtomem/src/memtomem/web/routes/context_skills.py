"""Context gateway — Skills CRUD, diff, sync, and import."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

import click
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from memtomem.context._atomic import atomic_write_text
from memtomem.context._canonical_txn import canonical_sidecar_lock, new_lock_budget
from memtomem.context._dir_swap import SwapRecoveryError
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.detector import SKILL_DIRS
from memtomem.context.privacy_scan import PrivacyScanError, scan_text_content
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
    run_swap_prelude,
)
from memtomem.config import TargetScope
from memtomem.web.routes._artifact_common import (
    ArtifactCreateRequest,
    ArtifactUpdateRequest,
    HostWriteSyncRequest,
    ImportRequest,
    mtime_conflict_response,
    raise_if_privacy_blocked,
    reject_project_local_write,
    scanned_dirs_for,
)
from memtomem.web.routes._confirm import host_write_gate
from memtomem.web.routes._errors import PRIVACY_BLOCK_DETAIL, PRIVACY_BLOCK_IMPORT_DETAIL, _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._sync_phase import SyncPhaseError
from memtomem.web.routes.context_gateway import (
    _safe_rel,
    delete_skip_entry,
    read_text_lenient,
    redact_wire_reason,
    sanitize_diff_reason,
)
from memtomem.web.routes.context_projects import (
    resolve_scope_root,
    resolve_scope_root_cascade_gated,
    resolve_writable_scope_root,
)
from memtomem.web.schemas.context import ContextImportNeedsConfirmation, ContextImportReport

# Flat list of project-relative runtime scan paths reported on list / import
# responses so the web UI's empty-state hint can name the exact directories
# the detector inspects without hardcoding them client-side.
_SKILL_SCAN_DIRS: list[str] = [d for paths in SKILL_DIRS.values() for d in paths]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-skills"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _swap_recovery_error(exc: SwapRecoveryError, project_root: Path) -> HTTPException:
    """409 envelope for an interrupted skills swap (ADR-0030 §10).

    A distinct ``reason_code`` rather than the neighbouring ``already_exists``
    /``missing``: nothing about this state is a naming conflict or an absence,
    and the two other codes come with remediations ("pick another name", "create
    it first") that would send the user in the wrong direction. What this needs
    is an operator looking at the two paths the engine names.

    The message is raw engine text, so it goes through the same two-stage
    ``redact_wire_reason`` the Pull surfaces use — root-relative where it can
    be, scrubbed where it cannot (a Store on another volume).

    Not in ``context.remediation``'s hint table by design (#1870): the fix is
    identical on every surface, so there is no per-surface vocabulary to add,
    and ``action_hint`` fails open to "" for exactly this case.
    """
    return _error(
        409,
        "conflict",
        redact_wire_reason(str(exc), project_root) or "an interrupted swap is pending",
        reason_code="swap_recovery_pending",
    )


def _canonical_skill_dir(
    project_root: Path, raw_name: str, *, scope: TargetScope = "project_shared"
) -> Path:
    """Validate the name via core and return the canonical skill directory.

    ``scope`` selects the canonical residency tier (ADR-0011 PR-E3 / ADR-0016).
    Default ``project_shared`` preserves pre-ADR-0011 behavior.
    """
    name = validate_name(raw_name, kind="skill")
    return canonical_skills_root(project_root, scope=scope) / name


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


class SkillCreateRequest(ArtifactCreateRequest):
    pass


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
    reject_project_local_write(target_scope, "Create skill")
    skill_dir = _canonical_skill_dir(project_root, body.name, scope=target_scope)
    if target_scope == "project_shared":
        # #1509 write-time Gate A — scan the exact in-memory string that
        # atomic_write_text will write (no TOCTOU). user-tier saves are not
        # scanned: not git-tracked, gated by allow_host_writes, and keep the
        # sync-time force valve (ADR-0011 §5 asymmetry). source_path is the
        # skill dir (never opened here) so path.name is the artifact name,
        # not the useless constant SKILL.md.
        scan = scan_text_content(
            body.content,
            source_path=skill_dir,
            surface="web_context_skills_create",
            scope="project_shared",
            project_root=project_root,
        )
        raise_if_privacy_blocked(scan, kind="skill", artifact_name=body.name)

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

    # Validate the submitted content encodes as UTF-8 before anything touches
    # disk. A lone-surrogate body can't be UTF-8 encoded, so
    # ``atomic_write_text`` (which does ``text.encode``) would raise
    # UnicodeEncodeError AFTER ``mkdir``, leaving an orphan directory that
    # wedges every retry on the 409 below.
    try:
        body.content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _error(400, "validation", "Skill content is not valid UTF-8") from exc

    def _create_locked() -> None:
        # ADR-0030 §6: cross-process canonical lock (the same
        # ``<root>/.{name}.lock`` the skills importer takes) so a concurrent
        # Pull / transfer / migrate can't race the create. Worker-thread only
        # (blocking flock off the loop); ``_gateway_lock`` (held) serializes
        # in-process callers.
        with canonical_sidecar_lock(skill_dir.parent, body.name, timeout=new_lock_budget()()):
            # ADR-0030 §10: recovery before the existence re-check — a row that
            # recovery is about to roll back would otherwise 409 as "already
            # exists" while its tree is on its way out.
            run_swap_prelude(skill_dir.parent, body.name, kind="skills")
            if skill_dir.exists():
                # 409 Conflict, matching create_agent / create_command.
                raise _error(
                    409,
                    "conflict",
                    f"Skill '{body.name}' already exists",
                    reason_code="already_exists",
                )
            skill_dir.mkdir(parents=True)
            manifest = skill_dir / SKILL_MANIFEST
            try:
                atomic_write_text(manifest, body.content)
            except BaseException:
                # Roll back the partial skill dir so a transient failure doesn't
                # leave an empty directory that wedges every future create of
                # this name on the orphan-dir 409 above.
                shutil.rmtree(skill_dir, ignore_errors=True)
                raise

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                await asyncio.to_thread(_create_locked)
    except TimeoutError:
        raise _error(503, "busy", "Skill create timed out — another sync may be in progress")
    except SwapRecoveryError as exc:
        raise _swap_recovery_error(exc, project_root) from exc
    return {"name": body.name, "canonical_path": _safe_rel(skill_dir, project_root)}


# ── Update ───────────────────────────────────────────────────────────────


class SkillUpdateRequest(ArtifactUpdateRequest):
    pass


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
    reject_project_local_write(target_scope, "Update skill")
    skill_dir = _canonical_skill_dir(project_root, name, scope=target_scope)
    manifest = skill_dir / SKILL_MANIFEST
    if target_scope == "project_shared":
        # #1509 write-time Gate A — see create_skill. Runs before the 404
        # check so privacy refusal wins over existence, matching the
        # commands/agents editors; force=True only bypasses the mtime
        # guard, never this scan.
        scan = scan_text_content(
            body.content,
            source_path=skill_dir,
            surface="web_context_skills_update",
            scope="project_shared",
            project_root=project_root,
        )
        raise_if_privacy_blocked(scan, kind="skill", artifact_name=name)
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
        return mtime_conflict_response(pre_mtime_ns)
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action="Update skill",
        host_targets=[str(manifest)],
    )
    if gate is not None:
        return JSONResponse(content=gate)

    def _update_locked() -> tuple[str, int]:
        # ADR-0030 §6: existence + mtime re-check AND write under the
        # cross-process canonical lock (lock-only in B2a — no auto-snapshot on
        # edit save). The manifest existence is re-checked INSIDE C0 — a
        # concurrent delete could have removed the skill since the pre-lock 404
        # check, and a bare ``manifest.stat()`` would then 500. Returns
        # ``(status, mtime_ns)`` with status "gone" / "conflict" / "ok".
        with canonical_sidecar_lock(skill_dir.parent, name, timeout=new_lock_budget()()):
            # ADR-0030 §10: recovery before the manifest/mtime re-check — a
            # mid-swap canonical would otherwise report "gone" (404) or a bogus
            # mtime conflict against bytes that are about to be replaced.
            run_swap_prelude(skill_dir.parent, name, kind="skills")
            if not manifest.is_file():
                return "gone", 0
            current_mtime_ns = manifest.stat().st_mtime_ns
            if current_mtime_ns != body_mtime_ns:
                if not body.force:
                    return "conflict", current_mtime_ns
                logger.warning(
                    "force-save bypassed mtime check on %s (client_mtime_ns=%s server_mtime_ns=%s)",
                    manifest,
                    body_mtime_ns,
                    current_mtime_ns,
                )
            atomic_write_text(manifest, body.content)
            return "ok", manifest.stat().st_mtime_ns

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                status, mtime_ns = await asyncio.to_thread(_update_locked)
    except TimeoutError:
        raise _error(503, "busy", "Skill update timed out — another sync may be in progress")
    except SwapRecoveryError as exc:
        raise _swap_recovery_error(exc, project_root) from exc
    if status == "gone":
        raise _error(404, "missing", f"skill {name!r} not found")
    if status == "conflict":
        return mtime_conflict_response(mtime_ns)
    return JSONResponse(content={"name": name, "mtime_ns": str(mtime_ns)})


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
    reject_project_local_write(target_scope, "Delete skill")
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

    def _delete_locked() -> tuple[dict | None, list[str], list[dict[str, str]]]:
        removed: list[str] = []
        skipped: list[dict[str, str]] = []

        # ADR-0030 §6: acquire the canonical name lock UNCONDITIONALLY, then
        # re-evaluate BOTH existence and the host-write confirmation INSIDE it.
        # The pre-lock gate ran on an unlocked snapshot; if the skill (or a
        # cascade target) was absent then but a concurrent create/transfer
        # materialized it while we waited for the lock, deleting it now would
        # bypass the user-tier host-write confirmation. So recompute the host
        # targets from the CURRENT state and re-gate — a needs-confirmation
        # envelope aborts the delete (returned to the caller) rather than
        # silently removing an unconfirmed user-tier artifact.
        cascade_targets: list[Path] = []
        with canonical_sidecar_lock(skill_dir.parent, name, timeout=new_lock_budget()()):
            # ADR-0030 §10: recovery before the locked host-write re-gate, so
            # the snapshot the user confirms — and the set this deletes — is the
            # recovered tree, never a half-swapped one. Deliberately OUTSIDE the
            # ``except OSError`` below: a wedged artifact is a refusal for the
            # whole delete, not a ``skipped`` row that reads like a permissions
            # problem.
            run_swap_prelude(skill_dir.parent, name, kind="skills")
            locked_pending: list[Path] = [skill_dir] if skill_dir.exists() else []
            if cascade:
                for gen in SKILL_GENERATORS.values():
                    target = gen.target_dir(project_root, name, scope=target_scope)
                    if target is not None and target.exists():
                        cascade_targets.append(target)
            locked_pending.extend(cascade_targets)
            locked_gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action="Delete skill",
                host_targets=[str(p) for p in locked_pending],
            )
            if locked_gate is not None:
                return locked_gate, removed, skipped

            if skill_dir.exists():
                try:
                    shutil.rmtree(skill_dir)
                    removed.append(_safe_rel(skill_dir, project_root))
                except OSError as e:
                    skipped.append(delete_skip_entry(skill_dir, e, project_root))

        # Runtime cascade removals (fan-out targets, not canonical) stay outside
        # the lock, but delete ONLY the snapshot captured + confirmed under the
        # gate above — never re-scan. Fan-out writers don't share the canonical
        # lock, so a runtime target that appeared AFTER the gate must not be
        # deleted here without its own confirmation (ADR-0030 §6).
        for target in cascade_targets:
            if not target.exists():
                continue  # removed concurrently between the gate and here
            try:
                shutil.rmtree(target)
                removed.append(_safe_rel(target, project_root))
            except OSError as e:
                skipped.append(delete_skip_entry(target, e, project_root))
        return None, removed, skipped

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                gate_envelope, removed, skipped = await asyncio.to_thread(_delete_locked)
    except TimeoutError:
        raise _error(503, "busy", "Skill delete timed out — another sync may be in progress")
    except SwapRecoveryError as exc:
        raise _swap_recovery_error(exc, project_root) from exc

    if gate_envelope is not None:
        return gate_envelope
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
        # Lenient read for the display pane — a stray non-UTF-8 byte in the
        # canonical SKILL.md must not abort the whole diff with an uncaught
        # UnicodeDecodeError (the app maps it to a 400, not a diff). The engine
        # ``diff_skills`` reads bytes with errors="replace" and stays the
        # byte-wise authority for the list badge; this pane just mirrors it so
        # the detail panel diagnoses instead of erroring (parity with
        # diff_command / diff_agent — this route never got #1229/#1233's
        # lenient read). ``None`` here means missing OR unreadable canonical.
        canonical_content = read_text_lenient(canonical_manifest)

    # ``object`` values (not ``str``) — ``runtime_content`` may be ``None`` when
    # the lenient read hits an OSError; mirrors context_commands / context_agents.
    runtimes: list[dict[str, object]] = []
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
                    # Lenient read — see the canonical read above; a non-UTF-8
                    # runtime copy is drift to display, not a diff-wide abort.
                    "runtime_content": read_text_lenient(target_manifest),
                }
            )
        else:
            runtime_content = read_text_lenient(target_manifest)
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


# No ``on_drop`` (plain HostWriteSyncRequest, not AtomicSyncRequest): skills
# fan out byte-exact, so there are no droppable fields.
class SyncRequest(HostWriteSyncRequest):
    """Optional body for the sync routes (#1263).

    Absent body keeps the historical no-body POST working.
    """


async def _sync_skills_core(
    project_root: Path,
    target_scope: TargetScope,
    *,
    surface: str = "web_context_skills_sync",
    force_unsafe: bool = False,
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
            force_unsafe=force_unsafe,
        )
    except PrivacyScanError as exc:
        # Path-free detail — ``exc.message`` embeds the absolute canonical path
        # (#1385 finding 1). The chained ``exc`` keeps the full text for logs.
        raise SyncPhaseError(
            422, PRIVACY_BLOCK_DETAIL, error_kind="validation", reason_code="privacy_blocked"
        ) from exc
    return {
        "generated": [
            {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in result.generated
        ],
        # Engine skip reasons embed absolute paths — the destination for a
        # target conflict, the transients for a pending swap recovery, an
        # OSError's filename for an unreadable canonical. Sanitized here, at
        # the wire (#1385/#1412): the canonical-path disclosure rule is that
        # CLI gets the path verbatim and the web/MCP surfaces do not.
        #
        # ``redact_wire_reason``, not bare ``sanitize_diff_reason``: these
        # are raw engine exception strings, and root-relative stripping alone
        # leaves an absolute path whenever the target resolves OUTSIDE both the
        # project root and ``$HOME`` — a runtime dir symlinked onto a shared
        # volume, which ``_runtime_targets`` resolves before the OSError is
        # raised. Same two-stage form the Pull reasons use.
        "skipped": [
            {
                "runtime": rt,
                "reason": redact_wire_reason(reason, project_root),
                "reason_code": code,
            }
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
    reject_project_local_write(target_scope, "Push skills")
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes if body else False,
        action="Push skills",
        host_targets=_user_sync_host_targets(project_root),
    )
    if gate is not None:
        return gate
    force_unsafe = body.force_unsafe_sync if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return await _sync_skills_core(
                    project_root, target_scope, force_unsafe=force_unsafe
                )
    except TimeoutError:
        raise _error(503, "busy", "Skills push timed out — another sync may be in progress")


# ── Import (reverse sync) ───────────────────────────────────────────────


def _import_payload(
    result: ExtractResult,
    project_root: Path,
    target_scope: TargetScope,
    dry_run: bool | None,
    source_scope: TargetScope | None = None,
) -> dict:
    """Wire shape shared by both import routes (and the gate's nested plan).

    ``dry_run=None`` omits the key — the single-import response never
    carried one. ``_safe_rel`` (not bare ``relative_to``) because user-tier
    canonical paths live under ``~/.memtomem/`` and would otherwise raise
    out of the response encoder.

    ``scanned_dirs`` describes where the import READ from, which is the
    destination ``target_scope`` for the coupled routes but the decoupled
    SOURCE for ``import_skill_to_user`` (project runtime → user library);
    pass ``source_scope`` there so the field doesn't misreport the user
    runtime roots for a project-runtime read.
    """
    payload: dict = {
        "imported": [
            {
                "name": p.name,
                "canonical_path": _safe_rel(p, project_root),
                "source_runtime": result.source_runtimes.get(p.name),
                "duplicate_candidates": result.runtime_candidates.get(p.name, []),
            }
            for p in result.imported
        ],
        # Same wire-boundary redaction as the sync report above — an import
        # skip carries the same path-bearing engine reasons.
        "skipped": [
            {
                "name": name,
                "reason": redact_wire_reason(reason, project_root),
                "reason_code": code,
            }
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": scanned_dirs_for(
            source_scope if source_scope is not None else target_scope,
            kind="skills",
            project_scan_dirs=_SKILL_SCAN_DIRS,
        ),
    }
    if dry_run is not None:
        payload["dry_run"] = dry_run
    return payload


@router.post(
    "/context/skills/import",
    response_model=ContextImportReport | ContextImportNeedsConfirmation,
    # exclude_unset: dry_run is omitted (not null) when the builder gets None.
    response_model_exclude_unset=True,
)
async def import_skills(
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to pull into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the pull without writing to canonical (rank-10): runs the "
            "full scan + privacy walk + dedup and returns the would-pull / would-"
            "skip counts, leaving disk untouched. Returned regardless of "
            "confirmation flags (mirrors the transfer route's dry_run)."
        ),
    ),
) -> dict:
    """Pull runtime skills into the scoped canonical directory."""
    reject_project_local_write(target_scope, "Pull skills")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

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
                    force_unsafe_import=force_unsafe_import,
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
                action="Pull skills",
                host_targets=[str(p) for p in preview.imported],
                plan=_import_payload(preview, project_root, target_scope, dry_run=True),
            )
            if gate is not None:
                return gate
        result = await _run(dry=dry_run)
    except TimeoutError:
        raise _error(503, "busy", "Skills pull timed out — another sync may be in progress")
    except click.ClickException as exc:
        # The import engine's only ClickException is the project_shared Gate A
        # privacy hard-abort (ADR-0011 §5 — no force bypass; _gate_a.py). Render
        # it as the same string-detail 422 the sync route gives PrivacyScanError
        # and the MCP import tool gives this exact exception, rather than letting
        # it fall through to the generic 500 handler (the PrivacyScanError
        # docstring's stated intent: non-CLI surfaces translate, never 500).
        raise HTTPException(422, PRIVACY_BLOCK_IMPORT_DETAIL) from exc
    return _import_payload(result, project_root, target_scope, dry_run=dry_run)


@router.post(
    "/context/skills/{name}/import",
    response_model=ContextImportReport | ContextImportNeedsConfirmation,
    response_model_exclude_unset=True,
)
async def import_skill(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to pull into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    """Pull a single runtime skill into the scoped canonical directory.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime directory matches the name (the
    section import would silently report 0 imported, which is the wrong
    shape of feedback for "you clicked a specific item that doesn't exist")
    — pinned on the gate's dry-run preview too, so a misnamed user-tier
    import 404s instead of asking for confirmation.
    """
    reject_project_local_write(target_scope, "Pull skill")
    try:
        validate_name(name, kind="skill name")
    except InvalidNameError as exc:
        raise _error(400, "validation", f"Invalid skill name: {exc}")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

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
                    force_unsafe_import=force_unsafe_import,
                    surface="web_context_skills_import",
                )

    try:
        if target_scope == "user" and not allow_host_writes:
            preview = await _run(dry=True)
            if not preview.imported and not preview.skipped:
                raise _error(404, "missing", f"No runtime skill named {name!r} to pull")
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action="Pull skill",
                host_targets=[str(p) for p in preview.imported],
                plan=_import_payload(preview, project_root, target_scope, dry_run=None),
            )
            if gate is not None:
                return gate
        result = await _run(dry=False)
    except TimeoutError:
        raise _error(503, "busy", "Skill pull timed out — another sync may be in progress")
    except click.ClickException as exc:
        # project_shared Gate A privacy block → 422 (see import_skills).
        raise HTTPException(422, PRIVACY_BLOCK_IMPORT_DETAIL) from exc
    if not result.imported and not result.skipped:
        raise _error(404, "missing", f"No runtime skill named {name!r} to pull")
    return _import_payload(result, project_root, target_scope, dry_run=None)


@router.post(
    "/context/skills/{name}/import-to-user",
    response_model=ContextImportReport | ContextImportNeedsConfirmation,
    response_model_exclude_unset=True,
)
async def import_skill_to_user(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
) -> dict:
    """Pull a PROJECT-runtime skill into the USER library (~/.memtomem/skills).

    The one web path for a project-runtime skill that trips Gate A's
    false-positive secret heuristic. ``/import`` to ``project_shared`` is a
    hard 422 (git history is forever — no bypass, ADR-0011 §5),
    ``project_local`` web import is rejected (ADR-0011 §3), and a plain
    ``user``-tier import reads ``~/.claude/skills`` — the wrong source for a
    skill that lives under ``<project>/.claude/skills``. This route reads the
    PROJECT runtime (``source_scope="project_shared"``) but writes the USER
    canonical (``scope="user"``), so Gate A's block decision keys off the
    ``user`` destination and is force-bypassable after a reviewed confirm —
    exactly the upload/memory/CLI ``--force-unsafe-import`` valve. The user
    library is shared across every project, so this is the natural home for a
    reusable skill the project tier can't (and shouldn't) accept.

    Writes land outside any project root (``~/.memtomem/``), so the user-tier
    host-write disclosure (``allow_host_writes`` round-trip) applies — same as
    the user-tier ``/import``. 404 when no PROJECT runtime skill matches.

    Skills-only by design (#1520 item 5): the agents/commands extract
    engines have no ``source_scope`` variant, so there is no
    ``import-to-user`` route for those kinds until one exists.
    """
    try:
        validate_name(name, kind="skill name")
    except InvalidNameError as exc:
        raise _error(400, "validation", f"Invalid skill name: {exc}")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

    async def _run(dry: bool) -> ExtractResult:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                # Thread offload (#1247 id 18): see import_skills above. Reads
                # the project runtime, writes the user canonical — the only
                # call site that decouples source_scope from the dest scope.
                return await asyncio.to_thread(
                    extract_skills_to_canonical,
                    project_root,
                    overwrite=overwrite,
                    only_name=name,
                    dry_run=dry,
                    scope="user",
                    source_scope="project_shared",
                    force_unsafe_import=force_unsafe_import,
                    surface="web_context_skills_import_to_user",
                )

    try:
        if not allow_host_writes:
            # User dest → host write. Disclose the ~/.memtomem destinations the
            # confirmed run would touch; the dry preview threads force too, so a
            # reviewed force surfaces the would-import target (see #1379).
            preview = await _run(dry=True)
            if not preview.imported and not preview.skipped:
                raise _error(404, "missing", f"No project runtime skill named {name!r} to pull")
            gate = host_write_gate(
                "user",
                allow_host_writes,
                action="Pull skill to user library",
                host_targets=[str(p) for p in preview.imported],
                plan=_import_payload(
                    preview, project_root, "user", dry_run=None, source_scope="project_shared"
                ),
            )
            if gate is not None:
                return gate
        result = await _run(dry=False)
    except TimeoutError:
        raise _error(503, "busy", "Skill pull timed out — another sync may be in progress")
    except click.ClickException as exc:
        # Defensive: the user dest is force-bypassable, so Gate A raises a
        # ClickException only on a project_shared dest — which this route never
        # uses. Translate to 422 anyway rather than risk a generic 500 (#1378).
        raise HTTPException(422, PRIVACY_BLOCK_IMPORT_DETAIL) from exc
    if not result.imported and not result.skipped:
        raise _error(404, "missing", f"No project runtime skill named {name!r} to pull")
    return _import_payload(
        result, project_root, "user", dry_run=None, source_scope="project_shared"
    )
