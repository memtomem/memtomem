"""Session (episodic memory) endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from memtomem.web.deps import get_storage
from memtomem.web.schemas.sessions import (
    SessionEventsResponse,
    SessionEventOut,
    SessionOut,
    SessionsListResponse,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("", response_model=SessionsListResponse)
async def list_sessions(
    agent_id: str | None = Query(None),
    since: str | None = Query(None, description="ISO date filter"),
    limit: int = Query(50, ge=1, le=500),
    storage=Depends(get_storage),
) -> SessionsListResponse:
    """List episodic memory sessions."""
    rows = await storage.list_sessions(agent_id=agent_id, since=since, limit=limit)
    sessions = [SessionOut(**r) for r in rows]
    return SessionsListResponse(sessions=sessions, total=len(sessions))


@router.get("/{session_id}/events", response_model=SessionEventsResponse)
async def get_session_events(
    session_id: str,
    storage=Depends(get_storage),
) -> SessionEventsResponse:
    """Get events for a specific session."""
    events = await storage.get_session_events(session_id)
    out = [SessionEventOut(**e) for e in events]
    return SessionEventsResponse(session_id=session_id, events=out, total=len(out))
