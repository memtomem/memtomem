"""Whitelisted job kinds for the cron scheduler (P2 Phase A).

Each ``JobSpec`` declares a stable name, a one-line description (used
in Phase B's LLM prompt), a Pydantic v2 ``params_model`` for validating
parameters, and an async ``runner`` that takes ``(app, **params)`` and
returns a small result dict. A spec with ``keeps_state`` also takes the
state its schedule stored and returns what to store next (``run_job``).

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

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# expire_chunks lives in the search package; importing at module top is
# safe — there is no scheduler ↔ search cycle. Likewise orphan_detect lives
# in the leaf ``storage`` package, so this stays clear of the heavy
# ``server`` import that would risk a scheduler ↔ server cycle.
from memtomem.search.decay import expire_chunks
from memtomem.storage.mixins.schedules import KEEP_STATE, StateUpdate
from memtomem.storage.orphan_detect import hold_orphans, scan_orphans

if TYPE_CHECKING:
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


JobRunStatus = Literal["ok", "skipped", "error", "timeout", "running"]
"""Status enum for schedule run outcomes.

``ok``/``skipped``/``error``/``timeout`` are terminal, written by
``ScheduleMixin.schedule_mark_run``. ``skipped`` is a run whose runner
returned normally with a ``skipped_reason`` (see ``run_outcome``). ``running`` is the transient claim
state written by ``schedule_try_claim`` (issue #1564) — overwritten by a
terminal status when the run completes, or left as a diagnostic breadcrumb
if the process crashes mid-run (dispatch never gates on status, so a stuck
``running`` doesn't starve the schedule)."""

JobResult = dict[str, Any]
"""Runner return shape — small JSON-serializable summary."""

JobState = dict[str, Any]
"""What a job that continues between runs stores on its schedule (JSON)."""


def run_outcome(result: object) -> tuple[JobRunStatus, JobResult | None]:
    """Map a runner's return value to the status and result to record.

    Shared by every path that runs a job (the watchdog dispatcher and both
    run-now surfaces) so they record the same outcome. A runner that did no
    work signals it in-band with ``skipped_reason`` rather than raising — a
    missing component or a refused deletion is not a failure — and that run
    is recorded as ``skipped`` so ``mm schedule list`` shows it (#2471).
    """
    if not isinstance(result, dict):
        return "ok", None
    status: JobRunStatus = "skipped" if result.get("skipped_reason") else "ok"
    return status, result


@dataclass(frozen=True)
class JobSpec:
    """Schema + runner for one schedulable job kind."""

    name: str
    description: str
    params_model: type[BaseModel]
    # ``(app, **params) -> JobResult``; with ``keeps_state``,
    # ``(app, state=..., **params) -> (JobResult, StateUpdate)``.
    runner: Callable[..., Awaitable[Any]]
    keeps_state: bool = False


async def run_job(
    spec: JobSpec, app: AppContext, params: BaseModel, state: JobState | None
) -> tuple[Any, StateUpdate]:
    """Run *spec* and return its result with what to do to the stored state.

    Shared by every path that runs a job, like ``run_outcome``. *state* is
    the schedule row's ``state`` as the caller read it. The second value
    goes to ``schedule_mark_run`` with the outcome; a job that keeps no
    state always returns ``KEEP_STATE``. Only the first value is a result:
    it alone goes to ``run_outcome`` and to a run-now caller.
    """
    kwargs = params.model_dump()
    if spec.keeps_state:
        result, update = await spec.runner(app, state=state, **kwargs)
        return result, update
    return await spec.runner(app, **kwargs), KEEP_STATE


def stored_params(spec: JobSpec, sched: dict[str, Any]) -> BaseModel:
    """Validate a stored schedule's params for a run.

    Shared by every path that runs a stored schedule, like ``run_job``. A row
    whose ``params_json`` could not be read carries ``params_error`` and no
    params; that raises with the reason rather than validating ``{}``, which
    would run the job with its defaults. The callers record either failure
    as an ``invalid params`` error before the runner starts.
    """
    if sched.get("params_error"):
        raise ValueError(sched["params_error"])
    return spec.params_model.model_validate(sched["params"])


def validate_new_params(spec: JobSpec, params: object) -> BaseModel:
    """Validate params for a schedule being registered, refusing unknown keys.

    The params models ignore unknown keys (pydantic's default), and the
    registration paths store the caller's dict as given, so a typo such as
    ``{"limt": 20}`` would be saved and every run would quietly use the
    default. Registration therefore refuses any key the model does not
    declare. Runs of already-stored schedules validate with the model alone
    and keep ignoring such keys, so a row saved before this check still runs.

    The key check is a set difference rather than ``extra="forbid"`` on the
    call, because that per-call argument does not exist at the
    ``pydantic>=2.10`` floor. It only covers field names; no params model
    declares an alias. A non-dict skips the key check and is rejected by
    ``model_validate`` (``mem_do`` does not enforce the ``dict`` annotation,
    and iterating a string would report its characters as keys).
    """
    if isinstance(params, dict):
        unknown = sorted(set(params) - set(spec.params_model.model_fields), key=repr)
        if unknown:
            allowed = ", ".join(sorted(spec.params_model.model_fields)) or "none"
            names = ", ".join(repr(key) for key in unknown)
            raise ValueError(f"unknown params for {spec.name}: {names} (allowed: {allowed})")
    return spec.params_model.model_validate(params)


# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class CompactionParams(BaseModel):
    """No params — holds unavailable sources without purging chunks."""


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
    """Hold missing or inaccessible sources; automatic cleanup never purges."""
    result = await scan_orphans(app.storage)
    outcome = await hold_orphans(app.storage, result, app.config.indexing)
    if outcome.changed:
        app.search_pipeline.invalidate_cache()
    return {
        "sources_checked": result.total_sources,
        "orphan_files": outcome.missing_held,
        "held_sources": outcome.missing_held + outcome.unavailable_held,
        "archived_sources": outcome.archived,
        "chunks_deleted": 0,
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


def _dedup_cursor(state: JobState | None) -> UUID | None:
    """The chunk id a dedup walk continues after, from the stored state.

    ``None`` without a state. A state whose ``after`` is not a chunk id in
    the form ids are stored in is logged and read as no position.
    """
    if state is None:
        return None
    raw = state.get("after")
    if isinstance(raw, str):
        try:
            cursor = UUID(raw)
        except ValueError:
            pass
        else:
            if str(cursor) == raw:
                return cursor
    logger.warning(
        "dedup_scan: stored position %r is not a chunk id; starting from the first pool", state
    )
    return None


async def _run_dedup_scan(
    app: AppContext,
    threshold: float = 0.92,
    limit: int = 50,
    max_scan: int = 500,
    *,
    state: JobState | None = None,
) -> tuple[JobResult, StateUpdate]:
    """Surface duplicate-chunk candidates (no merges — that stays manual).

    Auto-merge is intentionally not part of the scheduled job. A merge
    can lose tags or namespace metadata in subtle ways; the operator
    should run ``mem_dedup_merge`` explicitly after reviewing.

    Each run scans the pool after the one the schedule's last finished run
    scanned: *state* holds that pool's last chunk id, and the state returned
    here holds this pool's. A run that reaches the end of the store clears
    the state, so the next run starts from the first pool; chunk ids are
    random, so a chunk added during a walk can sit before the cursor, and
    only a walk from the start reaches it.
    """
    if app.dedup_scanner is None:
        # Returning a normal result (not raising): a missing scanner is a
        # config state, not a runtime failure. ``run_outcome`` records the
        # run as ``skipped`` with this result. Nothing was read, so the
        # position stays; ``None`` here would start a walk over.
        return {"candidates": 0, "skipped_reason": "dedup_scanner_not_initialized"}, KEEP_STATE
    after = _dedup_cursor(state)
    candidates, coverage = await app.dedup_scanner.scan_with_coverage(
        threshold=threshold, limit=limit, max_scan=max_scan, after=after
    )
    # Strings, not UUIDs: both run-now surfaces print the result as JSON.
    next_after = None if coverage.next_after is None else str(coverage.next_after)
    # Coverage describes the selected pool only: zero candidates with
    # ``without_vector`` > 0 means part of the pool was never probed.
    result: JobResult = {
        "candidates": len(candidates),
        # Pairs before ``limit``; above ``candidates`` the list was cut.
        "pairs_found": coverage.pairs_found,
        "threshold": threshold,
        "pool": coverage.pool,
        "eligible": coverage.eligible,
        "remaining": coverage.remaining,
        "probed": coverage.probed,
        "without_vector": coverage.without_vector,
        "same_source_read_only_pairs": coverage.same_source_read_only,
        "near_search_enabled": coverage.near_search_enabled,
        # Where this run started (null: the first pool) and where the next
        # one starts (null: this run reached the end, so the first pool).
        "after": None if after is None else str(after),
        "next_after": next_after,
    }
    # An empty pool above the cursor also has no ``next_after``: it clears.
    return result, (None if next_after is None else {"after": next_after})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


JOB_KINDS: dict[str, JobSpec] = {
    "compaction": JobSpec(
        name="compaction",
        description=(
            "Hold chunks of missing sources, or sources whose availability cannot be"
            " checked, outside search for recovery; never deletes (purge"
            " confirmed-missing sources with mm gc orphan-sources --apply)."
        ),
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
        keeps_state=True,
    ),
}
