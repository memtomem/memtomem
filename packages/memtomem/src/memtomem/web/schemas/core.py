"""Core schemas used across multiple routes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChunkOut(BaseModel):
    id: str
    content: str
    source_file: str
    chunk_type: str
    start_line: int
    end_line: int
    heading_hierarchy: list[str]
    tags: list[str]
    namespace: str = "default"
    created_at: datetime
    updated_at: datetime


class ContextInfoOut(BaseModel):
    window_before: list[ChunkOut] = []
    window_after: list[ChunkOut] = []
    chunk_position: int = 0
    total_chunks_in_file: int = 0


class SearchResultOut(BaseModel):
    chunk: ChunkOut
    score: float
    rank: int
    source: str
    context: ContextInfoOut | None = None


class RetrievalStatsOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bm25_candidates: int = 0
    dense_candidates: int = 0
    fused_total: int = 0
    final_total: int = 0
    bm25_error: str | None = None


class DeleteResponse(BaseModel):
    deleted: int


def chunk_to_out(chunk) -> ChunkOut:
    """Convert a domain Chunk to ChunkOut schema."""
    meta = chunk.metadata
    return ChunkOut(
        id=str(chunk.id),
        content=chunk.content,
        source_file=str(meta.source_file),
        chunk_type=meta.chunk_type,
        start_line=meta.start_line,
        end_line=meta.end_line,
        heading_hierarchy=list(meta.heading_hierarchy),
        tags=list(meta.tags),
        namespace=meta.namespace,
        created_at=chunk.created_at,
        updated_at=chunk.updated_at,
    )


def to_result_out(r) -> SearchResultOut:
    """Convert a domain SearchResult to SearchResultOut schema."""
    ctx_out = None
    ctx = getattr(r, "context", None)
    if ctx and (ctx.window_before or ctx.window_after):
        ctx_out = ContextInfoOut(
            window_before=[chunk_to_out(c) for c in ctx.window_before],
            window_after=[chunk_to_out(c) for c in ctx.window_after],
            chunk_position=ctx.chunk_position,
            total_chunks_in_file=ctx.total_chunks_in_file,
        )
    return SearchResultOut(
        chunk=chunk_to_out(r.chunk),
        score=r.score,
        rank=r.rank,
        source=r.source,
        context=ctx_out,
    )
