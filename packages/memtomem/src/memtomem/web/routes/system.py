"""Stats, indexing, and memory-add endpoints.

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

import asyncio as _asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from memtomem.config import (
    FIELD_CONSTRAINTS,
    MUTABLE_FIELDS,
    RerankConfig,
    classify_scope,
    build_comparand,
    coerce_and_validate,
    memory_dir_kind,
    save_config_overrides,
)
from memtomem.search.reranker.factory import create_reranker
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools.memory_writer import append_entry
from memtomem.web import hot_reload as _hot_reload
from memtomem.web.deps import (
    get_config,
    get_embedder,
    get_index_engine,
    get_project_root,
    get_search_pipeline,
    get_storage,
    require_configured,
)
from memtomem.web.routes._errors import _redact_message
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
    ConfigRerankOut,
    ConfigResponse,
    ConfigSearchOut,
    ConfigStorageOut,
    EmbeddingConfigInfo,
    EmbeddingCoverage,
    EmbeddingResetResponse,
    EmbeddingStatusResponse,
    ModelComponent,
    ModelComponentState,
    ModelReadinessResponse,
    PrivacyPatternEntry,
    PrivacyPatternsResponse,
    PrivacyStatsResponse,
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


async def _validate_reranker_ready(reranker: object | None) -> None:
    """Force lazy local rerankers to load before enabling them at runtime.

    Reaches into the reranker's private ``_get_model`` to drive the lazy
    load eagerly, so a missing dependency (e.g. ``fastembed`` not
    installed) surfaces as a clean rejection rather than a 500 at first
    search.
    """
    if reranker is None:
        return

    load_model = getattr(reranker, "_get_model", None)
    if callable(load_model):
        await _asyncio.to_thread(load_model)


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
    project_context_root = None
    db_path = None
    mismatch = False
    if config is not None:
        memory_dirs = [str(Path(p).expanduser().resolve()) for p in config.indexing.memory_dirs]
        project_memory_dirs = [
            str(Path(p).expanduser().resolve()) for p in config.indexing.project_memory_dirs
        ]
        db_path = str(Path(config.storage.sqlite_path).expanduser().resolve())
        from memtomem.server.tools.search import _resolve_project_context_from_dirs

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
            threads=cfg.embedding.threads,
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
        rerank=ConfigRerankOut(
            enabled=cfg.rerank.enabled,
            provider=cfg.rerank.provider,
            model=cfg.rerank.model,
            oversample=cfg.rerank.oversample,
            min_pool=cfg.rerank.min_pool,
            max_pool=cfg.rerank.max_pool,
        ),
        namespace=ConfigNamespaceOut(
            default_namespace=cfg.namespace.default_namespace,
            enable_auto_ns=cfg.namespace.enable_auto_ns,
        ),
        config_mtime_ns=mtime_ns,
        config_reload_error=reload_error,
    )


@router.get("/config", response_model=ConfigResponse)
async def get_config_endpoint(
    request: Request,
    config=Depends(get_config),
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
) -> ConfigResponse:
    # Read-through reload is opportunistic and lock-free: if a write is in
    # flight, the writer will serve the fresh view on its own return. This
    # keeps the common GET path cheap while still catching CLI-side edits.
    app = request.app
    try:
        await _hot_reload.reload_if_stale(
            app,
            storage=storage,
            search_pipeline=search_pipeline,
        )
    except Exception:
        logger.warning("reload_if_stale raised unexpectedly during GET /config", exc_info=True)

    cfg = app.state.config if getattr(app.state, "config", None) is not None else config
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


@router.get("/privacy/stats", response_model=PrivacyStatsResponse)
async def get_privacy_stats() -> PrivacyStatsResponse:
    """Return the process-lifetime redaction counters (``privacy.snapshot()``).

    The GUI view (Settings → Redaction) of the same tally the
    ``mem_add_redaction_stats`` MCP tool surfaces — how many writes the
    secret-redaction gate passed, blocked, or bypassed (``force_unsafe``),
    broken down per write surface. ADR-0006 Axis E.1 audit surface.

    Read-only metadata; no ``require_configured`` gate (mirrors
    ``/api/privacy/patterns``). Counters are in-memory and reset on restart.
    """
    from memtomem import privacy

    return PrivacyStatsResponse(**privacy.snapshot())


# ---------------------------------------------------------------------------
# PATCH /api/config — runtime configuration update
# ---------------------------------------------------------------------------


# _MUTABLE_FIELDS, _FIELD_CONSTRAINTS, _coerce_and_validate are imported
# from memtomem.config (canonical single source of truth).


_RERANK_PATCH_FIELDS = (
    "enabled",
    "provider",
    "model",
    "api_key",
    "oversample",
    "min_pool",
    "max_pool",
)


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
    rerank_changed = False
    # Deferred live-fanout state (#1567): the reranker swap and tokenizer/FTS
    # rebuild are held back until AFTER a successful persist so a persist
    # timeout/validation failure reverts config and never leaves the live
    # pipeline on a value that was rejected (the old reranker is close()'d
    # during install and can't be restored). pending_reranker may be None on a
    # disable — rerank_changed is the gate, not the reranker's presence.
    pending_reranker: object | None = None
    pending_rerank_config: object | None = None

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                # Re-read from disk before merging so a concurrent CLI edit
                # is preserved. If disk is broken, refuse rather than
                # overwrite it.
                await _hot_reload.reload_if_stale(
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

                    if section_name == "rerank":
                        candidate_values: dict[str, object] = {}
                        pending_changes: list[tuple[str, object, object]] = []

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
                            candidate_values[key] = coerced
                            pending_changes.append((key, old_val, coerced))

                        if not pending_changes:
                            continue

                        candidate_data = {
                            key: getattr(section_obj, key)
                            for key in _RERANK_PATCH_FIELDS
                            if hasattr(section_obj, key)
                        }
                        candidate_data.update(candidate_values)
                        try:
                            candidate_rerank = RerankConfig(**candidate_data)
                        except ValueError as e:
                            rejected.append(f"rerank: {e}")
                            continue

                        new_reranker = None
                        try:
                            new_reranker = create_reranker(candidate_rerank)
                            await _validate_reranker_ready(new_reranker)
                        except Exception as e:
                            if new_reranker is not None:
                                await _hot_reload._close_reranker_safely(new_reranker)
                            rejected.append(f"rerank.enabled: {e}")
                            continue

                        # Record the config mutation now (so it persists) but
                        # DEFER the live pipeline swap until after a successful
                        # persist (see the fanout block below). Validating the
                        # reranker here still rejects a broken config with 400
                        # before any write.
                        config.rerank = candidate_rerank
                        rerank_changed = True
                        pending_reranker = new_reranker
                        pending_rerank_config = (
                            candidate_rerank if candidate_rerank.enabled else None
                        )

                        for key, old_val, coerced in pending_changes:
                            old_show = str(old_val)
                            new_show = str(coerced)
                            if "api_key" in key or "secret_key" in key:
                                old_show = "***" if old_val else ""
                                new_show = "***" if coerced else ""
                            applied.append(
                                ConfigPatchChange(
                                    field=f"{section_name}.{key}",
                                    old_value=old_show,
                                    new_value=new_show,
                                )
                            )
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

                        old_show = str(old_val)
                        new_show = str(coerced)
                        if "api_key" in key or "secret_key" in key:
                            old_show = "***" if old_val else ""
                            new_show = "***" if coerced else ""

                        applied.append(
                            ConfigPatchChange(
                                field=full_key,
                                old_value=old_show,
                                new_value=new_show,
                            )
                        )

                # Persist BEFORE any live fanout so a failed save (validation or
                # a cross-process lock timeout, #1567) reverts config and skips
                # the tokenizer/FTS/reranker fanout entirely — matching the
                # mem_config and CLI ``config set`` ordering. Otherwise a 400/503
                # would leave the live tokenizer or reranker on the rejected
                # value (and the old reranker already close()'d, unrecoverable).
                if persist:
                    try:
                        save_config_overrides(config)
                    except (ValueError, TimeoutError) as e:
                        request.app.state.config = _hot_reload._build_fresh_config()
                        _hot_reload._set_last_signature(
                            request.app, _hot_reload.current_signature()
                        )
                        # Discard the validated-but-uninstalled reranker.
                        if pending_reranker is not None:
                            await _hot_reload._close_reranker_safely(pending_reranker)
                        if isinstance(e, TimeoutError):
                            raise HTTPException(
                                503,
                                "Config update timed out — another update may be in progress",
                            )
                        raise HTTPException(400, detail=str(e))
                    # Self-write mtime bump — otherwise the next GET sees
                    # our own edit as "external" and reloads spuriously.
                    _hot_reload.commit_writer_signature(request.app)

                # Runtime fanout — applied only after a successful persist (or
                # immediately when persist=False). Reranker sync is inline here
                # (not via ``apply_runtime_config_changes``) because this route
                # validates + eagerly loads the reranker itself before
                # persisting. ``rerank_changed`` (not the reranker's presence)
                # is the gate so a disable — where pending_reranker is None —
                # still retires the old one and clears the pipeline.
                # ``swap_reranker`` owns the publish-first + deferred-close
                # contract (#1777), shared with ``_sync_reranker`` in
                # hot_reload.py: the new generation lands before any await, and
                # the old instance is closed only once no in-flight search
                # leases it.
                if rerank_changed:
                    await search_pipeline.swap_reranker(pending_reranker, pending_rerank_config)

                if tokenizer_changed:
                    from memtomem.storage.fts_tokenizer import set_tokenizer

                    set_tokenizer(config.search.tokenizer)
                    count = await storage.rebuild_fts()
                    logger.info(
                        "FTS index rebuilt with tokenizer=%s (%d chunks)",
                        config.search.tokenizer,
                        count,
                    )

                if applied or rerank_changed:
                    search_pipeline.invalidate_cache()
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
                await _hot_reload.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                try:
                    save_config_overrides(request.app.state.config)
                except ValueError as e:
                    request.app.state.config = _hot_reload._build_fresh_config()
                    _hot_reload._set_last_signature(request.app, _hot_reload.current_signature())
                    raise HTTPException(400, detail=str(e))
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
        force_unsafe (bool, default False): ADR-0006 Axis E.1 — bypass the
            secret-redaction gate during the ``auto_index`` scan
            (audit-logged inside the engine). No effect when
            ``auto_index=false``. Does **not** override the
            ``project_shared`` hard-refusal (ADR-0011 §5).
    """
    body = await request.json()
    dir_path = body.get("path", "").strip()
    auto_index = bool(body.get("auto_index", True))
    # Strict: only a literal JSON ``true`` bypasses the redaction gate. A plain
    # ``bool(...)`` would make the string ``"false"`` (and any non-empty string)
    # truthy — a silent secret-bypass footgun on a security override.
    force_unsafe = body.get("force_unsafe", False) is True
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")

    resolved = Path(dir_path).expanduser().resolve()
    if not resolved.is_dir():
        resolved.mkdir(parents=True, exist_ok=True)

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                await _hot_reload.reload_if_stale(
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
                    try:
                        save_config_overrides(config)
                    except TimeoutError:
                        # Cross-process lock timeout (#1567): the append was not
                        # persisted, so revert runtime to disk state instead of
                        # keeping an unpersisted dir the 503 claims we didn't add.
                        request.app.state.config = _hot_reload._build_fresh_config()
                        _hot_reload._set_last_signature(
                            request.app, _hot_reload.current_signature()
                        )
                        raise HTTPException(
                            503, "memory-dirs/add timed out — another update may be in progress"
                        )
                    _hot_reload.commit_writer_signature(request.app)

                memory_dirs_snapshot = [
                    str(Path(p).expanduser().resolve()) for p in config.indexing.memory_dirs
                ]
                message = "Already in memory_dirs" if already_present else f"Added {resolved}"
    except TimeoutError:
        raise HTTPException(503, "memory-dirs/add timed out — another update may be in progress")

    watcher = getattr(request.app.state, "file_watcher", None)
    if watcher is not None:
        try:
            await watcher.reconfigure(config.indexing)
        except Exception as exc:
            logger.error("Failed to activate memory directory watcher", exc_info=True)
            raise HTTPException(
                status_code=503,
                detail="Directory was registered, but watcher activation failed. Retry or restart mm web.",
            ) from exc

    # Index outside the config lock so a slow scan doesn't block other
    # config writers (the watcher invariant — path inside ``memory_dirs``
    # — is already satisfied by the register block above, so
    # ``index_path`` will pass its own validation).
    indexed: dict[str, object] | None = None
    index_status = "not_requested"
    if auto_index:
        try:
            stats = await index_engine.index_path(
                resolved, recursive=True, force=False, force_unsafe=force_unsafe
            )
            indexed = {
                "total_files": stats.total_files,
                "total_chunks": stats.total_chunks,
                "indexed_chunks": stats.indexed_chunks,
                "skipped_chunks": stats.skipped_chunks,
                "deleted_chunks": stats.deleted_chunks,
                "duration_ms": stats.duration_ms,
                "errors": list(stats.errors) if stats.errors else [],
                "blocked_files": stats.blocked_files,
                "blocked_paths": list(stats.blocked_paths),
                "blocked_project_shared_files": stats.blocked_project_shared_files,
            }
            has_issues = bool(stats.errors or stats.blocked_files)
            has_processed_chunks = (stats.indexed_chunks + stats.skipped_chunks) > 0
            if not has_issues:
                index_status = "success"
            elif has_processed_chunks:
                index_status = "partial"
            else:
                index_status = "failed"
        except Exception:  # pragma: no cover — surface partial result
            logger.exception("Initial indexing failed for newly registered memory directory")
            indexed = {"error": "Initial indexing failed"}
            index_status = "failed"

    return {
        "ok": True,
        "message": message,
        "memory_dirs": memory_dirs_snapshot,
        "kind": kind,
        "indexed": indexed,
        "index_status": index_status,
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
                await _hot_reload.reload_if_stale(
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
                try:
                    save_config_overrides(config)
                except TimeoutError:
                    # Cross-process lock timeout (#1567): the removal was not
                    # persisted, so revert runtime to disk state instead of
                    # dropping a dir the 503 claims we kept.
                    request.app.state.config = _hot_reload._build_fresh_config()
                    _hot_reload._set_last_signature(request.app, _hot_reload.current_signature())
                    raise HTTPException(
                        503, "memory-dirs/remove timed out — another update may be in progress"
                    )
                _hot_reload.commit_writer_signature(request.app)

                # Chunk cleanup happens after the registration is removed
                # so a partial failure never leaves chunks orphaned with
                # the dir still registered. ``delete_by_source`` cascades
                # to ``chunks_fts`` / ``chunks_vec`` / ``chunk_links`` via
                # the schema's ``ON DELETE CASCADE``.
                deleted_chunks = 0
                if delete_chunks:
                    from memtomem.indexing.engine import norm_dir_prefix

                    rows = await storage.get_source_files_with_counts()
                    # Use the canonical helper so the trailing-separator
                    # rule (``os.sep``, native form on Windows) stays in
                    # one place; matches the comparison done by
                    # :func:`resolve_owning_memory_dir` (#647).
                    prefix = norm_dir_prefix(resolved)
                    for row in rows:
                        source_path = row[0]
                        if norm_path(source_path).startswith(prefix):
                            deleted_chunks += await storage.delete_by_source(source_path)

                watcher = getattr(request.app.state, "file_watcher", None)
                if watcher is not None:
                    await watcher.reconfigure(config.indexing)

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
        config.indexing.all_index_roots(),
        supported_extensions=config.indexing.supported_extensions,
    )
    return {"dirs": stats}


@router.post("/reindex", dependencies=[Depends(require_configured)])
async def reindex_all(
    force: bool = False,
    config=Depends(get_config),
    index_engine=Depends(get_index_engine),
):
    """Re-index every registered index root (user-tier + project-tier per ADR-0011)."""
    results = []
    for d in config.indexing.all_index_roots():
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
            "blocked_files": stats.blocked_files,
            "blocked_project_shared_files": stats.blocked_project_shared_files,
        }
        if stats.blocked_files:
            entry["blocked_paths"] = list(stats.blocked_paths)
        if stats.errors:
            entry["errors"] = list(stats.errors)
        results.append(entry)
    all_errors = [e for r in results for e in r.get("errors", [])]
    total_blocked = 0
    total_blocked_ps = 0
    for r in results:
        b = r.get("blocked_files", 0)
        total_blocked += b if isinstance(b, int) else 0
        bps = r.get("blocked_project_shared_files", 0)
        total_blocked_ps += bps if isinstance(bps, int) else 0
    return {
        "ok": len(all_errors) == 0,
        "results": results,
        "errors": all_errors,
        "blocked_files": total_blocked,
        "blocked_project_shared_files": total_blocked_ps,
    }


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

    # Dense-vector coverage so the UI can flag "BM25-only" runs without
    # forcing the user to peek at sqlite. ``get_dense_coverage`` falls
    # back to ``with_dense=0`` when the vec virtual table is absent, so
    # we always have a numeric pair to render. Storage backends that
    # don't implement the method (e.g. mocked test doubles) leave the
    # field at ``None`` rather than 500ing this endpoint — the warning
    # banner the field feeds is informational, not load-bearing.
    coverage_out: EmbeddingCoverage | None = None
    if hasattr(storage, "get_dense_coverage"):
        try:
            cov = await storage.get_dense_coverage()
            total = int(cov["total"])
            with_dense = int(cov["with_dense"])
            pct = round((with_dense / total) * 100, 1) if total > 0 else 0.0
            coverage_out = EmbeddingCoverage(total=total, with_dense=with_dense, percent=pct)
        except Exception:
            logger.debug("dense coverage query failed", exc_info=True)

    mismatch = getattr(storage, "embedding_mismatch", None)
    if mismatch is None:
        return EmbeddingStatusResponse(has_mismatch=False, stored=stored_out, coverage=coverage_out)
    return EmbeddingStatusResponse(
        has_mismatch=True,
        dimension_mismatch=mismatch["dimension_mismatch"],
        model_mismatch=mismatch["model_mismatch"],
        stored=EmbeddingConfigInfo(**mismatch["stored"]),
        configured=EmbeddingConfigInfo(**mismatch["configured"]),
        coverage=coverage_out,
    )


# Providers that route through the lazy fastembed loaders we instrumented
# for #696 — i.e. ones where introspecting ``_model`` / ``_loading`` /
# ``_load_error`` is meaningful. The embedder advertises this path as
# ``"onnx"``; the reranker advertises it as ``"fastembed"``. Other
# providers (Ollama/Cohere/local) have their own connection-based
# readiness model and are reported as ``state="skipped"``.
_INTROSPECTABLE_PROVIDERS = {"onnx", "fastembed"}


def _component_for(
    *,
    provider: str,
    model: str | None,
    holder: object | None,
    enabled: bool,
) -> ModelComponent:
    """Build a ``ModelComponent`` snapshot for one lazy loader.

    ``holder`` is either an ``OnnxEmbedder`` / ``FastEmbedReranker`` (with
    ``_model``, ``_loading``, ``_load_error`` flags introduced for #696)
    or ``None`` when the component is disabled. ``enabled=False`` short-
    circuits to ``state="skipped"`` regardless of the holder.
    """
    from memtomem.embedding.aliases import approx_size_mb, resolve_embedder_id
    from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir
    from memtomem.embedding.readiness import model_snapshot_present

    if not enabled or provider not in _INTROSPECTABLE_PROVIDERS or holder is None:
        return ModelComponent(state="skipped", provider=provider, model=model)

    # Embedder config stores either a short alias (``bge-m3``) or the
    # raw fastembed id; the reranker config always stores the full
    # fastembed id directly. ``resolve_embedder_id`` is a no-op for
    # already-resolved ids, so it's safe to apply uniformly.
    fastembed_id = resolve_embedder_id(model) if model else None
    cache_present = False
    if fastembed_id:
        try:
            cache_dir = resolve_fastembed_cache_dir()
            cache_present = model_snapshot_present(cache_dir, fastembed_id)
        except Exception:
            # Resolving the cache dir or stat'ing it should never raise in
            # practice; fall through to ``cache_present=False`` so the
            # endpoint still returns a meaningful state.
            logger.debug("model_snapshot_present probe failed", exc_info=True)

    loaded = getattr(holder, "_model", None) is not None
    loading = bool(getattr(holder, "_loading", False))
    load_error = getattr(holder, "_load_error", None)

    if loaded:
        # A lazy loader can keep a stale ``_load_error`` from an earlier
        # failed attempt even after a later search successfully constructs
        # the model. Loaded-in-memory is the authoritative terminal state.
        state: ModelComponentState = "ready"
        load_error = None
    elif load_error:
        state = "error"
    elif loading:
        state = "downloading" if not cache_present else "loading"
    else:
        state = "cold"

    return ModelComponent(
        state=state,
        provider=provider,
        model=model,
        cache_present=cache_present,
        approx_size_mb=approx_size_mb(fastembed_id) if fastembed_id else None,
        error=load_error,
    )


@router.get("/system/model-readiness", response_model=ModelReadinessResponse)
async def get_model_readiness(
    request: Request,
    embedder=Depends(get_embedder),
    config=Depends(get_config),
    pipeline=Depends(get_search_pipeline),
) -> ModelReadinessResponse:
    """Snapshot the load state of the embedder + reranker (issue #696).

    Read-only: inspects the lazy loaders' ``_model`` / ``_loading`` /
    ``_load_error`` flags and probes the fastembed cache directory. Never
    triggers a model download itself — the Web UI banner polls this while
    waiting for ``state="ready"`` so the user sees what's happening
    instead of a frozen Search button.
    """
    emb_cfg = config.embedding
    embedder_component = _component_for(
        provider=emb_cfg.provider,
        model=emb_cfg.model,
        holder=embedder,
        enabled=True,
    )

    # Reranker: lives on the SearchPipeline. ``app.state.search_pipeline``
    # always exists (lifespan creates it), but ``_reranker`` is None when
    # ``config.rerank.enabled is False`` — skip the readiness check then.
    rerank_cfg = config.rerank
    reranker_holder = getattr(pipeline, "_reranker", None) if pipeline else None
    reranker_component = _component_for(
        provider=rerank_cfg.provider,
        model=rerank_cfg.model if rerank_cfg.enabled else None,
        holder=reranker_holder,
        enabled=rerank_cfg.enabled,
    )

    return ModelReadinessResponse(
        embedder=embedder_component,
        reranker=reranker_component,
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
    indexed_dirs = sorted(
        (
            (norm_dir_prefix(d), str(Path(d).expanduser().resolve()), memory_dir_kind(d))
            for d in config.indexing.memory_dirs
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


@router.post("/index/stream", dependencies=[Depends(require_configured)])
async def index_stream(
    req: IndexRequest,
    index_engine=Depends(get_index_engine),
) -> StreamingResponse:
    """Stream CSRF-protected indexing progress as Server-Sent Events."""
    resolved = Path(req.path).expanduser().resolve()

    async def _generate():
        try:
            async for event in index_engine.index_path_stream(
                resolved,
                recursive=req.recursive,
                force=req.force,
                namespace=req.namespace,
                force_unsafe=req.force_unsafe,
                path_scope="explicit",
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            # Engine-level failures escape to this handler (per-file errors
            # are caught inside the engine and reported as basenames in the
            # "complete" event). A raw ``str(exc)`` here can embed absolute
            # paths — leaking ``$HOME``/username — or secret-shaped fragments
            # to the client, so route it through the same redactor every
            # other error surface at this trust boundary uses.
            error_event = {"type": "error", "message": _redact_message(str(exc))}
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


@router.get("/index/stream")
async def index_stream_get_disabled() -> None:
    raise HTTPException(status_code=405, detail="Use POST /api/index/stream")


@router.post("/index", response_model=IndexResponse, dependencies=[Depends(require_configured)])
async def trigger_index(
    req: IndexRequest = IndexRequest(),
    index_engine=Depends(get_index_engine),
) -> IndexResponse:
    resolved = Path(req.path).expanduser().resolve()
    stats = await index_engine.index_path(
        resolved,
        recursive=req.recursive,
        force=req.force,
        force_unsafe=req.force_unsafe,
        namespace=req.namespace,
        path_scope="explicit",
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
        blocked_files=stats.blocked_files,
        blocked_paths=list(stats.blocked_paths),
        blocked_project_shared_files=stats.blocked_project_shared_files,
    )


# Cap on files walked by the preview endpoint. Large memory_dirs (10k+
# files) would otherwise stall the synchronous focus event for seconds;
# the truncated flag lets the UI surface "scanned N+, more not shown".
_PREVIEW_FILE_CAP = 200


@router.post(
    "/index/preview-namespace",
    response_model=PreviewNamespaceResponse,
    dependencies=[Depends(require_configured)],
)
async def preview_namespace(
    body: PreviewNamespaceRequest,
    index_engine=Depends(get_index_engine),
) -> PreviewNamespaceResponse:
    """Preview which namespace(s) would be applied if ``path`` were indexed.

    Walks the same file set ``trigger_index`` would walk (via
    ``IndexEngine.discover_indexable_files``) and returns the distinct
    namespaces ``_resolve_namespace`` produces with no explicit override.
    Capped at ``_PREVIEW_FILE_CAP`` files to keep focus-event latency
    bounded; ``truncated=True`` flags when the cap was hit.
    """
    resolved = Path(body.path).expanduser().resolve()
    files = index_engine.discover_indexable_files(resolved, body.recursive, path_scope="explicit")
    truncated = len(files) > _PREVIEW_FILE_CAP
    walked = files[:_PREVIEW_FILE_CAP]
    return PreviewNamespaceResponse(
        resolved_namespaces=index_engine.resolve_namespaces_for(walked),
        truncated=truncated,
        scanned_files=len(walked),
    )


_ALLOWED_UPLOAD_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}


@router.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(require_configured)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            }
                        },
                    }
                }
            },
        }
    },
)
async def upload_files(
    request: Request,
    force_unsafe: bool = False,
    index_engine=Depends(get_index_engine),
) -> UploadResponse:
    """Upload one or more files, save to ~/.memtomem/uploads/, and index them.

    Multipart parts first stream into an owner-only temporary disk quarantine.
    Each file's text content then passes through the trust-boundary redaction
    guard before durable promotion into ``uploads/``. A flagged file is
    rejected (``error="redaction_blocked"``) and only the quarantine copy
    exists until context cleanup; ``force_unsafe=True`` (query param) bypasses
    for the whole batch with audit logging.
    """
    from memtomem import privacy
    from memtomem.web.upload_quarantine import (
        UploadQuarantineError,
        QuarantinedUpload,
        promote_no_overwrite,
        quarantine_uploads,
    )

    upload_dir = Path("~/.memtomem/uploads").expanduser()

    try:
        async with quarantine_uploads(request, upload_dir) as quarantined:
            decisions: list[tuple[QuarantinedUpload, str, bool, str | None]] = []
            for item in quarantined:
                fname = item.filename
                suffix = Path(fname).suffix.lower()
                if suffix not in _ALLOWED_UPLOAD_EXTS:
                    decisions.append((item, fname, False, f"Unsupported type: {suffix}"))
                    continue
                try:
                    content = await _asyncio.to_thread(item.path.read_bytes)
                    text = content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    decisions.append((item, fname, False, f"Decode failed: {exc}"))
                    continue
                except Exception:
                    logger.exception("Upload adjudication failed for %s", fname)
                    decisions.append((item, fname, False, "Upload processing failed"))
                    continue

                try:
                    guard = privacy.enforce_write_guard(
                        text,
                        surface="web_api_upload",
                        force_unsafe=force_unsafe,
                        audit_context={"filename": fname},
                    )
                except Exception:
                    logger.exception("Upload privacy check failed for %s", fname)
                    decisions.append((item, fname, False, "Upload processing failed"))
                    continue
                if guard.decision == "blocked":
                    decisions.append(
                        (item, fname, False, f"redaction_blocked (hits={len(guard.hits)})")
                    )
                else:
                    decisions.append((item, fname, True, None))

            results: list[UploadFileResult] = []
            for item, fname, accepted, error in decisions:
                if not accepted:
                    results.append(UploadFileResult(filename=fname, indexed_chunks=0, error=error))
                    continue
                dest: Path | None = None
                try:
                    dest = promote_no_overwrite(item.path, upload_dir, fname)
                    stats = await index_engine.index_file(dest, already_scanned=True)
                    results.append(
                        UploadFileResult(
                            filename=fname,
                            indexed_chunks=stats.indexed_chunks,
                            path=str(dest),
                        )
                    )
                except Exception:
                    logger.exception("Upload processing failed for %s", fname)
                    results.append(
                        UploadFileResult(
                            filename=fname,
                            indexed_chunks=0,
                            path=str(dest) if dest is not None and dest.exists() else None,
                            error="Upload processing failed",
                        )
                    )

            return UploadResponse(
                files=results,
                total_indexed=sum(r.indexed_chunks for r in results),
            )
    except UploadQuarantineError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except BaseException as exc:
        if isinstance(exc, _asyncio.CancelledError):
            raise
        logger.exception("Upload batch processing failed")
        raise HTTPException(status_code=500, detail="Upload processing failed") from exc


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
    request: Request,
    index_engine=Depends(get_index_engine),
    storage=Depends(get_storage),
    config=Depends(get_config),
    server_project_root=Depends(get_project_root),
) -> AddMemoryResponse:
    from datetime import datetime, timezone

    from memtomem import privacy

    # ADR-0011 §5 Gate B (PR-F slice 4): project_shared writes go to git;
    # require an explicit ``confirm_project_shared=true`` so the SPA
    # cannot silently commit PII to a tracked tier through a default-bool
    # oversight. Mirrors the MCP ``mem_add`` gate at
    # ``memory_crud.py:204`` and the Web parallel on the chunks DELETE
    # path at ``chunks.py:157``. project_local is NOT Gate B-gated —
    # only the canonical-residency tier choice + ADR-0011 §3's
    # zero-fan-out rule applies. The 4xx body carries the CLI hint /
    # docs link so the SPA renders "rejected, here's the equivalent
    # invocation" without rewriting the prose client-side.
    if req.scope == "project_shared" and not req.confirm_project_shared:
        logger.info(
            "web add_memory rejected project_shared write without confirmation",
            extra={"file": req.file, "namespace": req.namespace},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "blocked_project_shared",
                "surface": "web_api_add",
                "scope": req.scope,
                "message": (
                    "scope='project_shared' writes to a git-tracked directory. "
                    "Re-submit with confirm_project_shared=true to proceed."
                ),
                "cli_hint": "mm mem add --scope project_shared",
                "docs_url": (
                    "https://github.com/memtomem/memtomem/blob/main/docs/adr/"
                    "0011-canonical-artifact-scope-hierarchy.md"
                ),
            },
        )

    # Trust-boundary redaction guard. Mirrors MCP ``mem_add`` so the
    # same secret patterns block writes regardless of which surface
    # the agent or user came in through. ``force_unsafe`` is opt-in
    # via the SPA's confirm-and-retry UX after the first 403. Threading
    # ``scope`` here makes Gate A's hard-refusal of ``force_unsafe=True``
    # on project_shared writes fire on the Web path too (ADR-0011 PR-D
    # round 7 parity — same fix as the chunks PATCH path at
    # ``chunks.py:73-86``).
    guard = privacy.enforce_write_guard(
        req.content,
        surface="web_api_add",
        force_unsafe=req.force_unsafe,
        scope=req.scope,
        audit_context={
            "namespace": req.namespace,
            "file": req.file,
            "scope": req.scope,
        },
    )
    if guard.decision == "blocked":
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "redaction_blocked",
                "hits": len(guard.hits),
                "surface": "web_api_add",
            },
        )
    if guard.decision == "blocked_project_shared":
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "blocked_project_shared",
                "hits": len(guard.hits),
                "surface": "web_api_add",
                "message": (
                    "force_unsafe is not permitted on scope='project_shared' "
                    "writes (git history is forever). Re-submit with "
                    "scope='project_local' or scope='user' to bypass, or "
                    "hand-edit the canonical file."
                ),
            },
        )

    # ADR-0011 §4 PR-F slice 4: resolve the canonical-residency base
    # directory per tier. User tier stays on ``memory_dirs[0]`` for
    # write-surface parity with MCP ``mem_add`` and the CLI; an empty
    # ``memory_dirs`` refuses with ``ConfigError`` → 409 instead of
    # falling back to the historical ``~/.memtomem/memories`` — the
    # "index nothing" state must not silently write into a directory the
    # active config disabled (#1768). Project tiers route through
    # ``resolve_memory_scope_dir`` against the server's project root so
    # writes land in ``<proj>/.memtomem/memories[/.local]/`` — the same
    # path the MCP ``mem_add(scope=...)`` flow uses. Refuses unregistered
    # project-tier dirs upfront so the row's persisted scope cannot
    # diverge from what the read surface / watcher can actually see.
    if req.scope == "user":
        from memtomem.memory_scope import require_user_base

        base = require_user_base(config.indexing.memory_dirs)
    else:
        from memtomem.memory_scope import (
            MemoryScopeError,
            is_project_tier_registered,
            project_tier_registration_error,
            resolve_memory_scope_dir,
        )
        from memtomem.server.tools.search import _resolve_project_context_from_dirs

        # ADR-0011 PR-F parity with MCP ``mem_add`` (memory_crud.py:285
        # via search.py:73-96): resolve the *registered* project root
        # containing the server's cwd rather than using ``cwd`` itself.
        # ``app.state.project_root`` was set to ``Path.cwd()`` in the
        # lifespan; if the server is launched from a subdirectory of a
        # registered project, the raw cwd has no ``.memtomem/memories``
        # tree, so ``resolve_memory_scope_dir`` would land on the wrong
        # directory and ``is_project_tier_registered`` would 422 — while
        # MCP correctly writes under the project root. Falls back to
        # the lifespan cwd only if no registered tier covers the cwd
        # (the 422 below then surfaces the operator-actionable
        # "register your project_memory_dirs first" error).
        pmdirs = config.indexing.project_memory_dirs
        project_root = _resolve_project_context_from_dirs(pmdirs)
        if project_root is None:
            project_root = Path(server_project_root)
        try:
            base = resolve_memory_scope_dir(req.scope, project_root)
        except MemoryScopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not is_project_tier_registered(base, pmdirs):
            raise HTTPException(
                status_code=422,
                detail=project_tier_registration_error(base, req.scope),
            )

    if req.file:
        raw = req.file
        if raw.startswith("/") or raw.startswith("\\") or ".." in raw:
            raise HTTPException(
                status_code=422,
                detail="File path must be relative and must not contain '..'",
            )
        target = (base / raw).resolve()
        if not str(target).startswith(str(base)):
            raise HTTPException(
                status_code=422,
                detail="File path must be relative and must not contain '..'",
            )
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = (base / f"{date_str}.md").resolve()

    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        _lock_path_for,
        async_file_lock,
    )

    tags = req.tags or []
    # #1587: hold the target file's cross-process sidecar (L2) across append +
    # reindex + tag-merge so a concurrent MCP mem_edit/mem_delete rollback — in
    # this process or another — cannot erase this appended entry. ``mm web`` has
    # no AppContext L1 lock; L2's in-process guard serializes web handlers too.
    # ``lock_held=True`` skips the nested engine acquire.
    try:
        async with async_file_lock(_lock_path_for(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S):
            target.parent.mkdir(parents=True, exist_ok=True)
            # Guarded above (``enforce_write_guard``); skip the engine gate (ADR-0006 PR-A).
            await _asyncio.to_thread(append_entry, target, req.content, title=req.title, tags=tags)
            stats = await index_engine.index_file(
                target, namespace=req.namespace, already_scanned=True, lock_held=True
            )

            # Apply tags to indexed chunks (the chunker doesn't parse tag text
            # from content). Inside the lock — it upserts rows keyed to this
            # file's just-indexed chunks.
            if tags and stats.indexed_chunks > 0:
                chunks = await storage.list_chunks_by_source(target)
                updated = []
                for c in chunks:
                    merged = set(c.metadata.tags) | set(tags)
                    if merged != set(c.metadata.tags):
                        c.metadata = c.metadata.__class__(
                            **{
                                **{
                                    f: getattr(c.metadata, f)
                                    for f in c.metadata.__dataclass_fields__
                                },
                                "tags": tuple(sorted(merged)),
                            }
                        )
                        updated.append(c)
                if updated:
                    await storage.upsert_chunks(updated)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Memory file is locked by another writer; try again."
        ) from exc

    return AddMemoryResponse(file=str(target), indexed_chunks=stats.indexed_chunks)
