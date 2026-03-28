"""Search-related schemas."""

from __future__ import annotations

from pydantic import BaseModel

from memtomem.web.schemas.core import SearchResultOut, RetrievalStatsOut


class SearchResponse(BaseModel):
    results: list[SearchResultOut]
    total: int
    retrieval_stats: RetrievalStatsOut | None = None


class SimilarChunksResponse(BaseModel):
    results: list[SearchResultOut]
    total: int
