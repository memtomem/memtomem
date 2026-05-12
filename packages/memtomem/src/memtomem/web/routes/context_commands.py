"""Context gateway — Commands CRUD, diff, sync, import, and rendered output."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.config import TargetTier
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.commands import (
    CANONICAL_COMMAND_ROOT,
    COMMAND_GENERATORS,
    CommandParseError,
    canonical_command_name,
    diff_commands,
    extract_commands_to_canonical,
    generate_all_commands,
    list_canonical_commands,
    parse_canonical_command,
    resolve_canonical_command,
)
from memtomem.context.detector import COMMAND_DIRS
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.web.deps import get_project_root, get_query_target_tier
from memtomem.web.routes.context_projects import resolve_scope_root
from memtomem.web.routes._locks import _gateway_lock

# Flat list of project-relative runtime scan paths reported on list / import
# responses so the web UI's empty-state hint can name the exact directories
# the detector inspects without hardcoding them client-side.
_COMMAND_SCAN_DIRS: list[str] = [rel for rel, _suffix in COMMAND_DIRS.values()]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-commands"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _commands_root(project_root: Path, *, scope: TargetTier = "project_shared") -> Path:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    return canonical_artifact_dir("commands", scope, project_root)


def _canonical_command_path(
    project_root: Path, raw_name: str, *, scope: TargetTier = "project_shared"
) -> Path:
    """Validate the name via core and return the canonical command path.

    ``scope`` selects the canonical residency tier (ADR-0016).
    """
    name = validate_name(raw_name, kind="command")
    return _commands_root(project_root, scope=scope) / f"{name}.md"


def _resolve_existing_command(
    project_root: Path, raw_name: str, *, scope: TargetTier = "project_shared"
):
    name = validate_name(raw_name, kind="command")
    return name, resolve_canonical_command(project_root, name, scope=scope)


def _reject_non_shared_write(target_tier: TargetTier, action: str) -> None:
    """Reject writes on non-``project_shared`` tiers with HTTP 400.

    See context_skills.py:_reject_non_shared_write for the rationale.
    Mirrored here so each route file stays self-contained.
    """
    if target_tier != "project_shared":
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action} is supported only on project_shared in this release; "
                f"got target_tier={target_tier!r}."
            ),
        )


def _safe_rel(p: Path, project_root: Path) -> str:
    try:
        return str(p.relative_to(project_root))
    except ValueError:
        return str(p)


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/commands")
async def list_commands(
    project_root: Path = Depends(resolve_scope_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    """List canonical commands. Accepts ``?scope_id=`` like list_skills."""
    canonicals = list_canonical_commands(project_root, scope=target_tier)
    diffs = diff_commands(project_root, scope=target_tier)

    by_name: dict[str, list[dict]] = {}
    for runtime, cmd_name, status in diffs:
        by_name.setdefault(cmd_name, []).append({"runtime": runtime, "status": status})

    commands: list[dict[str, object]] = []
    canonical_names: set[str] = set()
    for cmd_path, layout in canonicals:
        name = canonical_command_name(cmd_path, layout)
        canonical_names.add(name)
        commands.append(
            {
                "name": name,
                "canonical_path": _safe_rel(cmd_path, project_root),
                "target_tier": target_tier,
                "target_scope": target_tier,
                "runtimes": by_name.get(name, []),
            }
        )

    for cmd_name, runtimes in by_name.items():
        if cmd_name not in canonical_names:
            commands.append(
                {
                    "name": cmd_name,
                    "canonical_path": None,
                    "target_tier": target_tier,
                    "target_scope": target_tier,
                    "runtimes": runtimes,
                }
            )

    return {
        "commands": commands,
        "canonical_root": CANONICAL_COMMAND_ROOT,
        "scanned_dirs": _COMMAND_SCAN_DIRS,
    }


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/commands/{name}")
async def read_command(
    name: str,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    name, resolved = _resolve_existing_command(project_root, name, scope=target_tier)
    if resolved is None:
        raise KeyError(name)
    cmd_path, layout = resolved

    content = cmd_path.read_text(encoding="utf-8")
    # mtime_ns serialized as string (JS bigint-unsafe).
    mtime_ns = cmd_path.stat().st_mtime_ns

    fields: dict = {}
    try:
        parsed = parse_canonical_command(cmd_path, layout=layout)
        fields = {
            "description": parsed.description,
            "argument_hint": parsed.argument_hint,
            "allowed_tools": parsed.allowed_tools,
            "model": parsed.model,
        }
    except CommandParseError:
        pass

    return {"name": name, "content": content, "mtime_ns": str(mtime_ns), "fields": fields}


# ── Rendered (per-runtime output with dropped fields + field map) ────────

# Frontmatter keys that any command generator may drop. Hyphenated to match
# the strings the renderers emit on ``dropped_fields`` (see
# ``memtomem.context.commands._subcommand_to_gemini_toml``). Required fields
# (``name``, ``description``) are never dropped, so they aren't tracked here.
_ALL_OPTIONAL_FIELDS = ("argument-hint", "allowed-tools", "model")


@router.get("/context/commands/{name}/rendered")
async def rendered_command(
    name: str,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> JSONResponse:
    name, resolved = _resolve_existing_command(project_root, name, scope=target_tier)
    if resolved is None:
        raise KeyError(name)
    cmd_path, layout = resolved

    content = cmd_path.read_text(encoding="utf-8")
    try:
        parsed = parse_canonical_command(cmd_path, layout=layout)
    except CommandParseError as exc:
        return JSONResponse(status_code=422, content={"detail": f"Parse error: {exc}"})

    runtimes = []
    diffs = diff_commands(project_root, scope=target_tier)
    status_map: dict[tuple[str, str], str] = {(rt, n): s for rt, n, s in diffs}
    # ``field_map[field][runtime] = bool`` — True when the runtime keeps the
    # field, False when it drops it. Mirrors ``context_agents.rendered_agent``
    # so the front-end can render a single matrix for either surface.
    field_map: dict[str, dict[str, bool]] = {f: {} for f in _ALL_OPTIONAL_FIELDS}

    for gen_name, gen in COMMAND_GENERATORS.items():
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
        dropped_set = set(dropped_fields)
        for f in _ALL_OPTIONAL_FIELDS:
            field_map[f][gen_name] = f not in dropped_set

    return JSONResponse(
        content={
            "name": name,
            "canonical_content": content,
            "runtimes": runtimes,
            "field_map": field_map,
        }
    )


# ── Create ───────────────────────────────────────────────────────────────


class CommandCreateRequest(BaseModel):
    name: str
    content: str


@router.post("/context/commands")
async def create_command(
    body: CommandCreateRequest,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    _reject_non_shared_write(target_tier, "Create command")
    name = validate_name(body.name, kind="command")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if resolve_canonical_command(project_root, name, scope=target_tier) is not None:
                    raise HTTPException(409, detail=f"Command '{name}' already exists")
                cmd_path = _canonical_command_path(project_root, name, scope=target_tier)
                atomic_write_text(cmd_path, body.content)
    except TimeoutError:
        raise HTTPException(503, "Command create timed out — another sync may be in progress")
    return {"name": name, "canonical_path": str(cmd_path.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class CommandUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str
    # Bypass the mtime guard. The Web UI sets this only after the user
    # explicitly chose "Force save" in the conflict resolution dialog
    # (see issue #763); every force-save emits a WARNING with both mtime
    # values for the audit trail.
    force: bool = False


@router.put("/context/commands/{name}")
async def update_command(
    name: str,
    body: CommandUpdateRequest,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> JSONResponse:
    _reject_non_shared_write(target_tier, "Update command")
    name, resolved = _resolve_existing_command(project_root, name, scope=target_tier)
    if resolved is None:
        raise KeyError(name)
    cmd_path, _layout = resolved

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise HTTPException(422, f"Invalid mtime_ns: {body.mtime_ns!r}")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = cmd_path.stat().st_mtime_ns
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
                        cmd_path,
                        body_mtime_ns,
                        current_mtime_ns,
                    )
                atomic_write_text(cmd_path, body.content)
                new_mtime_ns = cmd_path.stat().st_mtime_ns
    except TimeoutError:
        raise HTTPException(503, "Command update timed out — another sync may be in progress")
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


# ── Delete ───────────────────────────────────────────────────────────────


@router.delete("/context/commands/{name}")
async def delete_command(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    _reject_non_shared_write(target_tier, "Delete command")
    name, resolved = _resolve_existing_command(project_root, name, scope=target_tier)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []

                if resolved is not None:
                    cmd_path, _layout = resolved
                    try:
                        cmd_path.unlink()
                        removed.append(_safe_rel(cmd_path, project_root))
                    except OSError as e:
                        skipped.append(
                            {"path": _safe_rel(cmd_path, project_root), "reason": str(e)}
                        )

                if cascade:
                    for gen in COMMAND_GENERATORS.values():
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
        raise HTTPException(503, "Command delete timed out — another sync may be in progress")

    return {"deleted": removed, "skipped": skipped}


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/commands/{name}/diff")
async def diff_command(
    name: str,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    name, resolved = _resolve_existing_command(project_root, name, scope=target_tier)

    canonical_content = None
    if resolved is not None:
        cmd_path, _layout = resolved
        canonical_content = cmd_path.read_text(encoding="utf-8")

    runtimes = []
    for gen_name, gen in COMMAND_GENERATORS.items():
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
            # For commands, content won't be byte-identical (placeholder rewrites)
            # so we always provide the runtime content for diff view.
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


@router.post("/context/commands/sync")
async def sync_commands(
    body: SyncRequest | None = None,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    _reject_non_shared_write(target_tier, "Sync commands")
    on_drop = body.on_drop if body else "warn"
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_commands(project_root, on_drop=on_drop)
    except TimeoutError:
        raise HTTPException(503, "Commands sync timed out — another sync may be in progress")
    except PrivacyScanError as exc:
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
        "canonical_root": CANONICAL_COMMAND_ROOT,
    }


# ── Import ───────────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False


@router.post("/context/commands/import")
async def import_commands(
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    _reject_non_shared_write(target_tier, "Import commands")
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_commands_to_canonical(project_root, overwrite=overwrite)
    except TimeoutError:
        raise HTTPException(503, "Commands import timed out — another sync may be in progress")
    return {
        "imported": [
            {
                "name": canonical_command_name(p, layout),
                "canonical_path": str(p.relative_to(project_root)),
            }
            for p, layout in result.imported
        ],
        "skipped": [
            {"name": name, "reason": reason, "reason_code": code}
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _COMMAND_SCAN_DIRS,
    }


@router.post("/context/commands/{name}/import")
async def import_command(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
    target_tier: TargetTier = Depends(get_query_target_tier),
) -> dict:
    """Import a single runtime command into ``.memtomem/commands/``.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime file matches the name (the section
    import would silently report 0 imported, which is the wrong shape of
    feedback for "you clicked a specific item that doesn't exist").
    """
    _reject_non_shared_write(target_tier, "Import command")
    try:
        validate_name(name, kind="command name")
    except InvalidNameError as exc:
        raise HTTPException(400, f"Invalid command name: {exc}")
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_commands_to_canonical(
                    project_root, overwrite=overwrite, only_name=name
                )
    except TimeoutError:
        raise HTTPException(503, "Command import timed out — another sync may be in progress")
    if not result.imported and not result.skipped:
        raise HTTPException(404, f"No runtime command named {name!r} to import")
    return {
        "imported": [
            {
                "name": canonical_command_name(p, layout),
                "canonical_path": str(p.relative_to(project_root)),
            }
            for p, layout in result.imported
        ],
        "skipped": [
            {"name": n, "reason": reason, "reason_code": code} for n, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _COMMAND_SCAN_DIRS,
    }
