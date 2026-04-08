"""Source and chunk-list schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from memtomem.web.schemas.core import ChunkOut


class SourceOut(BaseModel):
    path: str
    chunk_count: int = 0
    last_indexed_at: datetime | None = None
    file_size: int | None = None
    namespaces: list[str] = ["default"]
    avg_tokens: int = 0
    min_tokens: int = 0
    max_tokens: int = 0


class SourcesResponse(BaseModel):
    sources: list[SourceOut]
    total: int = 0
    offset: int = 0
    limit: int = 0


class ChunksListResponse(BaseModel):
    chunks: list[ChunkOut]
    total: int


class EditRequest(BaseModel):
    new_content: str


class ChunkSizeBucket(BaseModel):
    bucket: str
    count: int


class StatsResponse(BaseModel):
    total_chunks: int
    total_sources: int
    chunk_size_distribution: list[ChunkSizeBucket] = []


class TimelineResponse(BaseModel):
    chunks: list[ChunkOut]
    total: int
