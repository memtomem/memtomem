"""Procedure memory endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from memtomem.runtime.project_context import _resolve_project_context_from_dirs
from memtomem.web.deps import get_config, get_storage
from memtomem.web.schemas.core import ChunkOut, chunk_to_out

router = APIRouter(prefix="/procedures", tags=["procedures"])


class ProceduresListResponse(BaseModel):
    procedures: list[ChunkOut]
    total: int


@router.get("", response_model=ProceduresListResponse)
async def list_procedures(
    limit: int = Query(100, ge=1, le=500),
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> ProceduresListResponse:
    """List procedure-tagged chunks."""
    boundary = _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)
    chunks = await storage.recall_chunks(
        tag_filter="procedure",
        limit=limit,
        project_context_root=boundary,
    )
    procedures = [chunk_to_out(c) for c in chunks if "procedure" in c.metadata.tags]
    return ProceduresListResponse(procedures=procedures, total=len(procedures))
