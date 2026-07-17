"""Quality Lab search-run inspection and relevance feedback endpoints (#1801).

Dev-only surface (see ``_DEV_ONLY_ROUTERS``): list observed runs, inspect
one run's observation metadata and ranked snapshot, and attach relevance
judgments. Validation lives in storage; the app-level handlers translate
``KeyError``→404 and ``ValueError``→400, so this router only maps the
explicit-replacement conflict to 409.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.errors import FeedbackConflictError
from memtomem.web.deps import get_storage
from memtomem.web.schemas.search_runs import (
    FeedbackIn,
    FeedbackOut,
    SearchRunDetailResponse,
    SearchRunListResponse,
    SearchRunSummary,
    SnapshotEntryOut,
)

router = APIRouter(prefix="/search/runs", tags=["search-runs"])


@router.get("", response_model=SearchRunListResponse)
async def list_search_runs(
    limit: int = Query(50, ge=1, le=200),
    since: str | None = Query(None, description="ISO timestamp filter"),
    storage=Depends(get_storage),
) -> SearchRunListResponse:
    """Newest-first summaries of observed search runs."""
    rows = await storage.get_search_runs(limit=limit, since=since)
    runs = [SearchRunSummary(**r) for r in rows]
    return SearchRunListResponse(runs=runs, total=len(runs))


@router.get("/{run_id}", response_model=SearchRunDetailResponse)
async def get_search_run(
    run_id: str,
    storage=Depends(get_storage),
) -> SearchRunDetailResponse:
    """One run: query, observation metadata, ranked snapshot + judgments."""
    run = await storage.get_search_run(run_id)
    judgments = {
        j["chunk_id"]: j for j in await storage.get_search_feedback(run_id)
    }
    results = []
    for entry in run["result_snapshot"]:
        judgment = judgments.get(entry.get("chunk_id"))
        results.append(
            SnapshotEntryOut(
                **entry,
                judgment=judgment["judgment"] if judgment else None,
                feedback_updated_at=judgment["updated_at"] if judgment else None,
            )
        )
    return SearchRunDetailResponse(
        run_id=run["run_id"],
        query_text=run["query_text"],
        created_at=run["created_at"],
        observation=run["observation"],
        results=results,
    )


@router.post("/{run_id}/feedback", response_model=FeedbackOut)
async def save_search_feedback(
    run_id: str,
    body: FeedbackIn,
    storage=Depends(get_storage),
) -> FeedbackOut:
    """Record one relevance judgment for a snapshotted result."""
    try:
        saved = await storage.save_search_feedback(
            run_id, body.chunk_id, body.judgment, replace=body.replace
        )
    except FeedbackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FeedbackOut(**saved)
