"""One-time startup entity backfill for stores indexed before #2145.

#2145 put regex entity extraction on the indexing engine's chunk-write path and
#2155 extended it to import and consolidation, so every chunk written since
gets an extraction attempt. Chunks written *before* have zero ``chunk_entities``
rows and nothing revisits them: a plain ``mm index`` skips unchanged files, a
forced re-index re-embeds everything just to reach microsecond-cheap regex
rows and never sees chunks whose source is gone (consolidation summaries,
imported bundles), and ``mem_entity_scan`` is a maintenance command someone has
to know to run. ADR-0034's deferral trigger is exactly that last clause: entity
coverage counts as automatic only when an existing store's answer does not
depend on someone having typed a command. This module is that answer — it runs
from ``create_components``, the composition point every entry point (MCP
server, web, CLI, in-process runtime) passes through.

Correctness properties, in the order they bit during design review:

- **Insert-only, re-checked under the write lock.** Selection happens on the
  read pool; a concurrent ``mem_entity_scan`` LLM pass (strictly richer than
  the regex extractor) can populate a selected chunk before the batch's
  transaction starts, and ``upsert_entities`` deletes before inserting.
  Each batch therefore re-filters through ``filter_unextracted_chunk_ids`` on
  the writer connection inside the transaction and writes only chunks that
  still have zero rows — existing rows are never deleted or replaced here.
- **A store-level marker, because a per-chunk one cannot exist.** A chunk whose
  content yields no entities correctly stores nothing, so "has rows" cannot
  mean "was attempted" and a bare missing-rows walk would re-scan entity-less
  chunks on every startup. The ``entity_backfill_v1`` meta key is the
  completion flag: a rowid cursor while in progress, ``done`` at exhaustion.
  ``stale`` (armed by ``entity_sync`` when content is written with extraction
  disabled after ``done``) short-circuits exactly like ``done`` — the gap it
  records is the user's explicit opt-out, remediated by ``mem_entity_scan``,
  and never by silently re-walking the store.
- **Bounded startup cost.** Work is paged (each page one write transaction,
  one commit) and capped per startup; the persisted cursor resumes the walk on
  the next startup, so an oversized store amortizes instead of blocking one
  boot unboundedly. A fresh store pays one empty SELECT and flips to ``done``.
- **Crash-safe by idempotence.** The cursor is persisted after each committed
  batch. A crash loses at most the current batch, whose chunks either
  committed (skipped by the missing-rows predicate on re-run) or rolled back
  (re-listed and re-written). ``enabled=False`` returns without setting any
  state: the flag means "backfill completed", not "backfill considered", and
  stamping ``done`` under a disabled config would permanently strand a user
  who enables extraction later.

The audit trail is a ``maintenance_runs`` row (``kind="entity_backfill"``,
``source="startup"``), written only when the walk actually processed chunks —
the steady-state no-op startup leaves no row.

No cache coupling: ``create_components`` runs this before ``SearchPipeline``
exists, so the search cache is born after these writes and there is nothing to
invalidate.
"""

from __future__ import annotations

import logging
from typing import Any

from memtomem.storage.mixins.entities import ENTITY_BACKFILL_DONE, ENTITY_BACKFILL_STALE
from memtomem.tools.entity_sync import sync_entities_for_chunks

logger = logging.getLogger(__name__)

# Everything the walk calls, probed together like ``entity_sync`` does: the
# storage ABC declares none of this surface, and probing one name while calling
# five would turn a partial backend's clean degrade into an ``AttributeError``.
_REQUIRED_STORAGE_METHODS = (
    "list_chunks_missing_entities",
    "filter_unextracted_chunk_ids",
    "entity_backfill_get_state",
    "entity_backfill_set_state",
    "transaction",
)

# Per-startup work cap. Regex extraction is microseconds per chunk, so the cost
# that matters is reading content and committing rows; at this cap a cold
# legacy store adds low single-digit seconds to one startup and any remainder
# resumes on the next. Not configuration — no store shape observed so far needs
# a different value, and a knob would outlive the one-time migration it tunes.
_MAX_CHUNKS_PER_STARTUP = 25_000


async def backfill_entities(
    storage: Any,
    *,
    enabled: bool,
    batch_size: int = 500,
    max_chunks_per_startup: int = _MAX_CHUNKS_PER_STARTUP,
) -> int:
    """Walk chunks with no entity rows and give each one extraction attempt.

    Returns the number of chunks processed this call (0 on every no-op path).
    Raises nothing in normal operation but does not catch storage failures —
    the ``create_components`` call site wraps this call, because a failed
    backfill must degrade startup, never abort it.

    Args:
        storage: Storage backend. One without the backfill surface (see
            ``_REQUIRED_STORAGE_METHODS``) degrades to a no-op.
        enabled: ``indexing.extract_entities``. ``False`` is a full no-op that
            leaves no state behind.
        batch_size: Chunks per page / per write transaction.
        max_chunks_per_startup: Work cap for this call; the cursor resumes the
            remainder on the next startup.
    """
    if not enabled:
        return 0
    if not all(hasattr(storage, name) for name in _REQUIRED_STORAGE_METHODS):
        return 0

    state = await storage.entity_backfill_get_state()
    if state in (ENTITY_BACKFILL_DONE, ENTITY_BACKFILL_STALE):
        return 0
    cursor = int(state) if state and state.isdigit() else 0
    resumed_from = cursor

    processed = 0
    entities_written = 0
    run_id: int | None = None
    can_audit = hasattr(storage, "maintenance_run_start") and hasattr(
        storage, "maintenance_run_finish"
    )

    try:
        while processed < max_chunks_per_startup:
            page = await storage.list_chunks_missing_entities(
                after_rowid=cursor, limit=min(batch_size, max_chunks_per_startup - processed)
            )
            if not page:
                await storage.entity_backfill_set_state(ENTITY_BACKFILL_DONE)
                break

            if run_id is None and can_audit:
                run_id = await storage.maintenance_run_start("entity_backfill", source="startup")

            chunks_by_id = {str(chunk.id): chunk for _rowid, chunk in page}
            async with storage.transaction():
                # Writer-connection re-check: only chunks still bare of rows are
                # written, so a scan that populated one since selection wins.
                still_missing = await storage.filter_unextracted_chunk_ids(list(chunks_by_id))
                entities_written += await sync_entities_for_chunks(
                    storage,
                    [chunks_by_id[cid] for cid in chunks_by_id if cid in still_missing],
                    enabled=True,
                )

            processed += len(page)
            cursor = page[-1][0]
            # Persisted outside the batch transaction on purpose: a crash in
            # between re-lists one already-written batch, and the missing-rows
            # predicate skips it. The reverse order could skip an unwritten one.
            await storage.entity_backfill_set_state(str(cursor))
        else:
            logger.info(
                "entity backfill paused at the per-startup cap (%d chunks); "
                "resuming from rowid %d on the next startup",
                processed,
                cursor,
            )
    except BaseException as exc:
        if run_id is not None:
            await storage.maintenance_run_finish(
                run_id,
                status="error",
                affected_count=processed,
                summary={"entities_written": entities_written, "resumed_from": resumed_from},
                error=str(exc),
            )
        raise

    if run_id is not None:
        final_state = await storage.entity_backfill_get_state()
        await storage.maintenance_run_finish(
            run_id,
            status="ok",
            affected_count=processed,
            summary={
                "entities_written": entities_written,
                "resumed_from": resumed_from,
                "completed": final_state == ENTITY_BACKFILL_DONE,
            },
        )
    return processed
