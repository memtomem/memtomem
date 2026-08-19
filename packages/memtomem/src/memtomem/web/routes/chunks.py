"""Chunk CRUD endpoints."""

from __future__ import annotations

import logging
import stat
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.errors import NamespaceResolutionError
from memtomem.server.tools.search import _resolve_project_context_from_dirs
from memtomem.services import tag_management as tag_svc
from memtomem.tools.memory_writer import remove_lines, replace_chunk_body
from memtomem.web.deps import (
    get_config,
    get_embedder,
    get_index_engine,
    get_search_pipeline,
    get_storage,
    require_indexed_source,
)
from memtomem.web.routes._errors import NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
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
async def get_chunk(
    chunk_id: UUID,
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> ChunkOut:
    # Knowing an id is not authorization. Hydrate through the same always-on
    # ADR-0011 scope fragment as search/recall: outside a project only user
    # rows are visible; inside one, user + that project's rows are visible.
    project_context_root = _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)
    chunks = await storage.recall_chunks(
        chunk_ids=(chunk_id,),
        limit=1,
        project_context_root=project_context_root,
    )
    if not chunks:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return chunk_to_out(chunks[0])


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
        if fresh is None:
            raise HTTPException(status_code=409, detail="Chunk state changed; retry.")
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
        except NamespaceResolutionError as exc:
            # Before the generic handler below, and the web twin of the MCP
            # ``_mutate_file_and_reindex`` re-raise (#2005 follow-up): the
            # re-index could not read the file's stored namespace, which is
            # transient. ``mutate_source_and_reindex`` restored the pre-image
            # before re-raising, so nothing was changed and a retry is the
            # right response — a 500 would tell the caller to file a bug
            # instead. Mirrors the 503 the delete route below already answers.
            logger.warning("Namespace lookup failed during chunk edit %s: %s", chunk_id, exc)
            raise HTTPException(
                status_code=503, detail=NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
            ) from exc
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
    config=Depends(get_config),
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
        if fresh is None:
            raise HTTPException(status_code=409, detail="Chunk state changed; retry.")
        meta = fresh.metadata
        source = meta.source_file

        async def ensure_index_row_absent(*, source_removed: bool) -> None:
            """Finish an index-only delete and verify the route's post-condition.

            Once the source line has been removed, a failed cleanup is a partial
            success and must not invite the caller to repeat DELETE with the now
            stale line range.  Keep that response distinct from failures before
            the source mutation (#2016).
            """

            failure_detail = (
                "The source entry was removed, but index cleanup did not complete. "
                "Reindex the source before attempting another delete."
                if source_removed
                else "Index-only deletion failed; no source file was changed. Check server logs."
            )
            try:
                remaining = await storage.get_chunk(chunk_id)
                if remaining is not None:
                    await storage.delete_chunks([chunk_id])
                    remaining = await storage.get_chunk(chunk_id)
            except Exception as exc:
                logger.error("Index cleanup failed for deleted chunk %s", chunk_id, exc_info=True)
                raise HTTPException(status_code=500, detail=failure_detail) from exc
            if remaining is not None:
                logger.error("Chunk %s remained after index cleanup", chunk_id)
                raise HTTPException(status_code=500, detail=failure_detail)

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

        # Only a source that is genuinely absent permits an index-only delete.
        # ``Path.exists`` collapses permission errors, ELOOP, and other failures
        # into absence on supported Python versions; that would recreate the
        # false-success shape fixed here (#2016).  Mirror the fail-closed stat
        # policy used by the namespace-mix guard (#2017).
        try:
            source_stat = source.stat()
        except (FileNotFoundError, NotADirectoryError):
            source_exists = False
        except OSError as exc:
            logger.warning("Could not inspect source for chunk %s", chunk_id, exc_info=True)
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not access the source file; no index entry was deleted. "
                    "Retry once the file is accessible."
                ),
            ) from exc
        else:
            source_exists = True
            if not stat.S_ISREG(source_stat.st_mode):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The chunk source is not a regular file; no index entry was deleted. "
                        "Repair or reindex the source before retrying."
                    ),
                )

        # Remove lines from the original source file, then re-index. No file
        # rollback here (unlike edit): the intent is deletion, so on a reindex
        # failure we fall back to an index-only delete rather than restoring the
        # line. ``lock_held=True`` skips the nested sidecar acquire (#1587).
        if source_exists:
            if meta.start_line < 1 or meta.end_line < meta.start_line:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Chunk has no usable source-line provenance; no index entry was "
                        "deleted. Reindex the source and retry."
                    ),
                )

            # Issue #2005: ``force=True`` used to re-apply namespace
            # resolution to every chunk, including the ones this delete leaves
            # alone — so deleting one chunk from an ``aaa`` file moved its
            # survivors to whatever the rules said that day. Passing the
            # file's existing namespace was the fix.
            #
            # Since #2061 / ADR-0033 the engine preserves a unanimously stored
            # namespace under ``force`` on its own, so this pin is redundant
            # for that case — kept deliberately: it is also what turns the
            # untagged carve-out into the stored spelling (see below), and an
            # explicit namespace is the one input whose meaning cannot drift.
            #
            # Outside the try below on purpose. That handler's fallback is an
            # index-only delete, which is the right answer when the *file*
            # edit fails but a wrong one here: the row would go while the
            # source kept the entry, so the chunk returns on the next
            # re-index and the caller was told the delete succeeded. A
            # namespace lookup that cannot answer is retryable, and nothing
            # has been mutated yet.
            try:
                # ``or default_namespace``: the resolver returns ``None`` for
                # the untagged carve-out, and passing that back through would
                # read as "no caller namespace" and re-enter rule resolution —
                # the very thing being avoided. The explicit default stores
                # the same value the carve-out does.
                preserved_ns = (
                    await index_engine.effective_namespace_for(source)
                ) or config.namespace.default_namespace
            except NamespaceResolutionError as exc:
                # Only this one: it is the resolver's declared "the store did
                # not answer" signal and is genuinely retryable. Catching
                # everything here would dress a config or programming error up
                # as a transient failure and invite the caller to keep retrying.
                logger.warning("Namespace lookup failed for %s: %s", source, exc)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Could not determine the source file's namespace; nothing was "
                        "deleted. Retry once the chunk store is reachable."
                    ),
                ) from exc
            try:
                await asyncio.to_thread(remove_lines, source, meta.start_line, meta.end_line)
            except ValueError as exc:
                logger.warning("Stale line provenance for chunk %s: %s", chunk_id, exc)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Chunk source-line provenance is stale; no index entry was deleted. "
                        "Reindex the source and retry."
                    ),
                ) from exc
            except OSError as exc:
                logger.warning("Source file deletion failed for chunk %s", chunk_id, exc_info=True)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Could not update the source file; no index entry was deleted. "
                        "Retry once the file is accessible."
                    ),
                ) from exc
            except Exception as exc:
                logger.error("Source deletion failed for chunk %s", chunk_id, exc_info=True)
                raise HTTPException(
                    status_code=500,
                    detail="Source deletion failed; no index entry was deleted. Check server logs.",
                ) from exc

            try:
                stats = await index_engine.index_file(
                    source,
                    force=True,
                    namespace=preserved_ns,
                    already_scanned=True,
                    lock_held=True,
                )
            except Exception as exc:
                logger.warning("Re-index failed after deleting chunk %s: %s", chunk_id, exc)
            else:
                if stats.errors:
                    logger.warning(
                        "Re-index reported errors after deleting chunk %s: %s",
                        chunk_id,
                        "; ".join(stats.errors),
                    )

            # A single-file index can report an error in IndexingStats instead
            # of raising (for example, an embedding failure), or can finish with
            # zero work after a read error.  Verify the target row rather than
            # treating the await itself as proof of deletion.
            await ensure_index_row_absent(source_removed=True)
        else:
            await ensure_index_row_absent(source_removed=False)

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
