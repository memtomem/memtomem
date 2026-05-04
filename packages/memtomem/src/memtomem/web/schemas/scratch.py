"""Working memory (scratch) schemas."""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "ScratchEntryOut",
    "ScratchListResponse",
    "ScratchSetRequest",
    "ScratchSetResponse",
    "ScratchDeleteResponse",
    "ScratchPromoteRequest",
    "ScratchPromoteResponse",
]


class ScratchEntryOut(BaseModel):
    key: str
    value: str
    session_id: str | None = None
    created_at: str
    expires_at: str | None = None
    promoted: bool = False


class ScratchListResponse(BaseModel):
    entries: list[ScratchEntryOut]
    total: int


class ScratchSetRequest(BaseModel):
    key: str
    value: str
    ttl_minutes: int | None = None
    session_id: str | None = None


class ScratchSetResponse(BaseModel):
    key: str
    status: str = "ok"


class ScratchDeleteResponse(BaseModel):
    key: str
    deleted: bool


class ScratchPromoteRequest(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    file: str | None = None
    # Bypass the trust-boundary redaction guard when promoting a
    # scratch entry into long-term memory. Default False — the
    # promotion flow asks the user to confirm before retrying.
    force_unsafe: bool = False


class ScratchPromoteResponse(BaseModel):
    key: str
    promoted: bool
    file: str | None = None
    indexed_chunks: int = 0
