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
    # Scannable chunks in the store. The scan covered ``scanned_chunks`` of
    # them, in chunk-id order: the first ones, or the ones after ``after``.
    eligible_chunks: int = 0
    near_search_enabled: bool = True
    probed_chunks: int = 0
    chunks_without_vector: int = 0
    # Pairs left out because both members are read-only chunks of one source
    # file (#3418); not in ``candidates`` and not counted in ``total``.
    same_source_read_only_pairs: int = 0
    # Pairs the scan kept before ``offset`` and ``limit``. Above
    # ``offset + total``, the rest are not reached by ``next_after``: scan
    # again from the same ``after`` with a higher ``limit`` or ``offset``.
    pairs_found: int = 0
    # The pairs of the pool skipped before ``candidates``.
    offset: int = 0
    # The cursor this scan was given, as the chunk id it names.
    after: str | None = None
    # Scannable chunks after the last one scanned; 0 at the end of the store.
    remaining_chunks: int = 0
    # The ``after`` that continues from this scan; null at the end.
    next_after: str | None = None


class MergeRequest(BaseModel):
    keep_id: str
    delete_ids: list[str]


class MergeResponse(BaseModel):
    deleted: int
    kept_id: str
