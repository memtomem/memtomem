"""Memory evaluation / health report endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from memtomem.web.deps import get_project_context_root, get_storage

router = APIRouter(prefix="/eval", tags=["evaluation"])


@router.get("")
async def get_eval_report(
    namespace: str | None = Query(None),
    storage=Depends(get_storage),
    project_context_root=Depends(get_project_context_root),
) -> dict:
    """Return a memory health report."""
    return await storage.get_health_report(
        namespace=namespace, project_context_root=project_context_root
    )
