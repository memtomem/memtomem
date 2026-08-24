"""Decay scan and TTL expiry endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from memtomem.search.decay import expire_chunks
from memtomem.web.deps import get_search_pipeline, get_storage
from memtomem.web.schemas import DecayScanResponse, ExpireRequest, ExpireResponse

router = APIRouter(prefix="/decay", tags=["decay"])


@router.get("/scan", response_model=DecayScanResponse)
async def scan_expired(
    max_age_days: float = 90.0,
    source_filter: str | None = None,
    storage=Depends(get_storage),
) -> DecayScanResponse:
    """Preview chunks that would be expired (dry-run, no mutations)."""
    stats = await expire_chunks(
        storage,
        max_age_days=max_age_days,
        dry_run=True,
        source_filter=source_filter,
    )
    return DecayScanResponse(
        total_chunks=stats.total_chunks,
        expired_chunks=stats.expired_chunks,
        dry_run=True,
    )


@router.post("/expire", response_model=ExpireResponse)
async def expire_old_chunks(
    body: ExpireRequest,
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
) -> ExpireResponse:
    """Expire (delete) chunks older than max_age_days. Set dry_run=false to actually delete."""
    if body.dry_run:
        stats = await expire_chunks(
            storage,
            max_age_days=body.max_age_days,
            dry_run=True,
            source_filter=body.source_filter,
        )
    else:
        stats = await expire_chunks(
            storage,
            max_age_days=body.max_age_days,
            dry_run=False,
            source_filter=body.source_filter,
        )
        if stats.deleted_chunks > 0:
            # Same gate as the MCP twin ``mem_decay_expire`` (#2157). The
            # single ``delete_chunks`` call this route can reach rolls itself
            # back on failure, so a raised expiry committed nothing and a run
            # that found no expired chunks wrote nothing — neither needs the
            # cache dropped, and ``invalidate_cache`` also clears the LLM
            # query-expansion cache.
            search_pipeline.invalidate_cache()
    return ExpireResponse(
        total_chunks=stats.total_chunks,
        expired_chunks=stats.expired_chunks,
        deleted_chunks=stats.deleted_chunks,
        dry_run=body.dry_run,
    )
