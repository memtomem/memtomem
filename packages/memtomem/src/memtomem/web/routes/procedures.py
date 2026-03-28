"""Procedure memory endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from memtomem.web.deps import get_storage
from memtomem.web.schemas.core import ChunkOut, chunk_to_out

router = APIRouter(prefix="/procedures", tags=["procedures"])


class ProceduresListResponse(BaseModel):
    procedures: list[ChunkOut]
    total: int


@router.get("", response_model=ProceduresListResponse)
async def list_procedures(
    limit: int = Query(100, ge=1, le=500),
    storage=Depends(get_storage),
) -> ProceduresListResponse:
    """List procedure-tagged chunks."""
    db = storage._get_db()
    rows = db.execute(
        """
        SELECT * FROM chunks
        WHERE EXISTS (SELECT 1 FROM json_each(tags) WHERE value = 'procedure')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    procedures = [chunk_to_out(storage._row_to_chunk(row)) for row in rows]
    return ProceduresListResponse(procedures=procedures, total=len(procedures))
