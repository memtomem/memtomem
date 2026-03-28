"""Decay scan and TTL expiry endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from memtomem.search.decay import expire_chunks
from memtomem.web.deps import get_storage
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
) -> ExpireResponse:
    """Expire (delete) chunks older than max_age_days. Set dry_run=false to actually delete."""
    stats = await expire_chunks(
        storage,
        max_age_days=body.max_age_days,
        dry_run=body.dry_run,
        source_filter=body.source_filter,
    )
    return ExpireResponse(
        total_chunks=stats.total_chunks,
        expired_chunks=stats.expired_chunks,
        deleted_chunks=stats.deleted_chunks,
        dry_run=body.dry_run,
    )
