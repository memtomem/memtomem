"""Chunk CRUD endpoints."""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.errors import NamespaceResolutionError
from memtomem.search.visibility import resolve_visible_chunk
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


def _boundary(config) -> Path | None:
    """The caller's ADR-0011 project context root."""
    return _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)


async def _screened_chunk(storage, chunk_id: UUID, config):
    """Fetch a chunk by id, or raise the 404 an unknown id would raise.

    ADR-0036: the id-addressed routes answer "no such chunk" and "not in your
    project" identically. Every fetch on these routes that can influence a
    response goes through here — a raw ``get_chunk`` used only as a preflight
    still leaks, because reaching a *later* check (a 403 on a symlinked
    source, say) tells the caller the row exists.
    """
    chunk = await resolve_visible_chunk(storage, chunk_id, project_context_root=_boundary(config))
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return chunk


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
    # ADR-0036 generalised this route's rule to every id-addressed surface.
    chunks = await storage.recall_chunks(
        chunk_ids=(chunk_id,),
        limit=1,
        project_context_root=_boundary(config),
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
    search_pipeline=Depends(get_search_pipeline),
    config=Depends(get_config),
) -> ChunkOut:
    chunk = await _screened_chunk(storage, chunk_id, config)
    if chunk.metadata.source_file.is_symlink():
        raise HTTPException(status_code=403, detail="Cannot edit chunks from symlinked files.")

    from memtomem import privacy
    from memtomem.tools.memory_mutation import locked_source_chunk, mutate_source_and_reindex

    # #1587: hold the source file's cross-process sidecar (L2) across the whole
    # read → rewrite → reindex → rollback span and re-fetch the chunk fresh under
    # it, so a concurrent MCP CRUD / CLI write / memory-migrate cannot splice us
    # with a stale line range or lose this edit. ``mm web`` has no AppContext L1
    # lock; L2's in-process guard serializes concurrent web handlers too.
    async with locked_source_chunk(storage, chunk_id, project_context_root=_boundary(config)) as (
        fresh,
        reason,
    ):
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
            stats = await mutate_source_and_reindex(
                index_engine,
                meta.source_file,
                lambda: replace_chunk_body(
                    meta.source_file, meta.start_line, meta.end_line, body.new_content
                ),
            )
            # #2141, the web twin of ``memory_crud._mutate_file_and_reindex``:
            # the edit rewrote chunk text (or only its tags / line ranges,
            # which the counters report as ``skipped``), so a query warmed
            # before it must not keep serving the pre-edit body.
            if stats.mutated:
                search_pipeline.invalidate_cache()
        except NamespaceResolutionError as exc:
            # Before the generic handler below, and the web twin of the MCP
            # ``_mutate_file_and_reindex`` re-raise (#2005 follow-up): the
            # re-index could not read the file's stored namespace, which is
            # transient. ``mutate_source_and_reindex`` restored the pre-image
            # before re-raising, so nothing was changed and a retry is the
            # right response — a 500 would tell the caller to file a bug
            # instead. Mirrors the 503 the delete route below already answers.
            #
            # "Nothing was changed" is a claim about the *file*, not the
            # index: restoring the pre-image runs a rollback re-index, which
            # is itself a write (#2141). Invalidate here for the same reason
            # the generic branch below does, and the same reason the MCP twin
            # invalidates on its rollback path.
            search_pipeline.invalidate_cache()
            logger.warning("Namespace lookup failed during chunk edit %s: %s", chunk_id, exc)
            raise HTTPException(
                status_code=503, detail=NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
            ) from exc
        except Exception as exc:
            # The rollback re-index inside ``mutate_source_and_reindex`` is
            # itself a write, and a failure can land anywhere in that pair, so
            # the only sound post-condition on this branch is "the index may
            # have moved". Mirrors the MCP twin, which invalidates on the
            # rollback path too (``memory_crud.py``).
            search_pipeline.invalidate_cache()
            logger.error("Chunk edit failed for %s: %s", chunk_id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Edit failed. Check server logs.") from exc

    # Screened like every other fetch on this route. The edit already
    # succeeded, so this only decides whether the response echoes the fresh
    # row or the pre-edit one; a chunk re-scoped out from under us during the
    # write falls back rather than returning a row the caller may not read.
    updated = await resolve_visible_chunk(storage, chunk_id, project_context_root=_boundary(config))
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
    search_pipeline=Depends(get_search_pipeline),
) -> DeleteResponse:
    # Preflight, screened like every other fetch on this route, so an unknown
    # or out-of-boundary id 404s before we take a lock. It is not the
    # authoritative check — ``locked_source_chunk`` re-screens the chunk it
    # re-fetches under the lock, which is the value the delete acts on.
    await _screened_chunk(storage, chunk_id, config)

    import asyncio

    from memtomem.tools.memory_mutation import locked_source_chunk

    # #1587: hold the source file's cross-process sidecar (L2) across the
    # remove-lines + reindex span (and the Gate-B probe, re-checked on the fresh
    # chunk under the lock) so a concurrent write cannot resurrect or corrupt the
    # rows we remove. ``mm web`` has no AppContext L1 lock; L2 covers it.
    async with locked_source_chunk(storage, chunk_id, project_context_root=_boundary(config)) as (
        fresh,
        reason,
    ):
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
        # #2141: the chunk delete can commit and the verification read below
        # can then fail with a 500, so the invalidation belongs in ``finally``
        # rather than on the success path. It is armed rather than
        # unconditional: every gate between here and the first write refuses
        # without touching disk or the store, and ``invalidate_cache`` also
        # drops the LLM query-expansion cache, so flushing on a pure refusal
        # would be pointless churn.
        mutation_attempted = False
        try:
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
                # file's existing namespace was the fix at the time.
                #
                # Since #2061 / ADR-0033 the engine preserves a unanimously stored
                # namespace under ``force`` itself, and refuses outright when the
                # file's rows span several. So the re-index below deliberately
                # passes **no** namespace: this pre-flight is a gate, not the
                # write's authority. Pinning what it resolved would re-introduce
                # the bug twice over — an explicit namespace wins over the
                # refusal, and it would freeze a value read outside the write's
                # own critical section, so a concurrent writer that moved the file
                # in between would be silently undone. The engine re-resolves
                # in-lock; that answer is the authoritative one.
                #
                # What the pre-flight is still for: refusing *before* the file is
                # edited. Letting the engine refuse would leave the entry already
                # removed from disk with its chunks still indexed.
                #
                # Outside the try below on purpose. That handler's fallback is an
                # index-only delete, which is the right answer when the *file*
                # edit fails but a wrong one here: the row would go while the
                # source kept the entry, so the chunk returns on the next
                # re-index and the caller was told the delete succeeded. A
                # namespace lookup that cannot answer is retryable, and nothing
                # has been mutated yet.
                try:
                    # ``force=True`` matches the re-index below, so the decision
                    # reports what that call would decide on its own.
                    decision = await index_engine.namespace_decision_for(source, force=True)
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
                if decision.reason == "mixed_force_refused":
                    # Before the file edit below: refusing after it would leave the
                    # entry gone from disk with its chunks still indexed. Permanent
                    # (409, not 503) — retrying changes nothing; splitting the file
                    # per namespace does.
                    logger.warning(
                        "Refusing chunk delete for %s: source spans namespaces %s",
                        source,
                        ", ".join(sorted(decision.stored)),
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This source file's chunks span several namespaces, and the "
                            "re-index a delete performs would rewrite every remaining chunk "
                            "into one of them. Nothing was deleted. Split the file so each "
                            "namespace has its own, then retry."
                        ),
                    )
                try:
                    # Armed before the call, not after: a partial write that
                    # then raises is exactly the case the flag must cover.
                    mutation_attempted = True
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
                    logger.warning(
                        "Source file deletion failed for chunk %s", chunk_id, exc_info=True
                    )
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
                    # No ``namespace=``: see the pre-flight above. The engine
                    # preserves the file's stored namespace in-lock, which is both
                    # the correct value and a fresher one than anything read here.
                    stats = await index_engine.index_file(
                        source,
                        force=True,
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
                # The index-only branch deletes rows directly inside the
                # helper, so it is a mutation by definition.
                mutation_attempted = True
                await ensure_index_row_absent(source_removed=False)
        finally:
            if mutation_attempted:
                search_pipeline.invalidate_cache()

    return DeleteResponse(deleted=1)


@router.patch("/{chunk_id}/tags", response_model=TagsUpdateResponse)
async def update_chunk_tags(
    chunk_id: UUID,
    body: TagsUpdateRequest,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
    config=Depends(get_config),
) -> TagsUpdateResponse:
    """Replace the tags on a chunk with the given list.

    Routed through ``services.tag_management`` so the same
    ``_tag_write_lock`` and cache-invalidation policy that govern global
    rename/delete/merge also cover per-chunk edits — the previous direct
    ``upsert_chunks`` call could race against an in-flight bulk rewrite
    and leave search-result tag filters cached against stale tags.
    """
    updated = await tag_svc.replace_chunk_tags(
        storage,
        chunk_id,
        body.tags,
        project_context_root=_boundary(config),
        search_pipeline=search_pipeline,
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
    config=Depends(get_config),
) -> SimilarChunksResponse:
    """Find chunks semantically similar to the given chunk using dense search."""
    chunk = await _screened_chunk(storage, chunk_id, config)

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
