"""Chunk CRUD endpoints."""

from __future__ import annotations

import logging
from uuid import UUID

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.tools.memory_writer import remove_lines, replace_lines
from memtomem.web.deps import get_embedder, get_index_engine, get_storage
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
    request_path = Path(source).resolve()
    indexed_sources = await storage.get_all_source_files()
    indexed_resolved = {p.resolve() for p in indexed_sources}
    if request_path not in indexed_resolved:
        raise HTTPException(status_code=403, detail="Path is not an indexed source file.")
    chunks = await storage.list_chunks_by_source(request_path, limit=limit)
    out = [chunk_to_out(c) for c in chunks]
    return ChunksListResponse(chunks=out, total=len(out))


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

    meta = chunk.metadata
    if meta.source_file.is_symlink():
        raise HTTPException(status_code=403, detail="Cannot edit chunks from symlinked files.")
    try:
        replace_lines(meta.source_file, meta.start_line, meta.end_line, body.new_content)
        await index_engine.index_file(meta.source_file, force=True)
    except Exception as exc:
        logger.error("Chunk edit failed for %s: %s", chunk_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Edit failed. Check server logs.") from exc

    updated = await storage.get_chunk(chunk_id)
    return chunk_to_out(updated if updated is not None else chunk)


@router.delete("/{chunk_id}", response_model=DeleteResponse)
async def delete_chunk(
    chunk_id: UUID,
    storage=Depends(get_storage),
    index_engine=Depends(get_index_engine),
) -> DeleteResponse:
    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    meta = chunk.metadata
    source = meta.source_file

    # Remove lines from original source file, then re-index
    if source.exists() and meta.start_line and meta.end_line:
        try:
            remove_lines(source, meta.start_line, meta.end_line)
            await index_engine.index_file(source, force=True)
        except Exception as exc:
            logger.warning("Source file edit failed for %s: %s", chunk_id, exc)
            # Fall back to index-only delete
            await storage.delete_chunks([chunk_id])
    else:
        await storage.delete_chunks([chunk_id])

    return DeleteResponse(deleted=1)


@router.patch("/{chunk_id}/tags", response_model=TagsUpdateResponse)
async def update_chunk_tags(
    chunk_id: UUID,
    body: TagsUpdateRequest,
    storage=Depends(get_storage),
) -> TagsUpdateResponse:
    """Replace the tags on a chunk with the given list."""
    from datetime import datetime, timezone

    chunk = await storage.get_chunk(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    deduped = list(dict.fromkeys(body.tags))

    new_meta = chunk.metadata.__class__(
        source_file=chunk.metadata.source_file,
        heading_hierarchy=chunk.metadata.heading_hierarchy,
        chunk_type=chunk.metadata.chunk_type,
        start_line=chunk.metadata.start_line,
        end_line=chunk.metadata.end_line,
        language=chunk.metadata.language,
        tags=tuple(deduped),
        namespace=chunk.metadata.namespace,
    )
    from memtomem.models import Chunk as _Chunk

    updated = _Chunk(
        content=chunk.content,
        metadata=new_meta,
        id=chunk.id,
        content_hash=chunk.content_hash,
        embedding=chunk.embedding,
        created_at=chunk.created_at,
        updated_at=datetime.now(timezone.utc),
    )
    await storage.upsert_chunks([updated])
    return TagsUpdateResponse(id=str(chunk_id), tags=deduped)


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
    raw = await storage.dense_search(embedding, top_k=top_k + 1)

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
