"""Deduplication schemas."""

from __future__ import annotations

from pydantic import BaseModel

from memtomem.web.schemas.core import ChunkOut

__all__ = [
    "DedupCandidateOut",
    "DedupScanResponse",
    "MergeRequest",
    "MergeResponse",
]


class DedupCandidateOut(BaseModel):
    chunk_a: ChunkOut
    chunk_b: ChunkOut
    score: float
    exact: bool


class DedupScanResponse(BaseModel):
    candidates: list[DedupCandidateOut]
    total: int
    scanned_chunks: int


class MergeRequest(BaseModel):
    keep_id: str
    delete_ids: list[str]


class MergeResponse(BaseModel):
    deleted: int
    kept_id: str
