"""Working memory (scratch) endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from memtomem.errors import NamespaceResolutionError
from memtomem.models import ChunkMetadata
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.source_provenance import EXCLUDED_TARGET_DETAIL, READ_ONLY_TARGET_DETAIL
from memtomem.web.deps import get_config, get_index_engine, get_search_pipeline, get_storage
from memtomem.web.routes._errors import NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
from memtomem.web.schemas.scratch import (
    ScratchDeleteResponse,
    ScratchEntryOut,
    ScratchListResponse,
    ScratchPromoteRequest,
    ScratchPromoteResponse,
    ScratchSetRequest,
    ScratchSetResponse,
)

router = APIRouter(prefix="/scratch", tags=["scratch"])

_UNTAGGED_NAMESPACE = ChunkMetadata(source_file=Path()).namespace


@router.get("", response_model=ScratchListResponse)
async def list_scratch(
    storage=Depends(get_storage),
) -> ScratchListResponse:
    """List all working memory entries."""
    rows = await storage.scratch_list()
    entries = [ScratchEntryOut(**r) for r in rows]
    return ScratchListResponse(entries=entries, total=len(entries))


@router.post("", response_model=ScratchSetResponse)
async def set_scratch(
    body: ScratchSetRequest,
    storage=Depends(get_storage),
) -> ScratchSetResponse:
    """Set a working memory entry."""
    expires_at = None
    if body.ttl_minutes:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)).isoformat(
            timespec="seconds"
        )
    await storage.scratch_set(
        body.key, body.value, session_id=body.session_id, expires_at=expires_at
    )
    return ScratchSetResponse(key=body.key)


@router.delete("/{key}", response_model=ScratchDeleteResponse)
async def delete_scratch(
    key: str,
    storage=Depends(get_storage),
) -> ScratchDeleteResponse:
    """Delete a working memory entry."""
    deleted = await storage.scratch_delete(key)
    return ScratchDeleteResponse(key=key, deleted=deleted)


@router.post("/{key}/promote", response_model=ScratchPromoteResponse)
async def promote_scratch(
    key: str,
    body: ScratchPromoteRequest,
    storage=Depends(get_storage),
    config=Depends(get_config),
    index_engine=Depends(get_index_engine),
    search_pipeline=Depends(get_search_pipeline),
) -> ScratchPromoteResponse:
    """Promote a working memory entry to long-term memory."""
    import asyncio as _asyncio

    entry = await storage.scratch_get(key)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Key '{key}' not found")

    from memtomem import privacy
    from memtomem.tools.memory_writer import append_entry

    # Promotion is the trust-boundary crossing — scratch is short-term
    # and stays in SQLite, but LTM markdown lands on disk and feeds
    # search. Apply the same guard as ``mem_add``.
    guard = privacy.enforce_write_guard(
        entry["value"],
        surface="web_api_scratch_promote",
        force_unsafe=body.force_unsafe,
        audit_context={"key": key},
    )
    if guard.decision == "blocked":
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "redaction_blocked",
                "hits": len(guard.hits),
                "surface": "web_api_scratch_promote",
            },
        )

    bases = [Path(d).expanduser().resolve() for d in config.indexing.memory_dirs]
    bases_norm = [Path(norm_path(b)) for b in bases]
    if body.file:
        raw_target = Path(body.file).expanduser()
        if raw_target.is_symlink():
            raise HTTPException(status_code=403, detail="Symlinked targets are not allowed")
        target = raw_target.resolve()
        if target.is_symlink():
            raise HTTPException(status_code=403, detail="Symlinked targets are not allowed")
        if target.suffix.lower() not in {".md", ".markdown"}:
            raise HTTPException(
                status_code=400,
                detail="Target file must be a Markdown file (.md or .markdown)",
            )
        target_norm = Path(norm_path(target))
        if not any(target_norm.is_relative_to(b) for b in bases_norm):
            raise HTTPException(
                status_code=403, detail="Path is outside configured memory directories"
            )
    else:
        from memtomem.memory_scope import require_user_base

        # ConfigError → the app-level 409 handler; the "index nothing"
        # state has no default promotion destination (#1768).
        base = require_user_base(config.indexing.memory_dirs, config.indexing.project_memory_dirs)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = base / f"{date_str}.md"

    # #2488: indexing skips an excluded target, so the promoted entry would never
    # become searchable while the scratch row is marked promoted. Refuse first.
    if index_engine.is_excluded(target):
        raise HTTPException(status_code=409, detail=EXCLUDED_TARGET_DETAIL)
    if index_engine.is_read_only_source(target):
        raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)

    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        async_file_lock,
        memory_lock_path,
    )

    try:
        async with async_file_lock(memory_lock_path(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S):
            # Resolve the namespace before appending, as web ``/api/add`` does
            # (#2005 follow-up): a lookup failure inside ``index_file`` would
            # land after the append is durable, and a retry of the 500 would
            # append the entry again (#2731). Passing the answer on keeps the
            # re-index from asking a second time.
            try:
                resolved_ns = await index_engine.effective_namespace_for(target)
            except NamespaceResolutionError as exc:
                raise HTTPException(
                    status_code=503, detail=NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
                ) from exc
            # ``None`` is the untagged carve-out, which stores the chunk model's
            # default rather than ``namespace.default_namespace``; the two differ
            # when that setting is "".
            write_ns = resolved_ns if resolved_ns is not None else _UNTAGGED_NAMESPACE
            await _asyncio.to_thread(
                append_entry, target, entry["value"], title=body.title, tags=body.tags
            )
            stats = await index_engine.index_file(
                target, namespace=write_ns, already_scanned=True, lock_held=True
            )
            # #2141: a warmed query must not answer as if the entry was never added.
            if stats.mutated:
                search_pipeline.invalidate_cache()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Memory file is locked by another writer; try again."
        ) from exc

    # Mark as promoted
    await storage.scratch_promote(key)

    return ScratchPromoteResponse(
        key=key,
        promoted=True,
        file=str(target),
        indexed_chunks=stats.indexed_chunks,
    )
