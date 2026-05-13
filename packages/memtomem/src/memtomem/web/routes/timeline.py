"""Timeline endpoint — chronological chunk browser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from memtomem.models import NamespaceFilter
from memtomem.server.tools.search import _resolve_project_context_from_dirs
from memtomem.web.deps import get_config, get_storage
from memtomem.web.schemas.core import chunk_to_out
from memtomem.web.schemas import TimelineResponse

router = APIRouter(prefix="/timeline", tags=["timeline"])


@router.get("", response_model=TimelineResponse)
async def get_timeline(
    days: int = Query(30, ge=1, le=365),
    source: str | None = Query(None),
    namespace: str | None = Query(None, description="Namespace filter"),
    limit: int = Query(200, ge=1, le=1000),
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> TimelineResponse:
    """Return chunks created within the last *days* days, newest first."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    ns_filter = NamespaceFilter.parse(
        namespace,
        system_prefixes=tuple(config.search.system_namespace_prefixes),
    )
    # ADR-0011 PR-D round 9: thread project context so Web timeline
    # surfaces project_shared / project_local rows when the server
    # runs inside a registered project. Without this, the always-on
    # scope filter would silently drop project-tier rows from the
    # timeline view.
    project_context_root = _resolve_project_context_from_dirs(config.indexing.project_memory_dirs)
    chunks = await storage.recall_chunks(
        since=since,
        source_filter=source,
        limit=limit + 1,
        namespace_filter=ns_filter,
        project_context_root=project_context_root,
    )
    has_more = len(chunks) > limit
    out = [chunk_to_out(c) for c in chunks[:limit]]
    return TimelineResponse(chunks=out, total=len(out), has_more=has_more)
