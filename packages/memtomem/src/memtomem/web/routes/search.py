"""Search endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.web.deps import get_search_pipeline
from memtomem.web.schemas.core import RetrievalStatsOut, to_result_out
from memtomem.web.schemas.search import SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str | None = Query(None, description="Search query", max_length=10_000),
    top_k: int | None = Query(None, ge=1, le=500),
    source_filter: str | None = Query(None),
    tag_filter: str | None = Query(None),
    namespace: str | None = Query(None),
    context_window: int = Query(0, ge=0, le=10, description="Expand ±N adjacent chunks"),
    pipeline=Depends(get_search_pipeline),
) -> SearchResponse:
    # #750: ``q`` is optional so a tag/source-only search (no keyword)
    # is a first-class path. The pipeline handles the empty-query branch
    # (filter becomes the primary selector); the API guard here only
    # rejects "no axis at all" — search needs *something* to scope by.
    q = (q or "").strip()
    if not q and not (tag_filter or source_filter):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of q, tag_filter, or source_filter.",
        )

    try:
        results, rstats = await pipeline.search(
            query=q,
            top_k=top_k,
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace=namespace,
            context_window=context_window if context_window > 0 else None,
        )
    except Exception as exc:
        logger.error("Search failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed") from exc
    out = [to_result_out(r) for r in results]
    return SearchResponse(
        results=out,
        total=len(out),
        retrieval_stats=RetrievalStatsOut(**vars(rstats)),
    )
