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

    Mirrors the observation snapshot fields (source basename, hash). Two
    distinct guarantees, at two layers — don't conflate them:

    * *Which* fields appear is bounded **here**. The field list below is a
      **deliberate allowlist, not a permissive passthrough**: Pydantic's
      default ``extra="ignore"`` is load-bearing, so any key the snapshot
      writer grows later (#1799/#1800 surface) is dropped until it is added
      here on purpose — the moment to make the privacy call. That stops a
      future writer regression that adds a stray ``content`` or
      absolute-path *key* from auto-surfacing. ``test_snapshot_out_is_an_allowlist``
      pins the drop so it can't happen by accident (#1812).
    * The *values* of the allowed fields (``source_name`` is a basename,
      never an absolute path or raw text) are guaranteed **upstream** by the
      writer's projection — ``search/pipeline.py`` snapshots
      ``chunk.metadata.source_file.name``, never the full path or content.
      This model relies on that projection; it does not re-sanitize field
      contents, so it is not the place to catch a writer that mislabels a
      value under an already-allowed key.
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
