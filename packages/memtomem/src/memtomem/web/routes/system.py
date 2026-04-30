"""Stats, indexing, and memory-add endpoints."""

from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from memtomem.config import (
    FIELD_CONSTRAINTS,
    MUTABLE_FIELDS,
    build_comparand,
    coerce_and_validate,
    memory_dir_kind,
    save_config_overrides,
)
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools.memory_writer import append_entry
from memtomem.web import hot_reload as _hot_reload
from memtomem.web.deps import (
    get_config,
    get_embedder,
    get_index_engine,
    get_search_pipeline,
    get_storage,
    require_configured,
)
from memtomem.web.routes._locks import _config_lock
from memtomem.web.schemas.config import (
    BuiltinExcludePatternsResponse,
    ConfigDecayOut,
    ConfigEmbeddingOut,
    ConfigIndexingOut,
    ConfigMMROut,
    ConfigNamespaceOut,
    ConfigPatchChange,
    ConfigPatchRequest,
    ConfigPatchResponse,
    ConfigResponse,
    ConfigSearchOut,
    ConfigStorageOut,
    EmbeddingConfigInfo,
    EmbeddingResetResponse,
    EmbeddingStatusResponse,
    PrivacyPatternEntry,
    PrivacyPatternsResponse,
)
from memtomem.web.schemas.memory import (
    AddMemoryRequest,
    AddMemoryResponse,
    IndexRequest,
    IndexResponse,
    PreviewNamespaceResponse,
    UploadFileResult,
    UploadResponse,
    UploadUsageResponse,
)
from memtomem.web.schemas.sources import StatsResponse

logger = logging.getLogger(__name__)

_LOCALHOST_ADDRS = {"127.0.0.1", "::1", "localhost"}


def _check_reload_block(request: Request) -> None:
    """Reject writes while a reload error is live for the current disk state.

    Writing would call :func:`save_config_overrides`, overwriting the broken
    disk file and destroying any recovery trail. User must fix disk first
    (``mm init --fresh`` or manual edit).
    """
    err = _hot_reload.get_reload_error(request.app)
    if err is None:
        return
    if err.at_mtime_ns != _hot_reload.get_config_mtime_ns():
        # Disk was fixed since the error was recorded; let the next reload
        # attempt clear it.
        return
    raise HTTPException(
        status_code=409,
        detail=f"Config file invalid on disk: {err.message}. "
        "Fix it (or run `mm init --fresh`) before saving from the UI.",
    )


def _require_localhost(request: Request) -> None:
    """Block non-localhost access to sensitive endpoints."""
    client = request.client
    if client and client.host not in _LOCALHOST_ADDRS:
        raise HTTPException(status_code=403, detail="This endpoint is restricted to localhost")


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


@router.get("/health")
async def health(storage=Depends(get_storage), embedder=Depends(get_embedder)):
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


def _build_config_response(
    cfg, *, mtime_ns: int = -1, reload_error: str | None = None
) -> ConfigResponse:
    """Build ConfigResponse from a Mem2MemConfig instance."""
    return ConfigResponse(
        embedding=ConfigEmbeddingOut(
            provider=cfg.embedding.provider,
            model=cfg.embedding.model,
            dimension=cfg.embedding.dimension,
            base_url=cfg.embedding.base_url,
            batch_size=cfg.embedding.batch_size,
            api_key="***" if cfg.embedding.api_key else "",
        ),
        storage=ConfigStorageOut(
            backend=cfg.storage.backend,
            sqlite_path=str(Path(cfg.storage.sqlite_path).expanduser().resolve()),
            collection_name=cfg.storage.collection_name,
        ),
        search=ConfigSearchOut(
            default_top_k=cfg.search.default_top_k,
            bm25_candidates=cfg.search.bm25_candidates,
            dense_candidates=cfg.search.dense_candidates,
            rrf_k=cfg.search.rrf_k,
            enable_bm25=cfg.search.enable_bm25,
            enable_dense=cfg.search.enable_dense,
            tokenizer=cfg.search.tokenizer,
            rrf_weights=cfg.search.rrf_weights,
        ),
        indexing=ConfigIndexingOut(
            memory_dirs=[str(Path(p).expanduser().resolve()) for p in cfg.indexing.memory_dirs],
            supported_extensions=sorted(cfg.indexing.supported_extensions),
            max_chunk_tokens=cfg.indexing.max_chunk_tokens,
            min_chunk_tokens=cfg.indexing.min_chunk_tokens,
            target_chunk_tokens=cfg.indexing.target_chunk_tokens,
            chunk_overlap_tokens=cfg.indexing.chunk_overlap_tokens,
            structured_chunk_mode=cfg.indexing.structured_chunk_mode,
            exclude_patterns=list(cfg.indexing.exclude_patterns),
        ),
        decay=ConfigDecayOut(
            enabled=cfg.decay.enabled,
            half_life_days=cfg.decay.half_life_days,
        ),
        mmr=ConfigMMROut(
            enabled=cfg.mmr.enabled,
            lambda_param=cfg.mmr.lambda_param,
        ),
        namespace=ConfigNamespaceOut(
            default_namespace=cfg.namespace.default_namespace,
            enable_auto_ns=cfg.namespace.enable_auto_ns,
        ),
        config_mtime_ns=mtime_ns,
        config_reload_error=reload_error,
    )


@router.get("/config", response_model=ConfigResponse)
async def get_config_endpoint(request: Request) -> ConfigResponse:
    # Read-through reload is opportunistic and lock-free: if a write is in
    # flight, the writer will serve the fresh view on its own return. This
    # keeps the common GET path cheap while still catching CLI-side edits.
    app = request.app
    try:
        _hot_reload.reload_if_stale(
            app,
            storage=getattr(app.state, "storage", None),
            search_pipeline=getattr(app.state, "search_pipeline", None),
        )
    except Exception:
        logger.warning("reload_if_stale raised unexpectedly during GET /config", exc_info=True)

    cfg = app.state.config
    err = _hot_reload.get_reload_error(app)
    return _build_config_response(
        cfg,
        mtime_ns=_hot_reload.get_config_mtime_ns(),
        reload_error=err.message if err is not None else None,
    )


@router.get("/config/defaults", response_model=ConfigResponse)
async def get_config_defaults() -> ConfigResponse:
    """Return the comparand config (defaults + env + ``config.d/`` fragments).

    Powers the Web UI per-field reset-to-default button: the client fetches
    these values to pre-fill a field when the user clicks ↺. Note that this
    is not "pristine code default" — if ``MEMTOMEM_MMR__ENABLED=true`` is in
    the environment, the comparand reflects ``true``, so ↺ shows what the
    field would revert to if ``~/.memtomem/config.json`` didn't pin it.
    After the user clicks Save, ``save_config_overrides`` drops the entry
    (now equal to comparand) and env/fragment values continue to flow.

    Read-only; no reload interaction needed.
    """
    return _build_config_response(build_comparand(quiet=True))


@router.get(
    "/indexing/builtin-exclude-patterns",
    response_model=BuiltinExcludePatternsResponse,
)
async def get_builtin_exclude_patterns() -> BuiltinExcludePatternsResponse:
    """Return the read-only built-in exclude pattern groups."""
    from memtomem.indexing.engine import _BUILTIN_NOISE_PATTERNS, _BUILTIN_SECRET_PATTERNS

    return BuiltinExcludePatternsResponse(
        secret=list(_BUILTIN_SECRET_PATTERNS),
        noise=list(_BUILTIN_NOISE_PATTERNS),
    )


@router.get("/privacy/patterns", response_model=PrivacyPatternsResponse)
async def get_privacy_patterns() -> PrivacyPatternsResponse:
    """Return the LTM secret-class redaction patterns in JS-RegExp shape.

    The Web UI's compose-mode privacy warning fetches this once on load
    and uses it to scan textarea content client-side. Each entry is a
    ``{pattern, flags}`` pair already translated to JS-compatible form
    by ``privacy.to_js_pattern`` — Python inline flag groups like
    ``(?i)`` are lifted out of the body, since ``new RegExp("(?i)…")``
    rejects them.

    Read-only metadata; no ``require_configured`` gate (mirrors
    ``/api/config`` and ``/api/indexing/builtin-exclude-patterns``).
    """
    from memtomem import privacy

    return PrivacyPatternsResponse(
        patterns=[PrivacyPatternEntry(**entry) for entry in privacy.JS_PATTERNS],
        sha=privacy.JS_PATTERNS_SHA,
    )


# ---------------------------------------------------------------------------
# PATCH /api/config — runtime configuration update
# ---------------------------------------------------------------------------


# _MUTABLE_FIELDS, _FIELD_CONSTRAINTS, _coerce_and_validate are imported
# from memtomem.config (canonical single source of truth).


@router.patch("/config", response_model=ConfigPatchResponse)
async def patch_config(
    req: ConfigPatchRequest,
    request: Request,
    persist: bool = False,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
):
    """Update mutable runtime configuration fields."""
    applied: list[ConfigPatchChange] = []
    rejected: list[str] = []
    tokenizer_changed = False

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                # Re-read from disk before merging so a concurrent CLI edit
                # is preserved. If disk is broken, refuse rather than
                # overwrite it.
                _hot_reload.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                config = request.app.state.config

                for section_name, updates in req.model_dump(exclude_none=True).items():
                    allowed = MUTABLE_FIELDS.get(section_name, set())
                    section_obj = getattr(config, section_name, None)
                    if section_obj is None:
                        rejected.append(f"{section_name}: unknown section")
                        continue

                    for key, value in updates.items():
                        full_key = f"{section_name}.{key}"
                        if key not in allowed:
                            rejected.append(f"{full_key}: read-only field")
                            continue

                        constraint = FIELD_CONSTRAINTS.get(full_key)
                        try:
                            coerced = coerce_and_validate(value, constraint)
                        except ValueError as e:
                            rejected.append(f"{full_key}: {e}")
                            continue

                        old_val = getattr(section_obj, key)
                        setattr(section_obj, key, coerced)
                        if full_key == "search.tokenizer" and old_val != coerced:
                            tokenizer_changed = True
                        applied.append(
                            ConfigPatchChange(
                                field=full_key,
                                old_value=str(old_val),
                                new_value=str(coerced),
                            )
                        )

                # Runtime fanout: tokenizer FTS rebuild + cache invalidation.
                # Shared with the reload path via
                # ``apply_runtime_config_changes`` so a disk-triggered change
                # fires the same side-effects as an in-process PATCH.
                if tokenizer_changed:
                    from memtomem.storage.fts_tokenizer import set_tokenizer

                    set_tokenizer(config.search.tokenizer)
                    count = await storage.rebuild_fts()
                    logger.info(
                        "FTS index rebuilt with tokenizer=%s (%d chunks)",
                        config.search.tokenizer,
                        count,
                    )

                if applied:
                    search_pipeline.invalidate_cache()

                if persist:
                    save_config_overrides(config)
                    # Self-write mtime bump — otherwise the next GET sees
                    # our own edit as "external" and reloads spuriously.
                    _hot_reload.commit_writer_signature(request.app)
    except TimeoutError:
        raise HTTPException(503, "Config update timed out — another update may be in progress")

    return ConfigPatchResponse(applied=applied, rejected=rejected)


@router.post("/config/save")
async def save_config(
    request: Request,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
):
    """Persist current mutable config to ~/.memtomem/config.json."""
    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                _hot_reload.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                save_config_overrides(request.app.state.config)
                _hot_reload.commit_writer_signature(request.app)
    except TimeoutError:
        raise HTTPException(503, "Config save timed out — another update may be in progress")
    return {"ok": True, "message": "Config saved to ~/.memtomem/config.json"}


@router.post("/memory-dirs/add", dependencies=[Depends(require_configured)])
async def add_memory_dir(
    request: Request,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
    index_engine=Depends(get_index_engine),
):
    """Add a directory to memory_dirs watch list, optionally indexing immediately.

    Body:
        path (str, required): Absolute or ``~``-relative path.
        auto_index (bool, default True): Index the dir immediately after
            registration so a single call covers register + index +
            watcher activation. Direct-API callers that want the historic
            register-only behavior must pass ``auto_index=false``
            explicitly. JSON ``null`` is treated the same as ``false``
            (opt-out), distinct from field omission which fires the
            default. PR #571 shipped this as opt-in
            (``default=False``); PR #576 flipped the default as a
            follow-up.
    """
    body = await request.json()
    dir_path = body.get("path", "").strip()
    auto_index = bool(body.get("auto_index", True))
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")

    resolved = Path(dir_path).expanduser().resolve()
    if not resolved.is_dir():
        resolved.mkdir(parents=True, exist_ok=True)

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                _hot_reload.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                config = request.app.state.config

                current = [Path(p).expanduser().resolve() for p in config.indexing.memory_dirs]
                # ``kind`` is preserved on the response for downstream
                # consumers (CLI scripts, settings UI). The Web UI's
                # historic "Switch view" toast was retired in PR #568 when
                # the Memory/General sub-toggle disappeared, but the
                # field stays for API stability.
                kind = memory_dir_kind(resolved)
                already_present = norm_path(resolved) in {norm_path(p) for p in current}

                if not already_present:
                    config.indexing.memory_dirs.append(resolved)
                    save_config_overrides(config)
                    _hot_reload.commit_writer_signature(request.app)

                memory_dirs_snapshot = [
                    str(Path(p).expanduser().resolve()) for p in config.indexing.memory_dirs
                ]
                message = "Already in memory_dirs" if already_present else f"Added {resolved}"
    except TimeoutError:
        raise HTTPException(503, "memory-dirs/add timed out — another update may be in progress")

    # Index outside the config lock so a slow scan doesn't block other
    # config writers (the watcher invariant — path inside ``memory_dirs``
    # — is already satisfied by the register block above, so
    # ``index_path`` will pass its own validation).
    indexed: dict[str, object] | None = None
    if auto_index:
        try:
            stats = await index_engine.index_path(resolved, recursive=True, force=False)
            indexed = {
                "total_files": stats.total_files,
                "total_chunks": stats.total_chunks,
                "indexed_chunks": stats.indexed_chunks,
                "skipped_chunks": stats.skipped_chunks,
                "deleted_chunks": stats.deleted_chunks,
                "duration_ms": stats.duration_ms,
                "errors": list(stats.errors) if stats.errors else [],
            }
        except Exception as err:  # pragma: no cover — surface partial result
            indexed = {"error": str(err)}

    return {
        "ok": True,
        "message": message,
        "memory_dirs": memory_dirs_snapshot,
        "kind": kind,
        "indexed": indexed,
    }


@router.post("/memory-dirs/remove")
async def remove_memory_dir(
    request: Request,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
):
    """Remove a directory from ``memory_dirs``, optionally deleting its chunks.

    Body: ``{path: str, delete_chunks?: bool}``. ``delete_chunks=False`` (the
    default) is the safe behaviour — only the registration is removed,
    indexed chunks stay searchable. ``delete_chunks=True`` additionally
    drops every chunk whose ``source_file`` is under the resolved dir
    prefix; the underlying files on disk are never touched. The Web UI's
    delete confirm shows a checkbox so the user opts in explicitly.
    """
    body = await request.json()
    dir_path = body.get("path", "").strip()
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")
    delete_chunks = bool(body.get("delete_chunks", False))

    resolved = Path(dir_path).expanduser().resolve()
    resolved_norm = norm_path(resolved)

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                _hot_reload.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                config = request.app.state.config

                new_dirs = [
                    p
                    for p in config.indexing.memory_dirs
                    if norm_path(Path(p).expanduser()) != resolved_norm
                ]
                if len(new_dirs) == len(config.indexing.memory_dirs):
                    raise HTTPException(status_code=404, detail="Directory not in memory_dirs")
                if len(new_dirs) == 0:
                    raise HTTPException(status_code=400, detail="Cannot remove last memory_dir")

                config.indexing.memory_dirs = new_dirs
                save_config_overrides(config)
                _hot_reload.commit_writer_signature(request.app)

                # Chunk cleanup happens after the registration is removed
                # so a partial failure never leaves chunks orphaned with
                # the dir still registered. ``delete_by_source`` cascades
                # to ``chunks_fts`` / ``chunks_vec`` / ``chunk_links`` via
                # the schema's ``ON DELETE CASCADE``.
                deleted_chunks = 0
                if delete_chunks:
                    rows = await storage.get_source_files_with_counts()
                    prefix = resolved_norm
                    if not prefix.endswith("/"):
                        prefix = prefix + "/"
                    for row in rows:
                        source_path = row[0]
                        if norm_path(source_path).startswith(prefix):
                            deleted_chunks += await storage.delete_by_source(source_path)

                return {
                    "ok": True,
                    "message": f"Removed {resolved}",
                    "memory_dirs": [
                        str(Path(p).expanduser().resolve()) for p in config.indexing.memory_dirs
                    ],
                    "deleted_chunks": deleted_chunks,
                }
    except TimeoutError:
        raise HTTPException(503, "memory-dirs/remove timed out — another update may be in progress")


def _open_in_file_manager(path: Path) -> None:
    """Spawn the platform's default file manager to reveal ``path``.

    On macOS / Linux this uses ``subprocess.run`` with stderr captured
    and a 5-second timeout so a non-zero exit (or a child that prints to
    stderr but technically succeeds) surfaces as an explicit error
    instead of a silent "Popen succeeded but Finder never opened"
    failure mode. The launcher itself returns immediately once the
    target app has been told to open — we're not waiting for the user
    to close Finder.

    Raises ``OSError`` for missing helpers (``xdg-open`` not installed,
    etc.), non-zero exit status, or timeout. The route handler maps
    these to a 500 with the captured stderr.
    """
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
    elif sys.platform == "win32":
        # ``os.startfile`` is Windows-only and is the canonical way to
        # open a path with the default associated application. It
        # returns immediately and doesn't expose a return code; trust it.
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    else:
        # Linux/BSD/etc. — ``xdg-open`` is the desktop-agnostic choice;
        # falls through to the user's configured file manager.
        cmd = ["xdg-open", str(path)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OSError(f"{cmd[0]} not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{cmd[0]} timed out after 5s") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "no stderr"
        raise OSError(f"{cmd[0]} exited {result.returncode}: {stderr}")


@router.post("/memory-dirs/open")
async def open_memory_dir(request: Request, config=Depends(get_config)):
    """Reveal a registered ``memory_dir`` in the OS file manager.

    Body: ``{path: str}``. The path must already be in
    ``config.indexing.memory_dirs`` — arbitrary filesystem paths cannot
    be opened through this endpoint, since ``mm web`` is a local tool
    but defense-in-depth keeps the route useful even if the bind host
    were ever changed away from ``127.0.0.1``. Missing dirs return 404
    rather than spawning a file-manager pointed at nothing.
    """
    body = await request.json()
    dir_path = body.get("path", "").strip()
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")

    resolved = Path(dir_path).expanduser().resolve()
    resolved_norm = norm_path(resolved)

    in_list = any(
        norm_path(Path(p).expanduser()) == resolved_norm for p in config.indexing.memory_dirs
    )
    if not in_list:
        raise HTTPException(status_code=404, detail="Directory not in memory_dirs")
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Directory does not exist on disk")

    try:
        _open_in_file_manager(resolved)
    except OSError as exc:
        # Log with the resolved path so the server log gives the user a
        # full repro line. The toast only sees the message; the log gets
        # the path too in case it's a path-related failure (NFC vs NFD,
        # special chars, etc.).
        logger.warning("memory-dirs/open failed for %s: %s", resolved, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"ok": True, "path": str(resolved)}


@router.get("/memory-dirs/status")
async def memory_dirs_status(
    config=Depends(get_config),
    storage=Depends(get_storage),
):
    """Per-dir index status for the web widget.

    Drives the "(N chunks)" / "(not indexed)" badges — users pick which
    dirs need a manual reindex instead of paying a blind startup scan
    cost across every provider memory dir.
    """
    from memtomem.indexing.engine import memory_dir_stats

    stats = await memory_dir_stats(
        storage,
        config.indexing.memory_dirs,
        supported_extensions=config.indexing.supported_extensions,
    )
    return {"dirs": stats}


@router.post("/reindex", dependencies=[Depends(require_configured)])
async def reindex_all(
    force: bool = False,
    config=Depends(get_config),
    index_engine=Depends(get_index_engine),
):
    """Re-index all memory_dirs."""
    results = []
    for d in config.indexing.memory_dirs:
        resolved = d.expanduser().resolve()
        if not resolved.is_dir():
            results.append({"path": str(resolved), "error": "not a directory"})
            continue
        stats = await index_engine.index_path(resolved, recursive=True, force=force)
        entry: dict = {
            "path": str(resolved),
            "total_files": stats.total_files,
            "indexed_chunks": stats.indexed_chunks,
            "skipped_chunks": stats.skipped_chunks,
            "deleted_chunks": stats.deleted_chunks,
            "duration_ms": stats.duration_ms,
        }
        if stats.errors:
            entry["errors"] = list(stats.errors)
        results.append(entry)
    all_errors = [e for r in results for e in r.get("errors", [])]
    return {"ok": len(all_errors) == 0, "results": results, "errors": all_errors}


@router.get("/embedding-status", response_model=EmbeddingStatusResponse)
async def get_embedding_status(storage=Depends(get_storage)) -> EmbeddingStatusResponse:
    stored_info = getattr(storage, "stored_embedding_info", None)
    stored_out = (
        EmbeddingConfigInfo(
            dimension=stored_info["dimension"],
            provider=stored_info["provider"],
            model=stored_info["model"],
        )
        if stored_info
        else None
    )

    mismatch = getattr(storage, "embedding_mismatch", None)
    if mismatch is None:
        return EmbeddingStatusResponse(has_mismatch=False, stored=stored_out)
    return EmbeddingStatusResponse(
        has_mismatch=True,
        dimension_mismatch=mismatch["dimension_mismatch"],
        model_mismatch=mismatch["model_mismatch"],
        stored=EmbeddingConfigInfo(**mismatch["stored"]),
        configured=EmbeddingConfigInfo(**mismatch["configured"]),
    )


@router.post(
    "/embedding-reset",
    response_model=EmbeddingResetResponse,
    dependencies=[Depends(_require_localhost)],
)
async def reset_embedding(
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> EmbeddingResetResponse:
    """Reset embedding metadata to current config. Drops all vectors."""
    await storage.reset_embedding_meta(
        dimension=config.embedding.dimension,
        provider=config.embedding.provider,
        model=config.embedding.model,
    )
    return EmbeddingResetResponse(
        ok=True,
        message="Embedding metadata reset. All indexed vectors deleted — please re-index.",
    )


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
async def get_stats(storage=Depends(get_storage)) -> StatsResponse:
    data = await storage.get_stats()
    distribution = await storage.get_chunk_size_distribution()
    return StatsResponse(
        total_chunks=data.get("total_chunks", 0),
        total_sources=data.get("total_sources", 0),
        chunk_size_distribution=distribution,
    )


@router.get("/indexing/active", dependencies=[Depends(require_configured)])
async def indexing_active(index_engine=Depends(get_index_engine)) -> JSONResponse:
    """Report whether any indexing run is in flight server-side.

    Drives cross-tab / post-reload survival of the header indicator
    introduced in #602 (umbrella #582 item 4.11). Covers ``index_path``,
    ``index_file``, and ``index_path_stream`` uniformly — the SSE stream
    path is not lock-protected, so we cannot rely on
    ``_index_lock.locked()``.

    Response shape is intentionally minimal (``{"active": bool}``) to
    match the client's single-boolean ``STATE.indexing`` model. Adding
    ``started_at`` / ``path`` / progress fields later is purely additive.

    ``Cache-Control: no-store`` mirrors ``/index/stream``: this endpoint
    is polled every few seconds while a run is in flight, and a cached
    ``{"active": false}`` from an intermediary would mask the
    false→true transition the client is waiting for.
    """
    return JSONResponse(
        {"active": index_engine.is_active},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/index/stream", dependencies=[Depends(require_configured)])
async def index_stream(
    path: str = ".",
    recursive: bool = True,
    force: bool = False,
    namespace: str | None = None,
    index_engine=Depends(get_index_engine),
    config=Depends(get_config),
) -> StreamingResponse:
    """Stream indexing progress as Server-Sent Events."""
    resolved = Path(path).expanduser().resolve()
    resolved_norm = Path(norm_path(resolved))
    memory_dirs = [Path(norm_path(Path(d).expanduser())) for d in config.indexing.memory_dirs]
    if not any(resolved_norm.is_relative_to(d) for d in memory_dirs):
        raise HTTPException(
            status_code=403,
            detail="Path is outside configured memory_dirs",
        )

    async def _generate():
        try:
            async for event in index_engine.index_path_stream(
                resolved, recursive=recursive, force=force, namespace=namespace
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            error_event = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/index", response_model=IndexResponse, dependencies=[Depends(require_configured)])
async def trigger_index(
    req: IndexRequest = IndexRequest(),
    index_engine=Depends(get_index_engine),
    config=Depends(get_config),
) -> IndexResponse:
    resolved = Path(req.path).expanduser().resolve()
    resolved_norm = Path(norm_path(resolved))
    memory_dirs = [Path(norm_path(Path(d).expanduser())) for d in config.indexing.memory_dirs]
    if not any(resolved_norm.is_relative_to(d) for d in memory_dirs):
        raise HTTPException(status_code=403, detail="Path is outside configured memory directories")
    stats = await index_engine.index_path(
        resolved,
        recursive=req.recursive,
        force=req.force,
        namespace=req.namespace,
    )
    return IndexResponse(
        total_files=stats.total_files,
        total_chunks=stats.total_chunks,
        indexed_chunks=stats.indexed_chunks,
        skipped_chunks=stats.skipped_chunks,
        deleted_chunks=stats.deleted_chunks,
        duration_ms=stats.duration_ms,
        errors=list(stats.errors) if stats.errors else [],
        resolved_namespaces=list(stats.resolved_namespaces),
    )


# Cap on files walked by the preview endpoint. Large memory_dirs (10k+
# files) would otherwise stall the synchronous focus event for seconds;
# the truncated flag lets the UI surface "scanned N+, more not shown".
_PREVIEW_FILE_CAP = 200


@router.get(
    "/index/preview-namespace",
    response_model=PreviewNamespaceResponse,
    dependencies=[Depends(require_configured)],
)
async def preview_namespace(
    path: str,
    recursive: bool = True,
    index_engine=Depends(get_index_engine),
    config=Depends(get_config),
) -> PreviewNamespaceResponse:
    """Preview which namespace(s) would be applied if ``path`` were indexed.

    Walks the same file set ``trigger_index`` would walk (via
    ``IndexEngine.discover_indexable_files``) and returns the distinct
    namespaces ``_resolve_namespace`` produces with no explicit override.
    Capped at ``_PREVIEW_FILE_CAP`` files to keep focus-event latency
    bounded; ``truncated=True`` flags when the cap was hit.
    """
    resolved = Path(path).expanduser().resolve()
    resolved_norm = Path(norm_path(resolved))
    memory_dirs = [Path(norm_path(Path(d).expanduser())) for d in config.indexing.memory_dirs]
    if not any(resolved_norm.is_relative_to(d) for d in memory_dirs):
        # 403 (not 422) — same trust gate as POST /index. Out-of-memory_dirs
        # is a security boundary (read access to arbitrary paths), not a
        # parse error. Mirror trigger_index for parity.
        raise HTTPException(status_code=403, detail="Path is outside configured memory directories")

    files = index_engine.discover_indexable_files(resolved, recursive)
    truncated = len(files) > _PREVIEW_FILE_CAP
    walked = files[:_PREVIEW_FILE_CAP]
    return PreviewNamespaceResponse(
        resolved_namespaces=index_engine.resolve_namespaces_for(walked),
        truncated=truncated,
        scanned_files=len(walked),
    )


_ALLOWED_UPLOAD_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}


@router.post("/upload", response_model=UploadResponse, dependencies=[Depends(require_configured)])
async def upload_files(
    files: list[UploadFile] = File(...),
    index_engine=Depends(get_index_engine),
) -> UploadResponse:
    """Upload one or more files, save to ~/.memtomem/uploads/, and index them."""
    _MAX_UPLOAD_BYTES = 100 * 1024 * 1024

    upload_dir = Path("~/.memtomem/uploads").expanduser()
    upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    results: list[UploadFileResult] = []
    for file in files:
        fname = Path(file.filename or "upload").name
        if Path(fname).suffix.lower() not in _ALLOWED_UPLOAD_EXTS:
            results.append(
                UploadFileResult(
                    filename=fname,
                    indexed_chunks=0,
                    error=f"Unsupported type: {Path(fname).suffix}",
                )
            )
            continue
        dest = upload_dir / fname
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            dest = upload_dir / f"{stem}_{dest.stat().st_mtime_ns}{suffix}"
        try:
            content = await file.read()
            if len(content) > _MAX_UPLOAD_BYTES:
                results.append(
                    UploadFileResult(
                        filename=fname,
                        indexed_chunks=0,
                        error=f"File too large ({len(content)} bytes, max {_MAX_UPLOAD_BYTES})",
                    )
                )
                continue
            dest.write_bytes(content)
            stats = await index_engine.index_file(dest)
            results.append(
                UploadFileResult(
                    filename=fname,
                    indexed_chunks=stats.indexed_chunks,
                    path=str(dest),
                )
            )
        except Exception as exc:
            results.append(UploadFileResult(filename=fname, indexed_chunks=0, error=str(exc)))

    return UploadResponse(
        files=results,
        total_indexed=sum(r.indexed_chunks for r in results),
    )


@router.get("/uploads/usage", response_model=UploadUsageResponse)
async def uploads_usage() -> UploadUsageResponse:
    """Cumulative disk footprint of files saved via /api/upload.

    Read-only directory stat; intentionally **no** ``require_configured``
    gate so the panel surfaces the empty state on a fresh install before
    the user finishes the config wizard.
    """
    upload_dir = Path("~/.memtomem/uploads").expanduser()
    if not upload_dir.is_dir():
        return UploadUsageResponse(file_count=0, total_bytes=0, oldest_mtime=None)
    file_count = 0
    total_bytes = 0
    oldest: float | None = None
    for entry in upload_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += st.st_size
        if oldest is None or st.st_mtime < oldest:
            oldest = st.st_mtime
    return UploadUsageResponse(file_count=file_count, total_bytes=total_bytes, oldest_mtime=oldest)


@router.post("/add", response_model=AddMemoryResponse, dependencies=[Depends(require_configured)])
async def add_memory(
    req: AddMemoryRequest,
    index_engine=Depends(get_index_engine),
    storage=Depends(get_storage),
) -> AddMemoryResponse:
    from datetime import datetime, timezone

    if req.file:
        raw = req.file
        if raw.startswith("/") or raw.startswith("\\") or ".." in raw:
            raise HTTPException(
                status_code=422,
                detail="File path must be relative and must not contain '..'",
            )
        base = Path("~/.memtomem/memories").expanduser().resolve()
        target = (base / raw).resolve()
        if not str(target).startswith(str(base)):
            raise HTTPException(
                status_code=422,
                detail="File path must be relative and must not contain '..'",
            )
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        base = Path("~/.memtomem/memories").expanduser().resolve()
        target = (base / f"{date_str}.md").resolve()

    target.parent.mkdir(parents=True, exist_ok=True)
    tags = req.tags or []
    append_entry(target, req.content, title=req.title, tags=tags)
    stats = await index_engine.index_file(target, namespace=req.namespace)

    # Apply tags to indexed chunks (the chunker doesn't parse tag text from content)
    if tags and stats.indexed_chunks > 0:
        chunks = await storage.list_chunks_by_source(target)
        updated = []
        for c in chunks:
            merged = set(c.metadata.tags) | set(tags)
            if merged != set(c.metadata.tags):
                c.metadata = c.metadata.__class__(
                    **{
                        **{f: getattr(c.metadata, f) for f in c.metadata.__dataclass_fields__},
                        "tags": tuple(sorted(merged)),
                    }
                )
                updated.append(c)
        if updated:
            await storage.upsert_chunks(updated)

    return AddMemoryResponse(file=str(target), indexed_chunks=stats.indexed_chunks)
