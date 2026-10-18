"""Deduplication scan and merge endpoints."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.web.deps import get_dedup_scanner, get_search_pipeline
from memtomem.web.schemas import (
    DedupCandidateOut,
    DedupScanResponse,
    MergeRequest,
    MergeResponse,
)
from memtomem.web.schemas.core import chunk_to_out

router = APIRouter(prefix="/dedup", tags=["dedup"])

_DEDUP_SCAN_TIMEOUT = 120  # seconds


@router.get("/candidates", response_model=DedupScanResponse)
async def scan_duplicates(
    threshold: float = 0.92,
    limit: int = 100,
    max_scan: int = Query(500, ge=1),
    dedup_scanner=Depends(get_dedup_scanner),
) -> DedupScanResponse:
    """Scan for duplicate chunk candidates (dry-run, no mutations)."""
    try:
        candidates, coverage = await asyncio.wait_for(
            dedup_scanner.scan_with_coverage(threshold=threshold, limit=limit, max_scan=max_scan),
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
    return DedupScanResponse(
        candidates=out,
        total=len(out),
        scanned_chunks=coverage.pool,
        near_search_enabled=coverage.near_search_enabled,
        probed_chunks=coverage.probed,
        chunks_without_vector=coverage.without_vector,
    )


@router.post("/merge", response_model=MergeResponse)
async def merge_duplicates(
    body: MergeRequest,
    dedup_scanner=Depends(get_dedup_scanner),
    search_pipeline=Depends(get_search_pipeline),
) -> MergeResponse:
    """Merge duplicates: keep *keep_id* chunk, delete *delete_ids* chunks."""
    try:
        keep_uuid = UUID(body.keep_id)
        delete_uuids = [UUID(d) for d in body.delete_ids]
    except ValueError as exc:
        # Pure rejection: nothing was written, keep the cache (and the LLM
        # query-expansion cache ``invalidate_cache`` would also drop).
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {exc}") from exc

    if not delete_uuids:
        # ``merge`` returns 0 without reading or writing anything.
        return MergeResponse(deleted=0, kept_id=body.keep_id)

    try:
        deleted = await dedup_scanner.merge(keep_uuid, delete_uuids)
    finally:
        # ``merge`` commits the kept chunk's merged tags and the deletes
        # together, so a failed call normally leaves nothing written. The
        # invalidation stays anyway (#2157): it costs a cache rebuild and does
        # not depend on where the failure happened. The MCP twin invalidates
        # on this path too.
        search_pipeline.invalidate_cache()
    return MergeResponse(deleted=deleted, kept_id=body.keep_id)
