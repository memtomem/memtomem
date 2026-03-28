"""Decay-related schemas."""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "DecayScanResponse",
    "ExpireRequest",
    "ExpireResponse",
]


class DecayScanResponse(BaseModel):
    total_chunks: int
    expired_chunks: int
    dry_run: bool = True


class ExpireRequest(BaseModel):
    max_age_days: float = 90.0
    source_filter: str | None = None
    dry_run: bool = True


class ExpireResponse(BaseModel):
    total_chunks: int
    expired_chunks: int
    deleted_chunks: int
    dry_run: bool
