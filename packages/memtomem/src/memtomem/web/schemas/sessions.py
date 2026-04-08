"""Session-related schemas."""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "SessionOut",
    "SessionEventOut",
    "SessionsListResponse",
    "SessionEventsResponse",
]


class SessionOut(BaseModel):
    id: str
    agent_id: str
    started_at: str
    ended_at: str | None = None
    summary: str | None = None
    namespace: str


class SessionEventOut(BaseModel):
    event_type: str
    content: str
    chunk_ids: list[str]
    created_at: str


class SessionsListResponse(BaseModel):
    sessions: list[SessionOut]
    total: int


class SessionEventsResponse(BaseModel):
    session_id: str
    events: list[SessionEventOut]
    total: int
