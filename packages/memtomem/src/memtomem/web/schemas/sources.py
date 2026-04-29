"""Source and chunk-list schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from memtomem.config import MemoryDirKind
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
    # The configured ``memory_dir`` that contains this source, expanded
    # to an absolute path. ``None`` for orphan sources whose owning dir
    # was unregistered after indexing — they still appear in the General
    # view so users can prune or re-register them.
    memory_dir: str | None = None
    # ``"memory"`` for agent / user-memory dirs (auto-classified by path
    # pattern) and ``"general"`` for arbitrary indexed folders. ``None``
    # for orphans (no owning dir to classify). Drives the Sources page's
    # Memory / General sub-toggle.
    kind: MemoryDirKind | None = None


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
