"""Stats, health, and system bootstrap endpoints, plus sub-router integration.

Path canonicalization invariant: every site in this module that accepts
or returns a ``memory_dir`` path uses ``str(Path(p).expanduser().resolve())``.
The Web UI keys per-row state on the resolved string (see
``static/app.js`` ``_renderMemoryDirGroup``), so reverting any single
site to ``expanduser``-only re-introduces #666 — the per-row stats
badge silently disappears for tilde-prefixed (``~/memories``) or
symlinked-prefix entries (macOS ``/tmp`` → ``/private/tmp``). Mirrored
on the read side at ``memtomem.indexing.engine.memory_dir_stats``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from memtomem.config import (
    FIELD_CONSTRAINTS,
    MUTABLE_FIELDS,
    RerankConfig,
    assign_section_fields,
    build_comparand,
    classify_scope,
    coerce_and_validate,
    memory_dir_kind,
    save_config_overrides,
    saved_embedding_identity,
)
from memtomem.embedding.runtime import publish_onnx_batch_size
from memtomem.search.reranker.factory import create_reranker
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools.memory_writer import append_entry
from memtomem.web import hot_reload as _hot_reload
from memtomem.web.deps import (
    get_config,
    get_embedder,
    get_storage,
)
from memtomem.web.routes import system_config, system_indexing
from memtomem.web.routes._locks import _config_lock
from memtomem.web.routes.system_config import (
    _DENIED_MEMORY_DIR_PARTS,
    _DENIED_MEMORY_DIR_PREFIXES,
    _INTROSPECTABLE_PROVIDERS,
    _LOCALHOST_ADDRS,
    _RERANK_PATCH_FIELDS,
    _ConfigSnapshot,
    _build_config_response,
    _cfg_value,
    _check_reload_block,
    _component_for,
    _declared_default,
    _open_in_file_manager,
    _path_identity_key,
    _require_localhost,
    _resume_watching,
    _resume_watching_locked,
    _validate_reranker_ready,
    add_memory_dir,
    get_builtin_exclude_patterns,
    get_config_defaults,
    get_config_endpoint,
    get_embedding_status,
    get_model_readiness,
    get_privacy_patterns,
    get_privacy_stats,
    memory_dirs_status,
    open_memory_dir,
    patch_config,
    remove_memory_dir,
    reset_embedding,
    save_config,
    validate_memory_dir_path,
)
from memtomem.web.routes.system_indexing import (
    _ADD_PATH_ERROR,
    _ALLOWED_UPLOAD_EXTS,
    _PREVIEW_FILE_CAP,
    _REINDEX_ROOT_NAMESPACE_ERROR,
    _ClosingStreamingResponse,
    _resolve_add_target,
    _revalidate_add_target,
    add_memory,
    index_stream,
    index_stream_get_disabled,
    indexing_active,
    preview_namespace,
    reindex_all,
    trigger_index,
    upload_files,
    uploads_usage,
)
from memtomem.web.schemas.config import (
    ConfigPatchChange,
    ConfigPatchRequest,
    ConfigPatchResponse,
    ConfigResponse,
)
from memtomem.web.schemas.memory import (
    AddMemoryRequest,
    AddMemoryResponse,
    IndexRequest,
    IndexResponse,
    PreviewNamespaceRequest,
    PreviewNamespaceResponse,
    UploadFileResult,
    UploadResponse,
    UploadUsageResponse,
)
from memtomem.web.schemas.sources import (
    HomeFileTypeCount,
    SourceOut,
    StatsResponse,
)

__all__ = [
    "AddMemoryRequest",
    "AddMemoryResponse",
    "ConfigPatchChange",
    "ConfigPatchRequest",
    "ConfigPatchResponse",
    "ConfigResponse",
    "FIELD_CONSTRAINTS",
    "IndexRequest",
    "IndexResponse",
    "MUTABLE_FIELDS",
    "PreviewNamespaceRequest",
    "PreviewNamespaceResponse",
    "RerankConfig",
    "UploadFileResult",
    "UploadResponse",
    "UploadUsageResponse",
    "_ADD_PATH_ERROR",
    "_ALLOWED_UPLOAD_EXTS",
    "_ClosingStreamingResponse",
    "_ConfigSnapshot",
    "_DENIED_MEMORY_DIR_PARTS",
    "_DENIED_MEMORY_DIR_PREFIXES",
    "_INTROSPECTABLE_PROVIDERS",
    "_LOCALHOST_ADDRS",
    "_PREVIEW_FILE_CAP",
    "_REINDEX_ROOT_NAMESPACE_ERROR",
    "_RERANK_PATCH_FIELDS",
    "_build_config_response",
    "_cfg_value",
    "_check_reload_block",
    "_component_for",
    "_config_lock",
    "_declared_default",
    "_hot_reload",
    "_open_in_file_manager",
    "_path_identity_key",
    "_require_localhost",
    "_resolve_add_target",
    "_resume_watching",
    "_resume_watching_locked",
    "_revalidate_add_target",
    "_validate_reranker_ready",
    "add_memory",
    "add_memory_dir",
    "append_entry",
    "assign_section_fields",
    "build_comparand",
    "coerce_and_validate",
    "create_reranker",
    "embed_text",
    "get_bootstrap_state",
    "get_builtin_exclude_patterns",
    "get_config_defaults",
    "get_config_endpoint",
    "get_embedding_status",
    "get_model_readiness",
    "get_privacy_patterns",
    "get_privacy_stats",
    "get_session",
    "get_stats",
    "get_ui_mode",
    "health_active",
    "health_liveness",
    "index_stream",
    "index_stream_get_disabled",
    "indexing_active",
    "memory_dirs_status",
    "open_memory_dir",
    "patch_config",
    "preview_namespace",
    "publish_onnx_batch_size",
    "readiness",
    "rebuild_fts",
    "reindex_all",
    "remove_memory_dir",
    "reset_all",
    "reset_embedding",
    "router",
    "save_config",
    "save_config_overrides",
    "saved_embedding_identity",
    "trigger_index",
    "upload_files",
    "uploads_usage",
    "validate_memory_dir_path",
]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get(
    "/system/ui-mode",
    dependencies=[Depends(_require_localhost)],
)
async def get_ui_mode(request: Request) -> dict[str, str]:
    """Return the current web UI mode (``prod`` or ``dev``).

    The SPA fetches this on boot to decide which tabs and settings sections
    to render. Falls back to ``prod`` if ``app.state.web_mode`` is missing.

    Localhost-guarded for consistency with other ``system`` endpoints — the
    SPA runs same-origin so this doesn't affect it, but it keeps external
    scanners from fingerprinting which installs are in dev mode.
    """
    mode = getattr(request.app.state, "web_mode", "prod")
    return {"mode": mode}


@router.get("/session")
async def get_session(request: Request) -> dict[str, str]:
    """Return the per-process CSRF token + UI mode for SPA bootstrap.

    The SPA's ``api(...)`` helper calls this once on first unsafe request
    and caches the token, threading ``X-Memtomem-CSRF`` through every
    subsequent ``POST``/``PATCH``/``PUT``/``DELETE``. Token rotation is
    by ``mm web`` restart — there is no in-process rotation surface (RFC
    #787 explicitly out-of-scope).

    Not localhost-guarded at the route layer: ``CSRFGuardMiddleware``
    already covers the Origin / Host checks for every ``/api/*`` request,
    including this one. Adding ``_require_localhost`` here would
    duplicate the Host check and tie the token endpoint to a different
    failure shape than the rest of the gate, making the AST registry's
    "one seam, one surface" assertion harder to keep clean.
    """
    return {
        "csrf": getattr(request.app.state, "csrf_token", ""),
        "mode": getattr(request.app.state, "web_mode", "prod"),
    }


@router.get("/bootstrap")
async def get_bootstrap_state(request: Request) -> dict[str, Any]:
    """Return one state snapshot that drives first-run and recovery UI.

    This route deliberately has no ``require_configured`` dependency: the SPA
    must be able to explain an unconfigured or degraded install instead of
    discovering it through a cascade of unrelated 409/503 responses.
    """
    configured = (Path.home() / ".memtomem" / "config.json").exists()
    startup_state = getattr(request.app.state, "startup_state", "not_started")
    config = getattr(request.app.state, "config", None)
    storage = getattr(request.app.state, "storage", None)

    total_chunks = 0
    total_sources = 0
    if storage is not None:
        try:
            stats = await storage.get_stats()
            total_chunks = int(stats.get("total_chunks", 0))
            total_sources = int(stats.get("total_sources", 0))
        except Exception:
            logger.debug("bootstrap stats unavailable", exc_info=True)

    memory_dirs = []
    project_memory_dirs = []
    read_only_memory_dirs = []
    project_context_root = None
    db_path = None
    mismatch = False
    if config is not None:
        memory_dirs = [str(Path(p).expanduser().resolve()) for p in config.indexing.memory_dirs]
        project_memory_dirs = [
            str(Path(p).expanduser().resolve()) for p in config.indexing.project_memory_dirs
        ]
        read_only_memory_dirs = [
            str(Path(p).expanduser().resolve()) for p in config.indexing.read_only_memory_dirs
        ]
        db_path = str(Path(config.storage.sqlite_path).expanduser().resolve())
        from memtomem.runtime.project_context import _resolve_project_context_from_dirs

        root = _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)
        project_context_root = str(root) if root is not None else None
    if storage is not None:
        mismatch = getattr(storage, "embedding_mismatch", None) is not None

    if not configured:
        stage = "unconfigured"
    elif startup_state != "ready" or storage is None:
        stage = "startup_unavailable"
    elif mismatch:
        stage = "degraded"
    elif total_sources == 0:
        stage = "needs_source"
    else:
        stage = "ready"

    return {
        "stage": stage,
        "configured": configured,
        "startup_state": startup_state,
        "total_chunks": total_chunks,
        "total_sources": total_sources,
        "db_path": db_path,
        "memory_dirs": memory_dirs,
        "project_memory_dirs": project_memory_dirs,
        "read_only_memory_dirs": read_only_memory_dirs,
        "project_context_root": project_context_root,
        "embedding_mismatch": mismatch,
    }


@router.get("/health")
async def health_liveness() -> dict[str, Any]:
    return {"status": "ok", "checks": {"process": "ok"}}


@router.get("/readiness")
async def readiness(request: Request) -> JSONResponse:
    state = getattr(request.app.state, "startup_state", "not_started")
    ready = state == "ready"
    content: dict[str, Any] = {"ready": ready, "state": state}
    if not ready:
        content["reason_code"] = "startup_unavailable"
    return JSONResponse(
        status_code=200 if ready else 503,
        content=content,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/health", response_model=None)
async def health_active(
    storage: Any = Depends(get_storage),
    embedder: Any = Depends(get_embedder),
) -> dict[str, Any] | JSONResponse:
    checks: dict[str, str] = {}
    try:
        await storage.get_stats()
        checks["storage"] = "ok"
    except Exception:
        logger.warning("Health check failed: storage", exc_info=True)
        checks["storage"] = "error"

    try:
        await embedder.embed_texts(["health check"])
        checks["embedding"] = "ok"
    except Exception:
        logger.warning("Health check failed: embedding", exc_info=True)
        checks["embedding"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    if all_ok:
        return {"status": "ok", "checks": checks}
    return JSONResponse(
        status_code=503,
        content={"status": "degraded", "checks": checks},
    )


@router.post("/embed", dependencies=[Depends(_require_localhost)])
async def embed_text(request: Request, embedder=Depends(get_embedder)):
    """Return embedding vector for a given text."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="text too long (max 5000 chars)")

    try:
        vectors = await embedder.embed_texts([text])
        return {"embedding": vectors[0]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Embedding failed") from exc


@router.post("/reset", dependencies=[Depends(_require_localhost)])
async def reset_all(storage=Depends(get_storage)):
    """Delete ALL data and reinitialize the database. Embedding config preserved."""
    deleted = await storage.reset_all()
    total = sum(deleted.values())
    return {
        "ok": True,
        "deleted": deleted,
        "total_deleted": total,
        "message": f"Database reset complete. {total} rows deleted across {len([v for v in deleted.values() if v])} tables.",
    }


@router.post("/fts-rebuild", dependencies=[Depends(_require_localhost)])
async def rebuild_fts(storage=Depends(get_storage)):
    """Rebuild the FTS5 full-text index using the current tokenizer."""
    count = await storage.rebuild_fts()
    return {"ok": True, "rebuilt_rows": count, "message": f"FTS index rebuilt for {count} chunks."}


@router.get("/stats", response_model=StatsResponse)
async def get_stats(storage=Depends(get_storage), config=Depends(get_config)) -> StatsResponse:
    from datetime import datetime, timezone

    from memtomem.indexing.engine import norm_dir_prefix

    def _to_datetime(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, TypeError):
            return None

    data = await storage.get_stats()
    distribution = await storage.get_chunk_size_distribution()

    pmdirs = config.indexing.project_memory_dirs
    # Every index root owns its sources, matching ``GET /api/sources`` —
    # a read-only or project root's files are not orphans. (#2522)
    indexed_dirs = sorted(
        (
            (norm_dir_prefix(d), str(Path(d).expanduser().resolve()), memory_dir_kind(d))
            for d in config.indexing.all_index_roots()
        ),
        key=lambda t: -len(t[0]),
    )

    all_sources: list[SourceOut] = []
    rows = await storage.get_source_files_with_counts()
    for p, cnt, last_indexed_iso, ns_csv, avg_tok, min_tok, max_tok in rows:
        source_scope, _src_project_root = classify_scope(p, pmdirs)
        if source_scope == "project_local":
            continue

        namespaces = ns_csv.split(",") if ns_csv else ["default"]
        target = norm_path(p)
        match = next(
            (
                (dir_path, dir_kind)
                for prefix, dir_path, dir_kind in indexed_dirs
                if target.startswith(prefix)
            ),
            None,
        )

        source_kind: str | None = None
        memory_dir_str: str | None = None
        if match is None:
            source_kind = None
            memory_dir_str = None
        else:
            owning_dir, source_kind = match
            memory_dir_str = str(owning_dir)

        try:
            file_size = Path(p).stat().st_size
        except OSError:
            file_size = None

        all_sources.append(
            SourceOut(
                path=str(p),
                chunk_count=cnt,
                last_indexed_at=_to_datetime(last_indexed_iso),
                file_size=file_size,
                namespaces=namespaces,
                avg_tokens=avg_tok,
                min_tokens=min_tok,
                max_tokens=max_tok,
                memory_dir=memory_dir_str,
                kind=source_kind,
                target_scope=source_scope,
            )
        )

    file_type_counts: dict[str, int] = {}
    for s in all_sources:
        if not s.path:
            continue
        file_type = s.path.split(".").pop().lower()
        file_type_counts[file_type or "other"] = file_type_counts.get(file_type or "other", 0) + 1

    file_type_distribution = [
        HomeFileTypeCount(file_type=k, count=v)
        for k, v in sorted(file_type_counts.items(), key=lambda item: item[1], reverse=True)
    ]
    total_source_size = sum((s.file_size or 0) for s in all_sources)
    recent_sources = sorted(
        all_sources,
        key=lambda s: s.last_indexed_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return StatsResponse(
        total_chunks=data.get("total_chunks", 0),
        total_sources=data.get("total_sources", 0),
        chunk_size_distribution=distribution,
        home_sources=all_sources,
        home_recent_sources=recent_sources[:8],
        home_total_source_size=total_source_size,
        home_file_type_distribution=file_type_distribution,
    )


router.include_router(system_config.router)
router.include_router(system_indexing.router)
