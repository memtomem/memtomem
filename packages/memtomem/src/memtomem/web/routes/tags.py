"""Tag listing and automatic tag extraction endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from memtomem.tools.auto_tag import auto_tag_storage
from memtomem.web.deps import get_storage
from memtomem.web.schemas.tags import AutoTagRequest, AutoTagResponse, TagCount, TagsListResponse

router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("", response_model=TagsListResponse)
async def list_tags(
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    storage=Depends(get_storage),
) -> TagsListResponse:
    """Return all unique tags across the knowledge base with occurrence counts."""
    tag_counts = await storage.get_tag_counts()
    all_tags = [TagCount(tag=t, count=c) for t, c in tag_counts]
    total = len(all_tags)
    page = all_tags[offset : offset + limit]
    return TagsListResponse(tags=page, total=total, offset=offset, limit=limit)


@router.post("/auto", response_model=AutoTagResponse)
async def run_auto_tag(
    body: AutoTagRequest,
    storage=Depends(get_storage),
) -> AutoTagResponse:
    """Auto-extract keyword tags for chunks. Set dry_run=false to persist tags."""
    stats = await auto_tag_storage(
        storage,
        source_filter=body.source_filter,
        max_tags=body.max_tags,
        overwrite=body.overwrite,
        dry_run=body.dry_run,
    )
    return AutoTagResponse(
        total_chunks=stats.total_chunks,
        tagged_chunks=stats.tagged_chunks,
        skipped_chunks=stats.skipped_chunks,
        dry_run=body.dry_run,
    )
