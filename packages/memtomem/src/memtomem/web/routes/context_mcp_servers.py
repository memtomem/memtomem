"""Context gateway — MCP server definitions CRUD, diff, and project fan-out."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem.config import TargetScope
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import validate_name
from memtomem.context.mcp_servers import (
    CANONICAL_MCP_SERVER_ROOT,
    MCP_RUNTIME,
    McpServerParseError,
    McpServerPrivacyError,
    canonical_mcp_server_path,
    diff_mcp_servers,
    format_mcp_server_definition,
    generate_all_mcp_servers,
    list_canonical_mcp_servers,
    parse_canonical_mcp_server,
    parse_mcp_server_text,
    scan_mcp_server_text,
)
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes.context_projects import (
    resolve_scope_root,
    resolve_writable_scope_root,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-mcp-servers"])


def _safe_rel(p: Path, project_root: Path) -> str:
    try:
        return str(p.relative_to(project_root))
    except ValueError:
        return str(p)


def _reject_non_shared_write(target_scope: TargetScope, action: str) -> None:
    """Guard a *write* (create/update/delete/sync) to project_shared only.

    Writes reject non-shared tiers because an MCP server canonical can only
    live in project_shared. Reads intentionally do NOT call this — they return
    an empty/absent result for other tiers instead (see ``list_mcp_servers``),
    so a tier switch never turns the panel into a load-failed state.
    """
    if target_scope != "project_shared":
        raise HTTPException(
            status_code=400,
            detail=(
                f"{action} is supported only on project_shared in this release; "
                f"got target_scope={target_scope!r}."
            ),
        )


@router.get("/context/mcp-servers")
async def list_mcp_servers(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to list. Only project_shared is supported in v1.",
    ),
) -> dict:
    # Reads (unlike writes) must never 400 on a tier switch — that turns the
    # generic ``loadCtxList`` panel into a load-failed state. MCP server
    # canonicals only ever reside in project_shared (``.memtomem/mcp-servers/``),
    # so for other tiers there is simply nothing to list: return empty, mirroring
    # the overview / projects-counts path (``mcp_servers: 0`` for non-shared) and
    # the skills/commands/agents read convention (only their writes reject).
    if target_scope != "project_shared":
        return {
            "mcp-servers": [],
            "canonical_root": CANONICAL_MCP_SERVER_ROOT,
            "scanned_dirs": [".mcp.json"],
        }
    diff_by_name = {
        name: [{"runtime": runtime, "status": status}]
        for runtime, name, status in diff_mcp_servers(project_root)
    }
    servers = []
    for path in list_canonical_mcp_servers(project_root):
        name = path.stem
        servers.append(
            {
                "name": name,
                "canonical_path": _safe_rel(path, project_root),
                "target_scope": target_scope,
                "runtimes": diff_by_name.get(name, []),
            }
        )
    return {
        "mcp-servers": servers,
        "canonical_root": CANONICAL_MCP_SERVER_ROOT,
        "scanned_dirs": [".mcp.json"],
    }


@router.get("/context/mcp-servers/{name}")
async def read_mcp_server(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to read from. Only project_shared is supported in v1.",
    ),
) -> dict:
    name = validate_name(name, kind="MCP server")
    # No MCP server canonical exists outside project_shared — surface it as a
    # plain 404 (KeyError) rather than a 400, keeping reads tier-tolerant like
    # the list route above and the skills/commands/agents read endpoints.
    if target_scope != "project_shared":
        raise KeyError(name)
    path = canonical_mcp_server_path(project_root, name)
    if not path.is_file():
        raise KeyError(name)
    content = path.read_text(encoding="utf-8")
    fields = {}
    try:
        parsed = parse_canonical_mcp_server(path)
        fields = {
            "command": parsed.definition.get("command", ""),
            "args_count": len(parsed.definition.get("args") or []),
            "env_count": len(parsed.definition.get("env") or {}),
        }
    except McpServerParseError:
        pass
    return {
        "name": name,
        "content": content,
        "mtime_ns": str(path.stat().st_mtime_ns),
        "fields": fields,
        "target_scope": target_scope,
        "layout": "flat",
    }


class McpServerCreateRequest(BaseModel):
    name: str
    content: str


@router.post("/context/mcp-servers")
async def create_mcp_server(
    body: McpServerCreateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to create in. Only project_shared is supported in v1.",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Create MCP server")
    name = validate_name(body.name, kind="MCP server")
    path = canonical_mcp_server_path(project_root, name)
    try:
        parse_mcp_server_text(body.content, name=name, source=path)
        scan_mcp_server_text(
            body.content,
            source_path=path,
            project_root=project_root,
            surface="web_context_mcp_servers_create",
        )
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if path.exists():
                    raise HTTPException(409, detail=f"MCP server '{name}' already exists")
                atomic_write_text(path, body.content)
    except TimeoutError:
        raise HTTPException(503, "MCP server create timed out — another sync may be in progress")
    except McpServerParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except McpServerPrivacyError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"name": name, "canonical_path": _safe_rel(path, project_root)}


class McpServerUpdateRequest(BaseModel):
    content: str
    mtime_ns: str
    force: bool = False


async def _update_mcp_server_impl(
    name: str,
    body: McpServerUpdateRequest,
    project_root: Path,
    target_scope: TargetScope,
) -> JSONResponse:
    _reject_non_shared_write(target_scope, "Update MCP server")
    name = validate_name(name, kind="MCP server")
    path = canonical_mcp_server_path(project_root, name)
    if not path.is_file():
        raise KeyError(name)
    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise HTTPException(422, f"Invalid mtime_ns: {body.mtime_ns!r}") from None

    try:
        parse_mcp_server_text(body.content, name=name, source=path)
        scan_mcp_server_text(
            body.content,
            source_path=path,
            project_root=project_root,
            surface="web_context_mcp_servers_update",
        )
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = path.stat().st_mtime_ns
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
                        path,
                        body_mtime_ns,
                        current_mtime_ns,
                    )
                atomic_write_text(path, body.content)
                new_mtime_ns = path.stat().st_mtime_ns
    except TimeoutError:
        raise HTTPException(503, "MCP server update timed out — another sync may be in progress")
    except McpServerParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except McpServerPrivacyError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


@router.put("/context/mcp-servers/{name}")
async def update_mcp_server(
    name: str,
    body: McpServerUpdateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to update. Only project_shared is supported in v1.",
    ),
) -> JSONResponse:
    return await _update_mcp_server_impl(name, body, project_root, target_scope)


@router.patch("/context/mcp-servers/{name}")
async def patch_mcp_server(
    name: str,
    body: McpServerUpdateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to update. Only project_shared is supported in v1.",
    ),
) -> JSONResponse:
    return await _update_mcp_server_impl(name, body, project_root, target_scope)


@router.delete("/context/mcp-servers/{name}")
async def delete_mcp_server(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to delete from. Only project_shared is supported in v1.",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Delete MCP server")
    name = validate_name(name, kind="MCP server")
    path = canonical_mcp_server_path(project_root, name)
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []
                if path.is_file():
                    try:
                        path.unlink()
                        removed.append(_safe_rel(path, project_root))
                    except OSError as exc:
                        skipped.append({"path": _safe_rel(path, project_root), "reason": str(exc)})
                if cascade:
                    mcp_path = project_root / ".mcp.json"
                    if mcp_path.is_file():
                        # Keep v1 conservative: delete only canonical; runtime
                        # entry cleanup belongs in a follow-up with conflict UI.
                        skipped.append(
                            {
                                "path": _safe_rel(mcp_path, project_root),
                                "reason": "cascade delete for .mcp.json entries is not supported in v1",
                            }
                        )
    except TimeoutError:
        raise HTTPException(503, "MCP server delete timed out — another sync may be in progress")
    return {"deleted": removed, "skipped": skipped}


@router.get("/context/mcp-servers/{name}/diff")
async def diff_mcp_server(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to diff. Only project_shared is supported in v1.",
    ),
) -> dict:
    name = validate_name(name, kind="MCP server")
    # Tier-tolerant read (see ``list_mcp_servers``): outside project_shared
    # there is no canonical to compare, so report ``missing canonical`` rather
    # than 400. The UI never reaches this in a non-shared tier (the list is
    # empty there), but a direct call stays consistent with the read routes.
    if target_scope != "project_shared":
        return {
            "name": name,
            "canonical_content": None,
            "runtimes": [
                {"runtime": MCP_RUNTIME, "status": "missing canonical", "runtime_content": None}
            ],
        }
    path = canonical_mcp_server_path(project_root, name)
    canonical_content = None
    canonical_definition = None
    if path.is_file():
        canonical_content = path.read_text(encoding="utf-8")
        try:
            canonical_definition = parse_canonical_mcp_server(path).definition
        except McpServerParseError:
            pass

    status = "missing target"
    runtime_content = None
    mcp_path = project_root / ".mcp.json"
    if canonical_definition is None:
        status = "parse error" if canonical_content is not None else "missing canonical"
    elif mcp_path.is_file():
        try:
            import json

            config = json.loads(mcp_path.read_text(encoding="utf-8"))
            target_definition = (config.get("mcpServers") or {}).get(name)
            if target_definition is None:
                status = "missing target"
            else:
                runtime_content = format_mcp_server_definition(target_definition)
                status = "in sync" if target_definition == canonical_definition else "out of sync"
        except Exception:
            status = "parse error"

    return {
        "name": name,
        "canonical_content": canonical_content,
        "runtimes": [
            {
                "runtime": MCP_RUNTIME,
                "status": status,
                "runtime_content": runtime_content,
            }
        ],
    }


@router.post("/context/mcp-servers/sync")
async def sync_mcp_servers(
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to fan out. Only project_shared is supported in v1.",
    ),
) -> dict:
    _reject_non_shared_write(target_scope, "Sync MCP servers")
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = generate_all_mcp_servers(project_root)
    except TimeoutError:
        raise HTTPException(503, "MCP server sync timed out — another sync may be in progress")
    except McpServerParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except McpServerPrivacyError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "generated": [
            {"runtime": runtime, "path": _safe_rel(path, project_root)}
            for runtime, path in result.generated
        ],
        "skipped": [
            {"runtime": runtime, "reason": reason, "reason_code": code}
            for runtime, reason, code in result.skipped
        ],
        "canonical_root": CANONICAL_MCP_SERVER_ROOT,
    }
