"""Search endpoint."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.models import InvalidFilterSyntaxError, NamespaceFilter, ScopeFilter
from memtomem.services.search_service import validate_scope_vocabulary
from memtomem.web.deps import get_project_context_root, get_search_pipeline
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
    scope: str | None = Query(
        None,
        description=(
            "ADR-0011 tier filter — a value, a comma list (user,project_local) "
            "or a glob (project_*), not both. Omitted, the default merge "
            "applies: inside a project, user plus that project's tiers; "
            "outside one, user only. project_shared from outside a project "
            "is a deliberate cross-project search"
        ),
    ),
    context_window: int = Query(
        0,
        ge=0,
        le=10,
        description=(
            "Expand ±N adjacent chunks. Neighbours obey visibility rules "
            "(hidden namespaces, project scope, validity) but not the "
            "tag/type/date selection filters"
        ),
    ),
    pipeline=Depends(get_search_pipeline),
    project_context_root=Depends(get_project_context_root),
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

    # An empty query param is a caller that emitted its declared params without
    # filling them in — "unset", not "a filter that matches nothing". Neither
    # parser reads it that way: ``parse("")`` yields ``scopes=("",)``, which the
    # SQL emits as ``scope IN ('')`` and answers 200 with zero rows, so the
    # normalization has to happen before the parse. ``scope`` gets its own
    # normalization from the shared validator below; ``namespace`` is left
    # unstripped because its alphabet is open, so leading or trailing space
    # could belong to a namespace someone actually indexed.
    namespace = namespace or None

    # Parse the namespace here rather than letting the pipeline do it: the
    # ``except Exception`` around the search call below turns anything raised
    # there into a 500, and a filter the caller spelled wrong is a request
    # problem, not a server fault.
    # ``validate_scope_vocabulary`` is the surface-independent half: the same
    # spelling has to mean the same thing here, in ``mem_search`` and in
    # ``mm search``, and it returns the value the pipeline should receive.
    try:
        NamespaceFilter.parse(namespace)
        scope = validate_scope_vocabulary(scope)
        ScopeFilter.parse(scope)
    except InvalidFilterSyntaxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
            scope=scope,
            context_window=context_window if context_window > 0 else None,
            project_context_root=project_context_root,
            origin="web",
        )
    except Exception as exc:
        logger.error("Search failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed") from exc
    out = [to_result_out(r) for r in results]
    return SearchResponse(
        results=out,
        total=len(out),
        query_run_id=rstats.query_run_id,
        retrieval_stats=RetrievalStatsOut(**vars(rstats)),
    )
