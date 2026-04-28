"""Whitelisted job kinds for the cron scheduler (P2 Phase A).

Each ``JobSpec`` declares a stable name, a one-line description (used
in Phase B's LLM prompt), a Pydantic v2 ``params_model`` for validating
parameters, and an async ``runner`` that takes ``(app, **params)`` and
returns a small result dict.

Pydantic was chosen over raw jsonschema because:
- memtomem already uses pydantic-settings for ``Mem2MemConfig`` so
  there is no new dependency,
- ``params_model.model_json_schema()`` gives the LLM-facing schema
  for free in Phase B,
- typed kwargs flow naturally into ``runner(**validated.model_dump())``.

The dispatcher (PR-A3) calls ``params_model.model_validate(dict)``
before invoking the runner — adversarial LLM output cannot reach the
runner without passing schema validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

# expire_chunks lives in the search package; importing at module top is
# safe — there is no scheduler ↔ search cycle.
from memtomem.search.decay import expire_chunks

if TYPE_CHECKING:
    from memtomem.server.context import AppContext


JobRunStatus = Literal["ok", "error", "timeout"]
"""Status enum for ``ScheduleMixin.schedule_mark_run``."""

JobResult = dict[str, Any]
"""Runner return shape — small JSON-serializable summary."""


@dataclass(frozen=True)
class JobSpec:
    """Schema + runner for one schedulable job kind."""

    name: str
    description: str
    params_model: type[BaseModel]
    runner: Callable[..., Awaitable[JobResult]]


# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class CompactionParams(BaseModel):
    """No params — runs cleanup_orphans with dry_run=False."""


class ImportanceDecayParams(BaseModel):
    max_age_days: float = Field(default=90.0, gt=0)
    source_filter: str | None = None


class DeadChunkLinkCleanupParams(BaseModel):
    """No params — removes chunk_links rows whose source chunk is gone."""


class DedupScanParams(BaseModel):
    threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    limit: int = Field(default=50, ge=1, le=500)
    max_scan: int = Field(default=500, ge=1)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


async def _run_compaction(app: AppContext) -> JobResult:
    """Delete orphan chunks (chunks whose source file no longer exists).

    Loops orphans one-by-one rather than batching: orphan counts are
    typically small, and ``delete_by_source`` already handles its own
    invalidation. If this turns into a bottleneck, add a bulk
    ``delete_by_sources`` to the storage layer (follow-up).
    """
    sources = await app.storage.get_all_source_files()
    orphaned = [sf for sf in sources if not sf.exists()]
    deleted = 0
    for sf in orphaned:
        deleted += await app.storage.delete_by_source(sf)
    if deleted > 0:
        app.search_pipeline.invalidate_cache()
    return {
        "sources_checked": len(sources),
        "orphan_files": len(orphaned),
        "chunks_deleted": deleted,
    }


async def _run_importance_decay(
    app: AppContext,
    max_age_days: float = 90.0,
    source_filter: str | None = None,
) -> JobResult:
    """Expire (delete) chunks older than ``max_age_days``."""
    stats = await expire_chunks(
        app.storage,
        max_age_days=max_age_days,
        dry_run=False,
        source_filter=source_filter,
    )
    if stats.deleted_chunks > 0:
        app.search_pipeline.invalidate_cache()
    return {
        "total_chunks": stats.total_chunks,
        "expired_chunks": stats.expired_chunks,
        "deleted_chunks": stats.deleted_chunks,
    }


async def _run_dead_chunk_link_cleanup(app: AppContext) -> JobResult:
    """Remove ``chunk_links`` rows whose source chunk has been deleted.

    The schema declares ``ON DELETE SET NULL`` for ``source_id``, so a
    deleted source leaves a dangling row with ``source_id IS NULL``.
    These rows are no longer useful for provenance walks (PR-A1's
    rescue leg) and accumulate over time.
    """
    deleted = await app.storage.delete_dangling_chunk_links()
    return {"dead_links_deleted": deleted}


async def _run_dedup_scan(
    app: AppContext,
    threshold: float = 0.92,
    limit: int = 50,
    max_scan: int = 500,
) -> JobResult:
    """Surface duplicate-chunk candidates (no merges — that stays manual).

    Auto-merge is intentionally not part of the scheduled job. A merge
    can lose tags or namespace metadata in subtle ways; the operator
    should run ``mem_dedup_merge`` explicitly after reviewing.
    """
    if app.dedup_scanner is None:
        # Returning a normal result (not raising) — the dispatcher in
        # PR-A3 should map this to status="ok" and surface the
        # ``skipped_reason`` via ``last_run_error``. A missing scanner
        # is a config state, not a runtime failure.
        return {"candidates": 0, "skipped_reason": "dedup_scanner_not_initialized"}
    candidates = await app.dedup_scanner.scan(threshold=threshold, limit=limit, max_scan=max_scan)
    return {"candidates": len(candidates), "threshold": threshold}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


JOB_KINDS: dict[str, JobSpec] = {
    "compaction": JobSpec(
        name="compaction",
        description=("Delete chunks whose source files no longer exist on disk."),
        params_model=CompactionParams,
        runner=_run_compaction,
    ),
    "importance_decay": JobSpec(
        name="importance_decay",
        description=("Delete chunks older than max_age_days (TTL-based decay)."),
        params_model=ImportanceDecayParams,
        runner=_run_importance_decay,
    ),
    "dead_chunk_link_cleanup": JobSpec(
        name="dead_chunk_link_cleanup",
        description=("Remove chunk_links rows whose source chunk has been deleted."),
        params_model=DeadChunkLinkCleanupParams,
        runner=_run_dead_chunk_link_cleanup,
    ),
    "dedup_scan": JobSpec(
        name="dedup_scan",
        description=("Find duplicate-chunk candidates (no auto-merge; surfaces only)."),
        params_model=DedupScanParams,
        runner=_run_dedup_scan,
    ),
}
