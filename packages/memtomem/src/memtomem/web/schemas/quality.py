"""Schemas for the Quality Lab eval-case + replay surface (#1802, PR-5).

Dev-only. ``EvalCaseSummary`` is a deliberate allowlist over
``storage.list_eval_cases`` rows (Pydantic ``extra="ignore"`` is load-bearing:
any key the storage layer grows later is dropped until added here on purpose).

The replay **response** is the raw report dict returned by
``memtomem.quality.replay.replay_cases`` — no response_model, no re-projection.
Privacy is guaranteed upstream by the engine (content hashes/scores/metrics
only, never chunk text or absolute paths; pinned by
``test_quality_replay.py``), so there is nothing to re-sanitize here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class EvalCaseSummary(BaseModel):
    case_id: str
    name: str | None = None
    query_text: str
    top_k: int
    source_run_id: str | None = None
    version: int
    status: str
    created_at: str
    updated_at: str
    label_count: int


class EvalCaseListResponse(BaseModel):
    cases: list[EvalCaseSummary]
    total: int


class PromoteCaseIn(BaseModel):
    run_id: str
    name: str | None = None
    allow_unreplayable_filters: bool = False

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        # A whitespace-only explicit name must not persist (it is reachable by
        # id but never by name); reject it rather than silently storing blanks.
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class PromoteCaseOut(BaseModel):
    ok: bool = True
    case_id: str
    name: str | None
    label_count: int


class ReplayIn(BaseModel):
    cases: list[str] | None = None
    as_of_unix: int | None = Field(None, ge=0)

    @field_validator("cases")
    @classmethod
    def _strip_cases(cls, v: list[str] | None) -> list[str] | None:
        # Parity with the MCP tool: trim selectors, reject blank/non-string
        # entries so `" baseline "` behaves identically on both surfaces.
        if v is None:
            return None
        cleaned: list[str] = []
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("cases must be case ids or names (non-empty strings)")
            cleaned.append(item.strip())
        return cleaned

    @field_validator("as_of_unix", mode="before")
    @classmethod
    def _strict_as_of(cls, v: Any) -> Any:
        # Pydantic's default int coercion accepts True->1 and "0"->0, which
        # diverges from the strict MCP contract. Require a real, non-boolean
        # integer so both surfaces reject the same inputs.
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("as_of_unix must be an integer unix timestamp")
        return v
