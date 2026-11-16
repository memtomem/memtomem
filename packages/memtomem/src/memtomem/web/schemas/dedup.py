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
    # Chunks the scan actually selected (at most the requested ``max_scan``).
    scanned_chunks: int
    # Chunks the pool was selected from. When it is larger than
    # ``scanned_chunks`` the scan covered the first ``scanned_chunks`` of them
    # in chunk-id order.
    eligible_chunks: int = 0
    near_search_enabled: bool = True
    probed_chunks: int = 0
    chunks_without_vector: int = 0


class MergeRequest(BaseModel):
    keep_id: str
    delete_ids: list[str]


class MergeResponse(BaseModel):
    deleted: int
    kept_id: str
