"""Schemas for the Quality Lab search-run inspection surface (#1801)."""

from __future__ import annotations

from pydantic import BaseModel


class SearchRunSummary(BaseModel):
    run_id: str
    query_text: str
    created_at: str
    result_count: int
    origin: str | None = None
    feedback_count: int


class SearchRunListResponse(BaseModel):
    runs: list[SearchRunSummary]
    total: int


class SnapshotEntryOut(BaseModel):
    """One snapshotted result row, merged with its current judgment.

    Mirrors the observation snapshot fields (source basename, hash — never
    content or absolute paths) and stays permissive on optional metadata so
    older snapshots render too.
    """

    chunk_id: str
    rank: int | None = None
    score: float | None = None
    source_name: str | None = None
    content_hash: str | None = None
    heading_hierarchy: list[str] | None = None
    namespace: str | None = None
    language: str | None = None
    judgment: str | None = None
    feedback_updated_at: str | None = None


class SearchRunDetailResponse(BaseModel):
    run_id: str
    query_text: str
    created_at: str
    observation: dict
    results: list[SnapshotEntryOut]


class FeedbackIn(BaseModel):
    """Judgment vocabulary stays a plain ``str`` here on purpose: the
    closed-set validation lives in storage so the MCP tool and this API
    emit the identical error message."""

    chunk_id: str
    judgment: str
    replace: bool = False


class FeedbackOut(BaseModel):
    run_id: str
    chunk_id: str
    judgment: str
    created_at: str
    updated_at: str
    created: bool
    replaced: bool
