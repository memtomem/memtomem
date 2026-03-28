"""Memory evaluation / health report endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from memtomem.web.deps import get_storage

router = APIRouter(prefix="/eval", tags=["evaluation"])


@router.get("")
async def get_eval_report(
    namespace: str | None = Query(None),
    storage=Depends(get_storage),
) -> dict:
    """Return a memory health report."""
    return await storage.get_health_report(namespace=namespace)
