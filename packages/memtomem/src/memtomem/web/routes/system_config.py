"""Configuration, memory-dir management, and model/embedding readiness endpoints."""

from __future__ import annotations

import asyncio as _asyncio
import copy
import logging
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from memtomem.config import (
    FIELD_CONSTRAINTS,
    MUTABLE_FIELDS,
    RerankConfig,
    assign_section_fields,
    build_comparand,
    coerce_and_validate,
    memory_dir_kind,
    save_config_overrides,
    saved_embedding_identity,
)
from memtomem.embedding.runtime import publish_onnx_batch_size
from memtomem.errors import RetryableError
from memtomem.search.reranker.factory import create_reranker
from memtomem.storage.sqlite_helpers import fold_path_form, norm_path
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
    ConfigLoadWarningOut,
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

logger = logging.getLogger("memtomem.web.routes.system")

_LOCALHOST_ADDRS = {"127.0.0.1", "::1", "localhost"}


def _sys():
    """Return ``memtomem.web.routes.system`` for runtime monkeypatch compatibility."""
    import memtomem.web.routes.system as _sys_mod

    return _sys_mod


class _ConfigSnapshot:
    """Undo this request's config edits without replacing shared sections.

    Capture after the writer's reload, before its first mutation. Components
    can retain the section (or memory_dirs list) itself, so replacing only
    app.state.config would leave them holding the rejected values. Retain the
    original objects as well as deep copies of the fields we may change.
    """

    def __init__(self, config: Any, fields: Mapping[str, Iterable[str]]) -> None:
        self._config = config
        self._config_fields_set = copy.copy(getattr(config, "__pydantic_fields_set__", None))
        self._sections: dict[str, tuple[Any, dict[str, tuple[Any, Any]], set[str] | None]] = {}
        for name, keys in fields.items():
            section = getattr(config, name, None)
            if section is None:
                continue
            values = {}
            for key in keys:
                if hasattr(section, key):
                    original = getattr(section, key)
                    values[key] = (original, copy.deepcopy(original))
            if not values:
                # A rejected/read-only section has nothing to undo. Recording
                # it would make restore() assign even non-field attributes.
                continue
            self._sections[name] = (
                section,
                values,
                copy.copy(getattr(section, "__pydantic_fields_set__", None)),
            )

    def restore(self) -> None:
        # No await and no disk read: restoration must finish before cleanup
        # can yield or a diagnostic read can fail. Do not assign app.state
        # here; a lock-free reader may have installed a newer config meanwhile.
        for name, (section, values, fields_set) in self._sections.items():
            for key, (original, saved) in values.items():
                if isinstance(original, list):
                    original[:] = saved
                elif isinstance(original, dict):
                    original.clear()
                    original.update(saved)
                setattr(section, key, original)
            if fields_set is not None:
                section.__pydantic_fields_set__.clear()
                section.__pydantic_fields_set__.update(fields_set)
            # The rerank handler replaces a section instead of mutating it.
            setattr(self._config, name, section)
        if self._config_fields_set is not None:
            self._config.__pydantic_fields_set__.clear()
            self._config.__pydantic_fields_set__.update(self._config_fields_set)


def _check_reload_block(request: Request) -> None:
    """Reject writes while a reload error is live for the current disk state.

    Writing would call :func:`save_config_overrides`, overwriting the broken
    disk file and destroying any recovery trail. User must fix disk first
    (``mm init --fresh`` or manual edit).
    """
    hr = getattr(_sys(), "_hot_reload", _hot_reload)
    err = hr.get_reload_error(request.app)
    if err is None:
        return
    if hr.reload_error_is_stale(err):
        # Disk was fixed since the error was recorded — the override file, or a
        # ``config.d`` fragment, which moves the composite signature without
        # touching that file's mtime. Let the next reload attempt clear it.
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


# Mounted only through ``system.router``, which carries the ``system`` tag.
router = APIRouter()


def _declared_default(model: type, name: str):
    """The default ``model`` itself declares for ``name``."""
    return model.model_fields[name].default


def _cfg_value(section: object, model: type, name: str):
    """Read ``name`` off ``section``, falling back to the config model's default.

    ``cfg`` here is not always a real ``Mem2MemConfig``: partial, duck-typed
    stand-ins reach this builder too, so a plain attribute read raises on a
    field they never declared. The fallback still must not *restate* the value —
    that would put a second copy next to ``config.py`` (and a third in the
    response schema) free to drift from the one that is load-bearing. Take it
    from the field that defines it instead.
    """
    return getattr(section, name, _declared_default(model, name))


def _build_config_response(
    cfg, *, mtime_ns: int = -1, reload_error: str | None = None
) -> ConfigResponse:
    """Build ConfigResponse from a Mem2MemConfig instance."""
    from memtomem.config import EmbeddingConfig, IndexingConfig

    return ConfigResponse(
        embedding=ConfigEmbeddingOut(
            provider=cfg.embedding.provider,
            model=cfg.embedding.model,
            dimension=cfg.embedding.dimension,
            base_url=cfg.embedding.base_url,
            batch_size=cfg.embedding.batch_size,
            onnx_batch_size=cfg.embedding.onnx_batch_size,
            max_sequence_tokens=cfg.embedding.max_sequence_tokens,
            onnx_cpu_mem_arena=cfg.embedding.onnx_cpu_mem_arena,
            api_key="***" if cfg.embedding.api_key else "",
            threads=cfg.embedding.threads,
            onnx_variant=_cfg_value(cfg.embedding, EmbeddingConfig, "onnx_variant"),
            onnx_artifact_path=_cfg_value(cfg.embedding, EmbeddingConfig, "onnx_artifact_path"),
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
            hard_max_chunk_tokens=_cfg_value(cfg.indexing, IndexingConfig, "hard_max_chunk_tokens"),
            chunk_context_tokens=_cfg_value(cfg.indexing, IndexingConfig, "chunk_context_tokens"),
            chunk_model_tokens=_cfg_value(cfg.indexing, IndexingConfig, "chunk_model_tokens"),
            chunk_tokenizer_path=_cfg_value(cfg.indexing, IndexingConfig, "chunk_tokenizer_path"),
            chunk_input_prefix=_cfg_value(cfg.indexing, IndexingConfig, "chunk_input_prefix"),
            watcher_debounce_ms=_cfg_value(cfg.indexing, IndexingConfig, "watcher_debounce_ms"),
            watcher_max_wait_ms=_cfg_value(cfg.indexing, IndexingConfig, "watcher_max_wait_ms"),
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
            timeout_s=cfg.rerank.timeout_s,
        ),
        namespace=ConfigNamespaceOut(
            default_namespace=cfg.namespace.default_namespace,
            enable_auto_ns=cfg.namespace.enable_auto_ns,
        ),
        config_mtime_ns=mtime_ns,
        config_reload_error=reload_error,
        config_load_warnings=[
            ConfigLoadWarningOut(
                section=diagnostic.section,
                path=diagnostic.path,
                error=diagnostic.error,
                fix=diagnostic.as_status_warning()["fix"],
            )
            for diagnostic in getattr(cfg, "load_diagnostics", ())
        ],
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
    hr = getattr(_sys(), "_hot_reload", _hot_reload)
    try:
        await hr.reload_if_stale(
            app,
            storage=storage,
            search_pipeline=search_pipeline,
        )
    except Exception:
        logger.warning("reload_if_stale raised unexpectedly during GET /config", exc_info=True)

    cfg = app.state.config if getattr(app.state, "config", None) is not None else config
    err = hr.get_reload_error(app)
    return _build_config_response(
        cfg,
        mtime_ns=hr.get_config_mtime_ns(),
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

    Retain the selected embedding profile, including a model selected in
    config.json, but never copy its editable pins into the reset values.
    ``saved_embedding_identity`` is the same identity ``save_config_overrides``
    prunes against, resolved from the files rather than the runtime, so ↺
    offers exactly the value Save drops even when the runtime is stale.
    Read-only; no reload interaction needed.
    """
    sys_mod = _sys()
    identity_fn = getattr(sys_mod, "saved_embedding_identity", saved_embedding_identity)
    comparand_fn = getattr(sys_mod, "build_comparand", build_comparand)
    identity = identity_fn(quiet=True)
    return _build_config_response(comparand_fn(quiet=True, embedding_context=identity))


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
    # Not PATCH-mutable (absent from MUTABLE_FIELDS), but it MUST ride along
    # in the reconstruction: RerankConfig(**candidate_data) would otherwise
    # silently reset a custom timeout to the default whenever any other
    # rerank field is patched.
    "timeout_s",
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
    sys_mod = _sys()
    hr = getattr(sys_mod, "_hot_reload", _hot_reload)
    applied: list[ConfigPatchChange] = []
    rejected: list[str] = []
    tokenizer_changed = False
    onnx_batch_changed = False
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
                await hr.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                config = request.app.state.config

                updates_by_section = req.model_dump(exclude_none=True)
                snapshot = (
                    _ConfigSnapshot(
                        config,
                        {
                            name: updates.keys() & MUTABLE_FIELDS.get(name, set())
                            for name, updates in updates_by_section.items()
                            # Extra sections may be scalars or lists. Leave
                            # rejection to the loop below; snapshot only edits.
                            if name in MUTABLE_FIELDS and isinstance(updates, dict)
                        },
                    )
                    if persist
                    else None
                )
                for section_name, updates in updates_by_section.items():
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
                            reranker_factory = getattr(sys_mod, "create_reranker", create_reranker)
                            validate_ready = getattr(
                                sys_mod, "_validate_reranker_ready", _validate_reranker_ready
                            )
                            new_reranker = reranker_factory(candidate_rerank)
                            await validate_ready(new_reranker)
                        except Exception as e:
                            if new_reranker is not None:
                                await hr._close_reranker_safely(new_reranker)
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

                    # Coerce every key first, then assign the section in one
                    # go. ``assign_section_fields`` re-runs the section's
                    # cross-field ``@model_validator(mode="after")`` — which the
                    # bare ``setattr`` path skips — so an invalid combination is
                    # rejected here instead of being persisted and then dropped
                    # by every later load (#2110). Validating the update as a
                    # whole matters on this surface in particular: the settings
                    # card sends every dirty field of a section at once, and a
                    # per-key check would refuse a pair that is legal together
                    # (raising min_chunk_tokens and max_chunk_tokens in the same
                    # request) depending on key order.
                    section_updates: dict[str, object] = {}
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

                        section_updates[key] = coerced

                    if not section_updates:
                        continue

                    try:
                        old_values = assign_section_fields(section_obj, section_updates)
                    except ValueError as e:
                        # Nothing was assigned — the helper restored the section.
                        rejected.append(f"{section_name}: {e}")
                        continue

                    for key, coerced in section_updates.items():
                        full_key = f"{section_name}.{key}"
                        old_val = old_values[key]
                        if full_key == "search.tokenizer" and old_val != coerced:
                            tokenizer_changed = True
                        if full_key == "embedding.onnx_batch_size" and old_val != coerced:
                            onnx_batch_changed = True

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
                # Nothing accepted means nothing to write: a fully-rejected
                # request used to still take the save path, which rewrites
                # config.json (delta-only, so it can prune unrelated stale
                # entries) on the operator's behalf for a request that changed
                # nothing. Whole-section refusal (#2110) makes that case common.
                if persist and (applied or rerank_changed):
                    save_fn = getattr(sys_mod, "save_config_overrides", save_config_overrides)
                    try:
                        save_fn(config)
                    except Exception as e:
                        if snapshot is not None:
                            snapshot.restore()
                        hr.record_save_failure(request.app)
                        # Discard the validated-but-uninstalled reranker.
                        if pending_reranker is not None:
                            await hr._close_reranker_safely(pending_reranker)
                        if isinstance(e, TimeoutError):
                            raise HTTPException(
                                503,
                                "Config update timed out — another update may be in progress",
                            )
                        if isinstance(e, ValueError):
                            raise HTTPException(400, detail=str(e))
                        raise
                    # Self-write mtime bump — otherwise the next GET sees
                    # our own edit as "external" and reloads spuriously.
                    hr.commit_writer_signature(request.app)

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

                if onnx_batch_changed:
                    embedder = getattr(request.app.state, "embedder", None)
                    publish_fn = getattr(
                        sys_mod, "publish_onnx_batch_size", publish_onnx_batch_size
                    )
                    publish_fn(embedder, config.embedding.onnx_batch_size)

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
    sys_mod = _sys()
    hr = getattr(sys_mod, "_hot_reload", _hot_reload)
    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                await hr.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                save_fn = getattr(sys_mod, "save_config_overrides", save_config_overrides)
                try:
                    save_fn(request.app.state.config)
                except Exception as e:
                    # Saving does not mutate config. Preserve any preceding
                    # runtime-only edits instead of replacing them from disk.
                    hr.record_save_failure(request.app)
                    if isinstance(e, ValueError):
                        raise HTTPException(400, detail=str(e))
                    raise
                hr.commit_writer_signature(request.app)
    except TimeoutError:
        raise HTTPException(503, "Config save timed out — another update may be in progress")
    return {"ok": True, "message": "Config saved to ~/.memtomem/config.json"}


_DENIED_MEMORY_DIR_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/private/etc",
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/dev",
    "/proc",
    "/sys",
)
_DENIED_MEMORY_DIR_PARTS: frozenset[str] = frozenset({".ssh", ".aws", ".gnupg", ".kube"})


def validate_memory_dir_path(dir_path: str) -> Path:
    """Validate a candidate memory_dir path before creating or registering it."""
    if "\x00" in dir_path:
        raise HTTPException(status_code=400, detail="Invalid directory path")
    expanded = Path(dir_path).expanduser()
    if expanded.is_symlink():
        raise HTTPException(status_code=403, detail="Symlinked memory directories are not allowed")
    resolved = expanded.resolve()
    if resolved == Path(resolved.anchor):
        raise HTTPException(status_code=403, detail="Path is not allowed as a memory directory")
    # Compare case-folded: on a case-insensitive filesystem (APFS and NTFS by
    # default) ``resolve()`` keeps the caller's spelling, so ``/USR/local`` and
    # ``~/.SSH`` name the denied directories. On a case-sensitive one this also
    # refuses spellings that name a different directory, which fails closed.
    folded = resolved.as_posix().casefold()
    for prefix in _DENIED_MEMORY_DIR_PREFIXES:
        denied = prefix.casefold()
        if folded == denied or folded.startswith(denied + "/"):
            raise HTTPException(status_code=403, detail="Path is not allowed as a memory directory")
    if any(part.casefold() in _DENIED_MEMORY_DIR_PARTS for part in resolved.parts):
        raise HTTPException(status_code=403, detail="Path is not allowed as a memory directory")
    if resolved.exists() and not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    return resolved


@router.post(
    "/memory-dirs/add",
    dependencies=[Depends(require_configured), Depends(_require_localhost)],
)
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
    sys_mod = _sys()
    hr = getattr(sys_mod, "_hot_reload", _hot_reload)
    body = await request.json()
    dir_path = body.get("path", "").strip()
    auto_index = bool(body.get("auto_index", True))
    # Strict: only a literal JSON ``true`` bypasses the redaction gate. A plain
    # ``bool(...)`` would make the string ``"false"`` (and any non-empty string)
    # truthy — a silent secret-bypass footgun on a security override.
    force_unsafe = body.get("force_unsafe", False) is True
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")

    resolved = validate_memory_dir_path(dir_path)
    if not resolved.is_dir():
        resolved.mkdir(parents=True, exist_ok=True)

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                await hr.reload_if_stale(
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
                    snapshot = _ConfigSnapshot(config, {"indexing": {"memory_dirs"}})
                    config.indexing.memory_dirs.append(resolved)
                    save_fn = getattr(sys_mod, "save_config_overrides", save_config_overrides)
                    try:
                        save_fn(config)
                    except Exception as e:
                        snapshot.restore()
                        hr.record_save_failure(request.app)
                        if isinstance(e, TimeoutError):
                            raise HTTPException(
                                503, "memory-dirs/add timed out — another update may be in progress"
                            )
                        raise
                    hr.commit_writer_signature(request.app)

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
            # #2141: register+index in one call, so a query warmed before the
            # registration would otherwise keep answering without the new
            # directory's chunks for up to ``search.cache_ttl``.
            if stats.mutated:
                search_pipeline.invalidate_cache()
            indexed = {
                "total_files": stats.total_files,
                "total_chunks": stats.total_chunks,
                "indexed_chunks": stats.indexed_chunks,
                "skipped_chunks": stats.skipped_chunks,
                "deleted_chunks": stats.deleted_chunks,
                "duration_ms": stats.duration_ms,
                "errors": list(stats.errors) if stats.errors else [],
                "retryable_errors": list(stats.retryable_errors),
                "blocked_files": stats.blocked_files,
                "blocked_paths": list(stats.blocked_paths),
                "blocked_project_shared_files": stats.blocked_project_shared_files,
                # #2076: files indexed under a frontmatter-declared exemption.
                "exempted_files": stats.exempted_files,
                "exempted_paths": list(stats.exempted_paths),
                "chunks_missing_vectors": stats.chunks_missing_vectors,
                # The advisory fields travel with every other index result;
                # this hand-built payload omitted them, so the shared reporter
                # had nothing to report here even once it was called.
                "namespaces_preserved_against_rules": (stats.namespaces_preserved_against_rules),
                "namespaces_reassigned": stats.namespaces_reassigned,
                "namespace_moves": list(stats.namespace_moves),
            }
            has_issues = bool(stats.errors or stats.blocked_files)
            has_processed_chunks = (stats.indexed_chunks + stats.skipped_chunks) > 0
            if not has_issues:
                index_status = "success"
            elif has_processed_chunks:
                index_status = "partial"
            else:
                index_status = "failed"
        except RetryableError:
            # Ahead of the generic branch: the engine's pre-write namespace
            # prepass (issue #2005) raises rather than returning per-file
            # errors, so the ``retryable_errors`` key populated on the success
            # path above would otherwise be absent for the one failure class it
            # exists to describe — the client would see a flat "failed" and
            # have no way to know a retry is the right move. The message is
            # fixed rather than derived from the exception: ``str(exc)`` can
            # carry the scanned path, and this response is not path-safe.
            logger.exception("Initial indexing failed (retryable) for newly registered memory dir")
            message = (
                "Initial indexing failed: chunk store unreachable. Retry once it is reachable."
            )
            indexed = {
                "error": message,
                "errors": [message],
                "retryable_errors": [message],
            }
            index_status = "failed"
        except Exception:  # pragma: no cover — surface partial result
            logger.exception("Initial indexing failed for newly registered memory directory")
            indexed = {"error": "Initial indexing failed", "errors": [], "retryable_errors": []}
            index_status = "failed"

    return {
        "ok": True,
        "message": message,
        "memory_dirs": memory_dirs_snapshot,
        "kind": kind,
        "indexed": indexed,
        "index_status": index_status,
    }


def _path_identity_key(path: str) -> str:
    """Compare a path as sent with what it resolves to.

    ``os.path.normcase`` so a Windows spelling that differs only in case or
    separators still matches; it is the identity on POSIX. No Unicode
    normalisation: ``resolve()`` keeps the names of components that are not
    symlinks as given, so a difference in form means a link was followed. On
    a filesystem that keeps NFC and NFD names apart, folding them here would
    let a symlink from one to the other pass (#2539).
    """
    return os.path.normcase(path)


@router.post("/memory-dirs/remove", dependencies=[Depends(_require_localhost)])
async def remove_memory_dir(
    request: Request,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
):
    """Remove a directory from ``memory_dirs``, optionally deleting its chunks.

    Body: ``{path: str, delete_chunks?: bool}``. ``delete_chunks=False`` (the
    default) is the safe behaviour — only the registration is removed,
    indexed chunks stay searchable. ``delete_chunks=True`` additionally
    drops the chunks of every source under the removed dir that no
    still-configured index root contains. A nested root, an enclosing root,
    or the same path listed in another tier keeps its sources, since it would
    index them again anyway (#2524, #2534). The dir's status
    ``delete_chunk_count``, which the confirm dialog shows, is a preview of
    that number, and the response's ``deleted_chunks`` is what the sweep did.
    The underlying files on disk are never touched. The Web UI's delete confirm
    shows a checkbox so the user opts in explicitly.

    Known limits (#2534):

    - Containment is decided on the stored, resolved path. A symlink in a
      surviving root that points at a file under the removed one does not
      keep that file's chunks. The surviving root's next walk, or the next
      event on the link, indexes the file again.
    - The sweep takes no indexing lock. Indexing that was already under way
      when the dir was removed (a watcher flush that passed its root check, a
      backfill ``index_path`` run) can write after the sweep read its rows.
      Taking ``_index_lock`` would not close this, because the bulk path holds
      only per-file sidecars (#2105).
    - The dialog's count comes from an earlier status request, which reads
      the config without reloading it. Roots or indexed files can change
      before this request: another tab, the CLI, a ``config.json`` edit, or
      the watcher. The sweep applies the rule to the roots and rows present
      when it runs, so it can delete more or fewer chunks than the dialog
      showed. More only when a nested root went away or files were indexed
      under the dir, which leaves chunks under the dir that no remaining root
      would index. The route does not re-check the count, because any
      watcher write that changes the dir's chunks would fail it (#2537).

    The path must still resolve to itself. Both Web UI callers send a path the
    server returned already resolved. If that path, or one of its parents, has
    since become a symlink or junction, it now names a different directory,
    and removing or sweeping that one would act on a root the user did not
    pick. The route answers 409 and changes nothing. A client that sends an
    alias spelling, such as a symlinked prefix, gets the same 409 and has to
    send the resolved path (#2539).
    """
    sys_mod = _sys()
    hr = getattr(sys_mod, "_hot_reload", _hot_reload)
    body = await request.json()
    dir_path = body.get("path", "").strip()
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")
    delete_chunks = bool(body.get("delete_chunks", False))

    try:
        async with _asyncio.timeout(60):
            async with _config_lock:
                await hr.reload_if_stale(
                    request.app, storage=storage, search_pipeline=search_pipeline
                )
                _check_reload_block(request)
                config = request.app.state.config

                # Resolved once, here, and reused below: resolving again after
                # the awaits further down would let a swap made in between
                # redirect the sweep (#2539). ``resolve()`` runs on the path as
                # sent, not on ``literal``: ``abspath`` folds ``..`` without
                # following symlinks, so on POSIX ``link/../d`` would otherwise
                # check and remove a directory the OS would not reach by that
                # path. Win32 folds ``..`` first itself, and so does
                # ``ntpath.realpath``, so there both sides agree.
                expanded = Path(dir_path).expanduser()
                literal = os.path.abspath(expanded)
                resolved = expanded.resolve()
                if _path_identity_key(literal) != _path_identity_key(str(resolved)):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"{literal} now resolves to {resolved}. Reload the list and "
                            "try again, or send the resolved path."
                        ),
                    )
                resolved_norm = fold_path_form(str(resolved))

                new_dirs = [
                    p
                    for p in config.indexing.memory_dirs
                    if norm_path(Path(p).expanduser()) != resolved_norm
                ]
                if len(new_dirs) == len(config.indexing.memory_dirs):
                    raise HTTPException(status_code=404, detail="Directory not in memory_dirs")
                if len(new_dirs) == 0:
                    raise HTTPException(status_code=400, detail="Cannot remove last memory_dir")

                snapshot = _ConfigSnapshot(config, {"indexing": {"memory_dirs"}})
                config.indexing.memory_dirs = new_dirs
                save_fn = getattr(sys_mod, "save_config_overrides", save_config_overrides)
                try:
                    save_fn(config)
                except Exception as e:
                    snapshot.restore()
                    hr.record_save_failure(request.app)
                    if isinstance(e, TimeoutError):
                        raise HTTPException(
                            503, "memory-dirs/remove timed out — another update may be in progress"
                        )
                    raise
                hr.commit_writer_signature(request.app)

                # Chunk cleanup happens after the registration is removed
                # so a partial failure never leaves chunks orphaned with
                # the dir still registered. ``delete_by_source`` cascades
                # to ``chunks_fts`` / ``chunks_vec`` / ``chunk_links`` via
                # the schema's ``ON DELETE CASCADE``.
                deleted_chunks = 0
                if delete_chunks:
                    from memtomem.indexing.engine import norm_dir_prefix, swept_on_remove

                    rows = await storage.get_source_files_with_counts()
                    # The same form :func:`norm_dir_prefix` builds (Unicode form
                    # folded where the filesystem folds it, a trailing
                    # ``os.sep``; #647), made from the path checked above
                    # instead of resolving it again after the await.
                    # A second resolve would follow a symlink swapped in
                    # meanwhile and sweep that directory (#2539).
                    prefix = (
                        resolved_norm if resolved_norm.endswith(os.sep) else resolved_norm + os.sep
                    )
                    # Every root still configured now that this one is gone. A
                    # source any of them contains stays (#2524, #2534); the
                    # status ``delete_chunk_count`` the dialog shows applies the
                    # same rule.
                    surviving = [norm_dir_prefix(p) for p in config.indexing.all_index_roots()]
                    try:
                        for row in rows:
                            source_path = row[0]
                            # Judged and deleted by the stored key: resolving it
                            # would follow a path that has since become a link
                            # to another source's rows (#2835, #2878).
                            key = fold_path_form(str(source_path))
                            if swept_on_remove(key, prefix, surviving):
                                deleted_chunks += await storage.delete_by_source(
                                    source_path, as_stored=True
                                )
                    finally:
                        # Same cache-staleness class as #2141: these rows are
                        # gone from the store but a warmed query would still
                        # return them. In ``finally`` because a source that
                        # fails partway through the sweep must not discard the
                        # deletions the sources before it already committed.
                        if deleted_chunks:
                            search_pipeline.invalidate_cache()

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


@router.post("/memory-dirs/open", dependencies=[Depends(_require_localhost)])
async def open_memory_dir(request: Request, config=Depends(get_config)):
    """Reveal a configured index root in the OS file manager.

    Body: ``{path: str}``. The path must already be one of
    ``config.indexing.all_index_roots()`` — user, project, or read-only
    (#2522: the Sources tree shows a group, and this button, for every
    tier). Arbitrary filesystem paths cannot be opened through this
    endpoint, since ``mm web`` is a local tool but defense-in-depth keeps
    the route useful even if the bind host were ever changed away from
    ``127.0.0.1``. Missing dirs return 404 rather than spawning a
    file-manager pointed at nothing.
    """
    body = await request.json()
    dir_path = body.get("path", "").strip()
    if not dir_path:
        raise HTTPException(status_code=400, detail="path is required")

    resolved = Path(dir_path).expanduser().resolve()
    resolved_norm = norm_path(resolved)

    # Resolved on both sides: the Sources tree sends the resolved path it
    # got from ``/api/memory-dirs/status``, which a root configured under a
    # symlinked prefix would otherwise never match.
    in_list = any(
        norm_path(Path(p).expanduser().resolve()) == resolved_norm
        for p in config.indexing.all_index_roots()
    )
    if not in_list:
        raise HTTPException(status_code=404, detail="Directory is not a configured index root")
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Directory does not exist on disk")

    opener = getattr(_sys(), "_open_in_file_manager", _open_in_file_manager)
    try:
        opener(resolved)
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

    Each entry carries ``tier`` — ``user`` (``memory_dirs``), ``project``
    (``project_memory_dirs``) or ``read_only`` (``read_only_memory_dirs``)
    — so the Sources tree can badge a group and offer "Remove from
    memory_dirs" only where it applies. (#2522)

    ``delete_chunk_count`` is a preview of what ``POST /memory-dirs/remove``
    with ``delete_chunks`` would delete for that entry: 0 when another root
    still contains the whole dir, and 0 for tiers the route cannot remove.
    (#2534) It is computed from this request's config, which is not reloaded
    here, and rows. The remove applies the same rule to what is configured
    and indexed when it runs, and reports that number (#2537).

    What the entry owns that ``GET /api/sources`` does not list by default is
    counted under the rule that route applies
    (:class:`~memtomem.web.source_visibility.SourceVisibility`):
    ``hidden_chunk_count`` / ``hidden_source_file_count`` count every such
    source once, so totals minus hidden are what the Sources tree lists for
    the root. ``held_*`` counts the held or pending ones (#2561) and
    ``project_local_*`` the ``project_local`` tier (#2567); a held draft is in
    both. The delete count still includes hidden chunks, because the sweep
    removes them.
    """
    from memtomem.indexing.engine import memory_dir_stats, norm_dir_prefix, remove_sweeps_nothing
    from memtomem.web.source_visibility import load_source_visibility

    visibility = await load_source_visibility(storage, config)
    stats = await memory_dir_stats(
        storage,
        config.indexing.all_index_roots(),
        supported_extensions=config.indexing.supported_extensions,
        hidden_reasons=lambda path: visibility.check(path)[0],
    )

    def _resolved(dirs: Iterable[Path]) -> set[str]:
        return {norm_path(Path(d).expanduser().resolve()) for d in dirs}

    # First match wins, in ``all_index_roots()`` order. Read-only roots are
    # disjoint from the writable tiers (``check_read_only_roots_disjoint``);
    # a path listed as both user and project reports ``user``.
    tiers = (
        ("user", _resolved(config.indexing.memory_dirs)),
        ("project", _resolved(config.indexing.project_memory_dirs)),
        ("read_only", _resolved(config.indexing.read_only_memory_dirs)),
    )
    # The remove route drops every ``memory_dirs`` entry equal to the path, so
    # what survives is every other root, including the same path in another
    # tier.
    user_keys = [norm_path(Path(p).expanduser()) for p in config.indexing.memory_dirs]
    others = [
        norm_dir_prefix(p)
        for p in (*config.indexing.project_memory_dirs, *config.indexing.read_only_memory_dirs)
    ]
    for entry in stats:
        key = norm_path(Path(str(entry["path"])))
        entry["tier"] = next((name for name, paths in tiers if key in paths), None)
        delete_count = 0
        if entry["tier"] == "user":
            surviving = [
                norm_dir_prefix(p)
                for p, k in zip(config.indexing.memory_dirs, user_keys)
                if k != key
            ] + others
            if not remove_sweeps_nothing(norm_dir_prefix(key), surviving):
                delete_count = entry["chunk_count"]
        entry["delete_chunk_count"] = delete_count
    return {"dirs": stats}


@router.get("/embedding-status", response_model=EmbeddingStatusResponse)
async def get_embedding_status(storage=Depends(get_storage)) -> EmbeddingStatusResponse:
    stored_info = getattr(storage, "stored_embedding_info", None)
    stored_out = (
        EmbeddingConfigInfo(
            dimension=stored_info["dimension"],
            provider=stored_info["provider"],
            model=stored_info["model"],
            policy_fingerprint=stored_info.get("policy_fingerprint") or "",
            max_sequence_tokens=stored_info.get("max_sequence_tokens"),
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
        policy_mismatch=mismatch.get("policy_mismatch", False),
        # A store may not record its policy (#2619): ``None`` renders as "".
        stored=EmbeddingConfigInfo(
            **{
                **mismatch["stored"],
                "policy_fingerprint": mismatch["stored"].get("policy_fingerprint") or "",
            }
        ),
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
    request: Request,
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> EmbeddingResetResponse:
    """Reset embedding metadata to current config. Drops all vectors."""
    from memtomem.config import embedding_policy_fingerprint

    await storage.reset_embedding_meta(
        dimension=config.embedding.dimension,
        provider=config.embedding.provider,
        model=config.embedding.model,
        policy_fingerprint=embedding_policy_fingerprint(config.embedding),
        max_sequence_tokens=config.embedding.max_sequence_tokens,
    )
    message = "Embedding metadata reset. All indexed vectors deleted — run a forced re-index."
    # A degraded startup leaves the watcher stopped, so auto-indexing is off
    # until something starts it. The MCP server does that in-process from its
    # own reset (#2181); this one does the same (#2188) rather than telling the
    # user to restart. Ordered after the write on purpose: the watcher's
    # startup backfill indexes immediately, and the mismatch must be gone
    # before the indexer touches the store.
    resume_fn = getattr(_sys(), "_resume_watching", _resume_watching)
    message += await resume_fn(request.app.state)
    return EmbeddingResetResponse(ok=True, message=message)


async def _resume_watching(state) -> str:
    """Start the file watcher a degraded startup left stopped; report in words.

    Returns the sentence to append to the reset message, empty when the
    watcher was already running and there is nothing to say.
    """
    from memtomem.indexing.watcher import WatcherResumer

    # Two resets arriving together must not both pass the "not started yet"
    # check: ``start`` overwrites the observer and the processor task, so the
    # second would strand the first pair with nothing able to stop them. Every
    # app built by ``create_app`` publishes the lock; a hand-assembled state
    # without one is single-caller by construction.
    lock = getattr(state, "file_watcher_resume_lock", None)
    if lock is None:
        return await _resume_watching_locked(state, WatcherResumer)
    async with lock:
        return await _resume_watching_locked(state, WatcherResumer)


async def _resume_watching_locked(state, resumer_cls) -> str:
    watcher = getattr(state, "file_watcher", None)
    # Read inside the lock: the reset that went first may have just started it.
    started = getattr(state, "file_watcher_started", watcher is not None)
    blocked = getattr(state, "file_watcher_resume_blocked", False)
    if started:
        return ""
    if watcher is None or blocked:
        # No instance to start, or an earlier attempt failed in a way that
        # cannot be retried in this process.
        return (
            " File watching is off in this server and cannot be started here:"
            " restart `mm web` to resume auto-indexing."
        )

    resumer = resumer_cls(watcher, started=started, can_retry=not blocked)
    try:
        await resumer.resume()
    finally:
        # In ``finally`` for the same reason the MCP side settles there: a
        # cancellation mid-start still decides whether this instance may be
        # started again, and losing that would let a later reset start over
        # handles nothing can stop.
        state.file_watcher_started = resumer.started
        state.file_watcher_resume_blocked = not resumer.can_retry
    if resumer.started:
        return " File watching is on again, so new edits are auto-indexed."
    if resumer.can_retry:
        return (
            " Starting the file watcher failed, so auto-indexing is still off:"
            " run this reset again, or restart `mm web`."
        )
    return (
        " Starting the file watcher failed and it could not be cleaned up, so"
        " auto-indexing is still off: restart `mm web` to resume it."
    )
