"""Web API routes for health watchdog."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/watchdog", tags=["watchdog"])


def _get_watchdog(request: Request):
    return getattr(request.app.state, "health_watchdog", None)


@router.get("/status")
async def watchdog_status(request: Request) -> JSONResponse:
    """Current health watchdog status and latest check results."""
    wd = _get_watchdog(request)
    if wd is None:
        return JSONResponse({"enabled": False, "message": "watchdog not configured"})
    return JSONResponse(wd.get_status())


@router.get("/history")
async def watchdog_history(
    request: Request,
    check: str = Query(..., description="Check name"),
    hours: float = Query(24.0, ge=0.1, le=168.0),
) -> JSONResponse:
    """Historical health snapshots for a specific check."""
    wd = _get_watchdog(request)
    if wd is None:
        return JSONResponse({"enabled": False})
    return JSONResponse(wd.get_trends(check, hours))


@router.post("/run")
async def watchdog_run_now(request: Request) -> JSONResponse:
    """Force an immediate health check run."""
    wd = _get_watchdog(request)
    if wd is None:
        return JSONResponse({"enabled": False, "message": "watchdog not configured"}, status_code=400)
    results = await wd.run_now()
    return JSONResponse(results)
