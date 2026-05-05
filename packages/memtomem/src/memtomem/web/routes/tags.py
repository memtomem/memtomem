"""Tag listing, auto-tag extraction, and tag-management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.services import tag_management as tag_svc
from memtomem.tools.auto_tag import auto_tag_storage
from memtomem.web.deps import get_search_pipeline, get_storage
from memtomem.web.schemas.tags import (
    AutoTagRequest,
    AutoTagResponse,
    AutoTagSample,
    TagCount,
    TagMergeRequest,
    TagOpResponse,
    TagOpSampleOut,
    TagRenameRequest,
    TagsListResponse,
)

_DRY_RUN_SAMPLE_CAP = 10

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
    """Auto-extract keyword tags for chunks. Set dry_run=false to persist tags.

    During a dry run the route asks ``auto_tag_storage`` for up to
    ``_DRY_RUN_SAMPLE_CAP`` previews so the Tags-tab UI can show the
    user what the apply path would actually write. Real-apply runs
    skip sampling — the chunks have already been mutated, so a preview
    on the response is just bytes the UI doesn't need.
    """
    sample_limit = _DRY_RUN_SAMPLE_CAP if body.dry_run else 0
    stats = await auto_tag_storage(
        storage,
        source_filter=body.source_filter,
        max_tags=body.max_tags,
        overwrite=body.overwrite,
        dry_run=body.dry_run,
        sample_limit=sample_limit,
    )
    return AutoTagResponse(
        total_chunks=stats.total_chunks,
        tagged_chunks=stats.tagged_chunks,
        skipped_chunks=stats.skipped_chunks,
        dry_run=body.dry_run,
        samples=[
            AutoTagSample(
                chunk_id=s.chunk_id,
                source_file=s.source_file,
                content_preview=s.content_preview,
                current_tags=list(s.current_tags),
                suggested_tags=list(s.suggested_tags),
            )
            for s in stats.samples
        ],
    )


def _to_response(result: tag_svc.TagOpResult) -> TagOpResponse:
    return TagOpResponse(
        tag=result.tag,
        affected_chunks=result.affected_chunks,
        dry_run=result.dry_run,
        samples=[
            TagOpSampleOut(
                chunk_id=str(s.chunk_id),
                source_file=s.source_file,
                content_preview=s.content_preview,
                current_tags=list(s.current_tags),
            )
            for s in result.samples
        ],
    )


@router.put("/{name}", response_model=TagOpResponse)
async def rename_tag(
    name: str,
    body: TagRenameRequest,
    dry_run: bool = Query(False),
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
) -> TagOpResponse:
    """Rename ``name`` to ``body.new_name`` across every chunk that carries it.

    With ``dry_run=true`` the response carries the count + up to 10 sample
    chunk previews so the UI can show a confirmation modal before the
    apply call.
    """
    try:
        result = await tag_svc.rename_tag(
            storage,
            name,
            body.new_name,
            dry_run=dry_run,
            search_pipeline=search_pipeline,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(result)


@router.delete("/{name}", response_model=TagOpResponse)
async def delete_tag(
    name: str,
    dry_run: bool = Query(False),
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
) -> TagOpResponse:
    """Drop ``name`` from every chunk that carries it. Chunks ending up
    tag-less stay indexed."""
    try:
        result = await tag_svc.delete_tag(
            storage, name, dry_run=dry_run, search_pipeline=search_pipeline
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(result)


@router.post("/merge", response_model=TagOpResponse)
async def merge_tags(
    body: TagMergeRequest,
    dry_run: bool = Query(False),
    storage=Depends(get_storage),
    search_pipeline=Depends(get_search_pipeline),
) -> TagOpResponse:
    """Replace every tag in ``body.sources`` with ``body.target`` across all
    chunks. Resulting per-chunk tag list is deduplicated."""
    try:
        result = await tag_svc.merge_tags(
            storage,
            body.sources,
            body.target,
            dry_run=dry_run,
            search_pipeline=search_pipeline,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(result)
