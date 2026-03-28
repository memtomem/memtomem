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
    q: str = Query(..., description="Search query"),
    top_k: int = Query(10, ge=1, le=100),
    source_filter: str | None = Query(None),
    tag_filter: str | None = Query(None),
    namespace: str | None = Query(None),
    pipeline=Depends(get_search_pipeline),
) -> SearchResponse:
    try:
        results, rstats = await pipeline.search(
            query=q,
            top_k=top_k,
            source_filter=source_filter,
            tag_filter=tag_filter,
            namespace=namespace,
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
