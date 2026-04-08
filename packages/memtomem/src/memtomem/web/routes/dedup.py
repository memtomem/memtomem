"""Deduplication scan and merge endpoints."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from memtomem.web.deps import get_dedup_scanner
from memtomem.web.schemas import (
    DedupCandidateOut,
    DedupScanResponse,
    MergeRequest,
    MergeResponse,
)
from memtomem.web.schemas.core import chunk_to_out

router = APIRouter(prefix="/dedup", tags=["dedup"])

_DEDUP_SCAN_TIMEOUT = 30  # seconds


@router.get("/candidates", response_model=DedupScanResponse)
async def scan_duplicates(
    threshold: float = 0.92,
    limit: int = 100,
    max_scan: int = 500,
    dedup_scanner=Depends(get_dedup_scanner),
) -> DedupScanResponse:
    """Scan for duplicate chunk candidates (dry-run, no mutations)."""
    try:
        candidates = await asyncio.wait_for(
            dedup_scanner.scan(threshold=threshold, limit=limit, max_scan=max_scan),
            timeout=_DEDUP_SCAN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=408,
            detail=f"Dedup scan timed out after {_DEDUP_SCAN_TIMEOUT}s. "
            f"Try reducing max_scan (current: {max_scan}).",
        )
    out = [
        DedupCandidateOut(
            chunk_a=chunk_to_out(c.chunk_a),
            chunk_b=chunk_to_out(c.chunk_b),
            score=c.score,
            exact=c.exact,
        )
        for c in candidates
    ]
    return DedupScanResponse(candidates=out, total=len(out), scanned_chunks=max_scan)


@router.post("/merge", response_model=MergeResponse)
async def merge_duplicates(
    body: MergeRequest,
    dedup_scanner=Depends(get_dedup_scanner),
) -> MergeResponse:
    """Merge duplicates: keep *keep_id* chunk, delete *delete_ids* chunks."""
    try:
        keep_uuid = UUID(body.keep_id)
        delete_uuids = [UUID(d) for d in body.delete_ids]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {exc}") from exc

    deleted = await dedup_scanner.merge(keep_uuid, delete_uuids)
    return MergeResponse(deleted=deleted, kept_id=body.keep_id)
