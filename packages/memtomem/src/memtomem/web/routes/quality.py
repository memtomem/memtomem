"""Quality Lab eval-case + replay endpoints (#1802, PR-5).

Dev-only surface (see ``_DEV_ONLY_ROUTERS``): list evaluation cases, promote a
labeled search run into a durable case, and replay cases into a deterministic
retrieval-quality report. Advisory only; replay reports are ephemeral (run
live, returned as JSON — never persisted) and content-free by construction.

Error mapping is router-local: :class:`EvalCaseNotFoundError` -> 404, every
other :class:`EvalCaseError` (no feedback, unreplayable filters, project scope,
name collision) -> 409, mirroring the ``FeedbackConflictError`` -> 409
precedent in ``search_runs``. Classification is by exception type, never by
message text (messages interpolate user-controlled names).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.errors import EvalCaseError, EvalCaseNotFoundError
from memtomem.quality.replay import replay_cases
from memtomem.quality.state import current_fingerprints
from memtomem.web.deps import get_config, get_search_pipeline, get_storage
from memtomem.web.schemas.quality import (
    EvalCaseListResponse,
    EvalCaseSummary,
    PromoteCaseIn,
    PromoteCaseOut,
    ReplayIn,
)

router = APIRouter(prefix="/quality", tags=["quality"])


def _eval_case_http(exc: EvalCaseError) -> HTTPException:
    """404 for missing run/case, 409 for every other eval-case refusal."""
    status = 404 if isinstance(exc, EvalCaseNotFoundError) else 409
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/cases", response_model=EvalCaseListResponse)
async def list_quality_cases(
    status: str | None = Query(None, pattern="^(active|archived)$"),
    storage=Depends(get_storage),
) -> EvalCaseListResponse:
    """Newest-first evaluation-case summaries, optionally filtered by status."""
    rows = await storage.list_eval_cases(status=status)
    cases = [EvalCaseSummary(**r) for r in rows]
    return EvalCaseListResponse(cases=cases, total=len(cases))


@router.post("/cases", response_model=PromoteCaseOut)
async def promote_quality_case(
    body: PromoteCaseIn,
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> PromoteCaseOut:
    """Promote a labeled search run into a durable evaluation case.

    Unlike ``mm quality promote`` (which allows unnamed cases), the web surface
    defaults an omitted name to ``run-<full run_id>`` — the full id, never a
    prefix, so two runs sharing an 8-char prefix don't collide in the globally
    UNIQUE name index. Re-promoting the same run then hits that constraint and
    returns 409.
    """
    name = body.name or f"run-{body.run_id}"
    fingerprints, _ = current_fingerprints(storage, config)
    try:
        case = await storage.promote_search_run(
            body.run_id,
            name=name,
            fingerprints=fingerprints,
            allow_unreplayable_filters=body.allow_unreplayable_filters,
        )
    except EvalCaseError as exc:
        raise _eval_case_http(exc) from exc
    return PromoteCaseOut(
        case_id=case["case_id"],
        name=case["name"],
        label_count=len(case["labels"]),
    )


@router.post("/replay")
async def run_quality_replay(
    body: ReplayIn,
    storage=Depends(get_storage),
    pipeline=Depends(get_search_pipeline),
    config=Depends(get_config),
) -> dict:
    """Replay evaluation cases into a deterministic report (no side effects)."""
    try:
        return await replay_cases(
            storage,
            pipeline,
            config,
            case_ids=body.cases or None,
            as_of_unix=body.as_of_unix,
        )
    except EvalCaseError as exc:
        raise _eval_case_http(exc) from exc
