"""Search endpoint."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.server.tools.search import _resolve_project_context_from_dirs
from memtomem.web.deps import get_config, get_search_pipeline
from memtomem.web.schemas.core import RetrievalStatsOut, to_result_out
from memtomem.web.schemas.search import SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str | None = Query(None, description="Search query", max_length=10_000),
    top_k: int | None = Query(None, ge=1, le=500),
    source_filter: str | None = Query(None),
    source_exact: list[str] | None = Query(None),
    chunk_type: list[str] | None = Query(None),
    created_from: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    tag_filter: str | None = Query(None),
    namespace: str | None = Query(None),
    context_window: int = Query(0, ge=0, le=10, description="Expand ±N adjacent chunks"),
    pipeline=Depends(get_search_pipeline),
    config=Depends(get_config),
) -> SearchResponse:
    # #750: ``q`` is optional so a tag/source-only search (no keyword)
    # is a first-class path. The pipeline handles the empty-query branch
    # (filter becomes the primary selector); the API guard here only
    # rejects "no axis at all" — search needs *something* to scope by.
    q = (q or "").strip()
    source_exact = [value.strip() for value in (source_exact or []) if value.strip()]
    chunk_type = [value.strip() for value in (chunk_type or []) if value.strip()]
    for name, value in (("created_from", created_from), ("created_before", created_before)):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    if created_from is not None:
        created_from = created_from.astimezone(UTC)
    if created_before is not None:
        created_before = created_before.astimezone(UTC)
    if created_from is not None and created_before is not None and created_from >= created_before:
        raise HTTPException(status_code=422, detail="created_from must be before created_before")

    if not q and not (
        tag_filter or source_filter or source_exact or chunk_type or created_from or created_before
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide at least one of q, tag_filter, source_filter, source_exact, "
                "chunk_type, created_from, or created_before."
            ),
        )

    # ADR-0011 PR-D round 9: thread project context onto the always-on
    # storage scope filter so a Web UI search session running inside a
    # registered project still surfaces project_shared / project_local
    # rows. ``_resolve_project_context_from_dirs`` reads only the dirs
    # list (the ``app``/``comp`` wrapper isn't available here — the
    # endpoint depends on ``Mem2MemConfig`` directly).
    project_context_root = _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)

    try:
        results, rstats = await pipeline.search(
            query=q,
            top_k=top_k,
            source_filter=source_filter,
            source_exact=source_exact,
            chunk_types=chunk_type,
            created_from=created_from,
            created_before=created_before,
            tag_filter=tag_filter,
            namespace=namespace,
            context_window=context_window if context_window > 0 else None,
            project_context_root=project_context_root,
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
