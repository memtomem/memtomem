"""Procedure memory endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from memtomem.models import InvalidFilterSyntaxError, NamespaceFilter
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
    namespace: str | None = Query(
        None, description="Namespace filter; omitted hides system namespaces"
    ),
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> ProceduresListResponse:
    """List procedure-tagged chunks.

    Without ``namespace`` the system namespaces stay hidden, as they do for
    ``mem_procedure_list`` (#3117). Naming one, for example an agent's
    ``agent-runtime:<id>``, reads it.
    """
    # A blank value names no namespace. Parsed as given it would be an exact
    # namespace ``""`` that matches no row, so it takes the omitted path, as in
    # ``GET /api/search``.
    try:
        ns_filter = NamespaceFilter.parse(
            namespace or None,
            system_prefixes=tuple(config.search.system_namespace_prefixes),
        )
    except InvalidFilterSyntaxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    boundary = _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)
    chunks = await storage.recall_chunks(
        tag_filter="procedure",
        limit=limit,
        namespace_filter=ns_filter,
        project_context_root=boundary,
    )
    procedures = [chunk_to_out(c) for c in chunks if "procedure" in c.metadata.tags]
    return ProceduresListResponse(procedures=procedures, total=len(procedures))
