"""Chunk CRUD endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.services import tag_management as tag_svc
from memtomem.tools.memory_writer import remove_lines, replace_chunk_body
from memtomem.web.deps import (
    get_embedder,
    get_index_engine,
    get_search_pipeline,
    get_storage,
    require_indexed_source,
)
from memtomem.web.schemas.core import (
    ChunkOut,
    DeleteResponse,
    SearchResultOut,
    chunk_to_out,
)
from memtomem.web.schemas.search import SimilarChunksResponse
from memtomem.web.schemas.sources import ChunksListResponse, EditRequest
from memtomem.web.schemas.tags import TagsUpdateRequest, TagsUpdateResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chunks", tags=["chunks"])


@router.get("", response_model=ChunksListResponse)
async def list_chunks(
    source: str = Query(..., description="Absolute path of the source file"),
    limit: int = Query(50, ge=1, le=500),
    storage=Depends(get_storage),
) -> ChunksListResponse:
    indexed_sources = await storage.get_all_source_files()
    request_path = require_indexed_source(source, indexed_sources)
    chunks = await storage.list_chunks_by_source(request_path, limit=limit)
    out = [chunk_to_out(c) for c in chunks]
    total = await storage.count_chunks_by_source(request_path)
    return ChunksListResponse(chunks=out, total=total)


@router.get("/{chunk_id}", response_model=ChunkOut)
async def get_chunk(chunk_id: UUID, storage=Depends(get_storage)) -> ChunkOut:
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return chunk_to_out(chunk)


@router.patch("/{chunk_id}", response_model=ChunkOut)
async def edit_chunk(
    chunk_id: UUID,
    body: EditRequest,
    storage=Depends(get_storage),
    index_engine=Depends(get_index_engine),
) -> ChunkOut:
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    if chunk.metadata.source_file.is_symlink():
        raise HTTPException(status_code=403, detail="Cannot edit chunks from symlinked files.")

    from memtomem import privacy
    from memtomem.tools.memory_mutation import locked_source_chunk, mutate_source_and_reindex

    # #1587: hold the source file's cross-process sidecar (L2) across the whole
    # read → rewrite → reindex → rollback span and re-fetch the chunk fresh under
    # it, so a concurrent MCP CRUD / CLI write / memory-migrate cannot splice us
    # with a stale line range or lose this edit. ``mm web`` has no AppContext L1
    # lock; L2's in-process guard serializes concurrent web handlers too.
    async with locked_source_chunk(storage, chunk_id) as (fresh, reason):
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Chunk not found")
        if reason == "moved":
            raise HTTPException(
                status_code=409, detail="Chunk moved by a concurrent migration; retry."
            )
        if reason == "locked":
            raise HTTPException(
                status_code=503, detail="Memory file is locked by another writer; try again."
            )
        assert fresh is not None
        meta = fresh.metadata

        # Re-check the symlink refusal on the FRESH source under the lock: a
        # concurrent reindex/migration could have re-pointed the row at a
        # symlink resolving to the same target (so ``locked_source_chunk`` does
        # not flag it as "moved"), and we must not edit through it.
        if meta.source_file.is_symlink():
            raise HTTPException(status_code=403, detail="Cannot edit chunks from symlinked files.")

        # ADR-0011 PR-D review round 7: infer scope from the loaded chunk's
        # persisted metadata so Gate A's project_shared hard-refusal of
        # ``force_unsafe=True`` fires on the web edit path too. Evaluated on the
        # fresh chunk (re-fetched under the lock) so a concurrent migrate cannot
        # leave us validating a stale scope. Mirrors MCP ``mem_edit``.
        inferred_scope = meta.scope or "user"
        guard = privacy.enforce_write_guard(
            body.new_content,
            surface="web_api_chunk_edit",
            force_unsafe=body.force_unsafe,
            scope=inferred_scope,
            audit_context={"chunk_id": str(chunk_id), "scope": inferred_scope},
        )
        if guard.decision == "blocked":
            raise HTTPException(
                status_code=403,
                detail={
                    "detail": "redaction_blocked",
                    "hits": len(guard.hits),
                    "surface": "web_api_chunk_edit",
                },
            )
        if guard.decision == "blocked_project_shared":
            raise HTTPException(
                status_code=403,
                detail={
                    "detail": "blocked_project_shared",
                    "hits": len(guard.hits),
                    "surface": "web_api_chunk_edit",
                    "message": (
                        "force_unsafe is not permitted on scope='project_shared' "
                        "chunks (git history is forever). Move the chunk to a "
                        "different scope first, or hand-edit the canonical file."
                    ),
                },
            )

        try:
            # ``replace_chunk_body`` keeps the heading + section-leading
            # blockquote header (``> created:`` / ``> tags:``) intact when the
            # caller passes body-only ``new_content``. The Web UI editor
            # surfaces ``chunk.content`` (already header-stripped by the
            # chunker), so saving without preservation would silently erase
            # the metadata header on disk. Prefix ``new_content`` with ``## ``
            # to override the heading explicitly. Guarded above; skip the engine
            # gate (ADR-0006 PR-A). Rolls back the file on reindex failure.
            await mutate_source_and_reindex(
                index_engine,
                meta.source_file,
                lambda: replace_chunk_body(
                    meta.source_file, meta.start_line, meta.end_line, body.new_content
                ),
            )
        except Exception as exc:
            logger.error("Chunk edit failed for %s: %s", chunk_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Edit failed. Check server logs.") from exc

    updated = await storage.get_chunk(chunk_id)
    return chunk_to_out(updated if updated is not None else chunk)


@router.delete("/{chunk_id}", response_model=DeleteResponse)
async def delete_chunk(
    chunk_id: UUID,
    confirm_project_shared: bool = Query(
        False,
        description=(
            "ADR-0011 Gate B: required when the target chunk lives in "
            "scope='project_shared' (git-tracked tier)."
        ),
    ),
    storage=Depends(get_storage),
    index_engine=Depends(get_index_engine),
) -> DeleteResponse:
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    import asyncio

    from memtomem.tools.memory_mutation import locked_source_chunk

    # #1587: hold the source file's cross-process sidecar (L2) across the
    # remove-lines + reindex span (and the Gate-B probe, re-checked on the fresh
    # chunk under the lock) so a concurrent write cannot resurrect or corrupt the
    # rows we remove. ``mm web`` has no AppContext L1 lock; L2 covers it.
    async with locked_source_chunk(storage, chunk_id) as (fresh, reason):
        if reason == "not_found":
            raise HTTPException(status_code=404, detail="Chunk not found")
        if reason == "moved":
            raise HTTPException(
                status_code=409, detail="Chunk moved by a concurrent migration; retry."
            )
        if reason == "locked":
            raise HTTPException(
                status_code=503, detail="Memory file is locked by another writer; try again."
            )
        assert fresh is not None
        meta = fresh.metadata
        source = meta.source_file

        # ADR-0011 PR-D review round 7: Gate B on the web delete path —
        # mirrors the MCP ``mem_delete`` round-3 fix (8407d73). Re-checked on the
        # fresh chunk so a concurrent re-scope cannot slip a project_shared
        # delete past the confirm.
        inferred_scope = meta.scope or "user"
        if inferred_scope == "project_shared" and not confirm_project_shared:
            logger.info(
                "web delete_chunk rejected project_shared chunk without confirmation",
                extra={"chunk_id": str(chunk_id), "scope": inferred_scope},
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "detail": "blocked_project_shared",
                    "surface": "web_api_chunk_delete",
                    "message": (
                        "Deleting scope='project_shared' chunks requires "
                        "confirm_project_shared=true. The chunk lives in the "
                        "git-tracked memory tier; pass the query parameter to proceed."
                    ),
                },
            )

        # Remove lines from original source file, then re-index. No file
        # rollback here (unlike edit): the intent is deletion, so on a reindex
        # failure we fall back to an index-only delete rather than restoring the
        # line. ``lock_held=True`` skips the nested sidecar acquire (#1587).
        if source.exists() and meta.start_line and meta.end_line:
            try:
                await asyncio.to_thread(remove_lines, source, meta.start_line, meta.end_line)
                await index_engine.index_file(
                    source, force=True, already_scanned=True, lock_held=True
                )
            except Exception as exc:
                logger.warning("Source file edit failed for %s: %s", chunk_id, exc)
                await storage.delete_chunks([chunk_id])
        else:
            await storage.delete_chunks([chunk_id])

    return DeleteResponse(deleted=1)


@router.patch("/{chunk_id}/tags", response_model=TagsUpdateResponse)
async def update_chunk_tags(
    chunk_id: UUID,
    body: TagsUpdateRequest,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
) -> TagsUpdateResponse:
    """Replace the tags on a chunk with the given list.

    Routed through ``services.tag_management`` so the same
    ``_tag_write_lock`` and cache-invalidation policy that govern global
    rename/delete/merge also cover per-chunk edits — the previous direct
    ``upsert_chunks`` call could race against an in-flight bulk rewrite
    and leave search-result tag filters cached against stale tags.
    """
    updated = await tag_svc.replace_chunk_tags(
        storage, chunk_id, body.tags, search_pipeline=search_pipeline
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return TagsUpdateResponse(id=str(chunk_id), tags=list(updated.metadata.tags))


@router.get("/{chunk_id}/similar", response_model=SimilarChunksResponse)
async def similar_chunks(
    chunk_id: UUID,
    top_k: int = Query(5, ge=1, le=50),
    storage=Depends(get_storage),
    embedder=Depends(get_embedder),
) -> SimilarChunksResponse:
    """Find chunks semantically similar to the given chunk using dense search."""
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    embedding = await embedder.embed_query(chunk.content)
    # ADR-0011 PR-D round 11 (P2): pin similar-chunk dense search to
    # the SOURCE chunk's own ``project_root`` rather than letting the
    # always-on storage scope filter default to user-only. Without
    # this, finding similar chunks for a project_shared / project_local
    # row excludes every other project-tier chunk in the same project,
    # because the storage layer treats missing
    # ``project_context_root`` as out-of-project. The chunk we're
    # comparing IS the project context for "similar chunks under the
    # same scope".
    raw = await storage.dense_search(
        embedding,
        top_k=top_k + 1,
        project_context_root=chunk.metadata.project_root,
    )

    results = [
        SearchResultOut(
            chunk=chunk_to_out(r.chunk),
            score=r.score,
            rank=i + 1,
            source="dense",
        )
        for i, r in enumerate(raw)
        if r.chunk.id != chunk_id
    ][:top_k]

    return SimilarChunksResponse(results=results, total=len(results))
