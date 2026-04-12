"""Timeline endpoint — chronological chunk browser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from memtomem.models import NamespaceFilter
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
    chunks = await storage.recall_chunks(
        since=since,
        source_filter=source,
        limit=limit,
        namespace_filter=ns_filter,
    )
    out = [chunk_to_out(c) for c in chunks]
    return TimelineResponse(chunks=out, total=len(out))
