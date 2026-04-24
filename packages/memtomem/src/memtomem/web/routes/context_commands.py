"""Context gateway — Commands CRUD, diff, sync, import, and rendered output."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import validate_name
from memtomem.context.commands import (
    CANONICAL_COMMAND_ROOT,
    COMMAND_GENERATORS,
    CommandParseError,
    diff_commands,
    extract_commands_to_canonical,
    generate_all_commands,
    list_canonical_commands,
    parse_canonical_command,
)
from memtomem.web.deps import get_project_root
from memtomem.web.routes._locks import _gateway_lock

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-commands"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _commands_root(project_root: Path) -> Path:
    return project_root / CANONICAL_COMMAND_ROOT


def _canonical_command_path(project_root: Path, raw_name: str) -> Path:
    """Validate the name via core and return the canonical command path."""
    name = validate_name(raw_name, kind="command")
    return _commands_root(project_root) / f"{name}.md"


def _safe_rel(p: Path, project_root: Path) -> str:
    try:
        return str(p.relative_to(project_root))
    except ValueError:
        return str(p)


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/commands")
async def list_commands(
    project_root: Path = Depends(get_project_root),
) -> dict:
    canonicals = list_canonical_commands(project_root)
    diffs = diff_commands(project_root)

    by_name: dict[str, list[dict]] = {}
    for runtime, cmd_name, status in diffs:
        by_name.setdefault(cmd_name, []).append({"runtime": runtime, "status": status})

    commands: list[dict[str, object]] = []
    for cmd_path in canonicals:
        name = cmd_path.stem
        commands.append(
            {
                "name": name,
                "canonical_path": str(cmd_path.relative_to(project_root)),
                "runtimes": by_name.get(name, []),
            }
        )

    canonical_names = {p.stem for p in canonicals}
    for cmd_name, runtimes in by_name.items():
        if cmd_name not in canonical_names:
            commands.append({"name": cmd_name, "canonical_path": None, "runtimes": runtimes})

    return {"commands": commands}


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/commands/{name}")
async def read_command(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    cmd_path = _canonical_command_path(project_root, name)
    if not cmd_path.is_file():
        raise KeyError(name)

    content = cmd_path.read_text(encoding="utf-8")
    # mtime_ns serialized as string (JS bigint-unsafe).
    mtime_ns = cmd_path.stat().st_mtime_ns

    fields: dict = {}
    try:
        parsed = parse_canonical_command(cmd_path)
        fields = {
            "description": parsed.description,
            "argument_hint": parsed.argument_hint,
            "allowed_tools": parsed.allowed_tools,
            "model": parsed.model,
        }
    except CommandParseError:
        pass

    return {"name": name, "content": content, "mtime_ns": str(mtime_ns), "fields": fields}


# ── Rendered (per-runtime output with dropped fields) ────────────────────


@router.get("/context/commands/{name}/rendered")
async def rendered_command(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    cmd_path = _canonical_command_path(project_root, name)
    if not cmd_path.is_file():
        raise KeyError(name)

    content = cmd_path.read_text(encoding="utf-8")
    try:
        parsed = parse_canonical_command(cmd_path)
    except CommandParseError as exc:
        return JSONResponse(status_code=422, content={"detail": f"Parse error: {exc}"})

    runtimes = []
    diffs = diff_commands(project_root)
    status_map: dict[tuple[str, str], str] = {(rt, n): s for rt, n, s in diffs}

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

    return JSONResponse(
        content={
            "name": name,
            "canonical_content": content,
            "runtimes": runtimes,
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
) -> dict:
    cmd_path = _canonical_command_path(project_root, body.name)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if cmd_path.exists():
                    raise ValueError(f"Command '{body.name}' already exists")
                atomic_write_text(cmd_path, body.content)
    except TimeoutError:
        raise HTTPException(503, "Command create timed out — another sync may be in progress")
    return {"name": body.name, "canonical_path": str(cmd_path.relative_to(project_root))}


# ── Update ───────────────────────────────────────────────────────────────


class CommandUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str


@router.put("/context/commands/{name}")
async def update_command(
    name: str,
    body: CommandUpdateRequest,
    project_root: Path = Depends(get_project_root),
) -> JSONResponse:
    cmd_path = _canonical_command_path(project_root, name)
    if not cmd_path.is_file():
        raise KeyError(name)

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise HTTPException(422, f"Invalid mtime_ns: {body.mtime_ns!r}")

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = cmd_path.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "aborted",
                            "reason": ("File was modified by another process. Reload and retry."),
                            "mtime_ns": str(current_mtime_ns),
                        },
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
) -> dict:
    cmd_path = _canonical_command_path(project_root, name)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []

                if cmd_path.is_file():
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
) -> dict:
    cmd_path = _canonical_command_path(project_root, name)

    canonical_content = None
    if cmd_path.is_file():
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
) -> dict:
    on_drop = body.on_drop if body else "warn"
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_commands(project_root, on_drop=on_drop)
    except TimeoutError:
        raise HTTPException(503, "Commands sync timed out — another sync may be in progress")
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


@router.post("/context/commands/import")
async def import_commands(
    body: ImportRequest | None = None,
    project_root: Path = Depends(get_project_root),
) -> dict:
    overwrite = body.overwrite if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = extract_commands_to_canonical(project_root, overwrite=overwrite)
    except TimeoutError:
        raise HTTPException(503, "Commands import timed out — another sync may be in progress")
    return {
        "imported": [
            {"name": p.stem, "canonical_path": str(p.relative_to(project_root))}
            for p in result.imported
        ],
        "skipped": [{"name": name, "reason": reason} for name, reason in result.skipped],
    }
