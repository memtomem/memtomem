# ADR-0005: Force-reindex metadata preservation contract

**Status:** Accepted
**Date:** 2026-04-30
**Context:** Issue #582 item 4.2 — review of `index_file(..., force=True)` and
its effect on chunks the caller did not intend to modify.

## Background

`IndexEngine._index_file` has two paths for reconciling a markdown file with
storage:

- **Diff path** (`force=False`): compute hash-diff between existing chunks
  and freshly chunked output; delete chunks whose hashes vanished, upsert new
  hashes, leave hash-matches as `unchanged`.
- **Force path** (`force=True`): unconditionally call
  `storage.delete_by_source(file_path)` (hard `DELETE FROM chunks WHERE
  source_file=?`, `engine.py:739`), then `upsert_chunks(new_chunks)`
  (`engine.py:744`) with freshly generated rows.

Today the force path is taken by:

1. `mem_edit` (`server/tools/memory_crud.py:303`) after replacing the body
   of one chunk in a multi-chunk file.
2. `mem_delete` (`server/tools/memory_crud.py:363, 368`) after removing
   one chunk's line range.
3. CLI `mm index --force` (`cli/indexing.py:15`).
4. Web `POST /reindex` (`web/routes/system.py:687`).

## What the spike found

A standalone repro (`mem_increment_access([B.id])` → `mem_edit(A)` → SQL
direct check) on a 2-chunk file showed:

| Field | Before mem_edit | After mem_edit |
|---|---|---|
| `B.access_count` | 1 | **0** |
| `B.last_accessed_at` | (set) | **NULL** |
| `B.id` | `f637ff2a…` | **`81bf918b…`** |
| `B.content` (hash) | unchanged | unchanged |

Chunk B was not the target of the edit. Its content (and therefore content
hash) did not change. Yet the row was hard-deleted and re-inserted with
schema defaults — losing access stats *and* changing the chunk's UUID
identity.

This affects every force-path caller above; mem_edit / mem_delete are the
hot path because they fire force-reindex per single-chunk edit.

### Verdict — Case 2 (accidental coupling)

The force path was added so that storage's `start_line` / `end_line` columns
catch up after a body replacement shifts line numbers — the hash-only diff
path does not update line ranges for chunks it leaves in `unchanged`. The
metadata + identity loss is collateral damage from "delete-then-insert"
being the cheapest way to refresh those columns. There is no design
document, comment, or test asserting that force-reindex *should* reset
metadata, and there is at least one user-visible signal (access_count is a
search-ranking input via `search/access.py:access_boost`) that argues the
opposite.

## Decision

**Force-reindex preserves per-chunk metadata for content-hash-matched
chunks. New / changed chunks behave as today.**

Concretely, the contract for `force=True` becomes:

- For every chunk in the file whose **content hash matches a row in
  storage**: preserve `id`, `access_count`, `last_accessed_at`,
  `importance_score`. Update only the columns that the caller's edit
  actually shifted (e.g., `start_line`, `end_line`, optionally
  `heading_hierarchy` / `tags` if changed).
- For every chunk whose hash is **new**: insert with schema defaults (same
  as today).
- For every existing row whose hash **no longer appears** in the file:
  delete (same as today's `delete_by_source` semantics, but scoped via
  diff rather than blanket).

This is a behavior change. It is intentional and aligns with what every
force-path caller is most likely already trying to express:

- `mem_edit` / `mem_delete`: "I touched one chunk; reconcile storage with
  the new file." Other chunks should be untouched in every observable
  way (including identity).
- CLI `mm index --force`: "Rebuild because I think the cache is stale."
  Users running this rarely want their access stats reset; the use case
  is "the embedder changed" or "I edited the file outside the watcher
  window", not "wipe my history".
- Web `POST /reindex`: same as CLI.

If a caller genuinely needs the old "hard reset" semantics, the path is
explicit: delete by source, then index without force.

## Considered options

1. **Tooltip-only — document the loss, change nothing.**
   - *Pro:* zero risk.
   - *Con:* leaves silent metadata loss in place; chunk-id churn breaks
     any agent that holds an ID across edits (potential follow-on bugs).
   - Rejected: spike showed the cost is real and silent.

2. **Drop `force=True` from mem_edit / mem_delete; teach the diff path to
   update `start_line` / `end_line` for `unchanged` chunks.**
   - *Pro:* fixes mem_edit / mem_delete without touching CLI / web force
     semantics.
   - *Con:* CLI `mm index --force` and web `POST /reindex` keep the
     metadata-loss footgun. Two parallel reconcile paths to maintain.
   - Rejected: leaves the wider footgun open and asymmetric.

3. **Hash-aware reconcile inside the force path (the chosen option).**
   - Approach: have the force path read existing rows up front (same
     `SELECT id, content_hash, ...` shape the diff path already uses),
     then *don't* delete rows whose hash matches a new chunk — assign
     the existing `id` to the matching new chunk and update only the
     columns the caller's edit actually shifted. Delete-by-id for
     hashes that vanished, upsert for hashes that are new.
   - *Pro:* one fix covers every force caller. The force branch
     converges on the same hash-keyed reasoning the diff path already
     does — at the limit, the two branches collapse into a single
     reconcile (deferred to the impl PR's discretion). mem_edit /
     mem_delete keep `force=True` and Just Work.
   - *Con:* the force branch grows an extra `SELECT` and a hash-match
     loop. Negligible at single-file scale, but it is more code than
     the current 2-line "delete + upsert".

4. **Introduce a separate `force_reset=True` flag that is the only path
   to today's hard-reset behavior.**
   - *Pro:* the rare "true clean slate" use case stays accessible.
   - *Con:* premature surface — no one has asked for hard-reset
     semantics. Add the flag *if* a real use case shows up, not
     prophylactically.
   - Deferred (see Trigger criteria).

## Implementation outline (PR #5)

In rough order, all in `packages/memtomem/src/memtomem/`:

1. `indexing/engine.py:_index_file` (force branch, line 704): replace
   the `delete_by_source` + `upsert_chunks` pair with a hash-aware
   reconcile:
   - Read existing rows for this `source_file` (id + content_hash +
     metadata columns).
   - Compute hash diff between existing and `new_chunks`.
   - For `unchanged` (hash match): assign existing `id` to the new
     chunk and update only line-range / heading / tag columns
     in-place.
   - For `to_delete`: delete by id (not by source).
   - For `to_upsert`: upsert as today.
2. `storage/sqlite_backend.py`: add `update_line_ranges(chunks)` (or
   inline UPDATE in the engine's transaction). `delete_by_source`
   stays as a public method but stops being the force path's primary
   tool.
3. Tests:
   - `test_force_reindex_preserves_access_count`
   - `test_force_reindex_preserves_chunk_id_for_unchanged`
   - `test_force_reindex_updates_line_ranges_for_unchanged_after_body_edit`
   - `test_mem_edit_preserves_sibling_chunk_metadata`
   These names are the regression-guard documentation.
4. CHANGELOG entry under `## Unreleased` — `mem_edit` / `mem_delete` /
   `mm index --force` / `POST /reindex` no longer reset access stats
   or chunk IDs for unchanged chunks. Listed as a behavior change, not
   a bugfix, because external agents may have assumed the prior
   contract (unlikely but worth flagging).
5. PR description must call out the chunk-id stability change — agents
   holding chunk IDs across edits will start seeing IDs persist where
   they used to churn. This is the intended end state but is a
   user-visible diff.

## Consequences

- **Search ranking**: more accurate. `access_count` no longer drops to 0
  every time the user edits a sibling chunk. Documents whose owner
  uses `mem_edit` heavily (auto-memory updates, CLAUDE.md edits) start
  retaining their access-frequency boost across edits.
- **Chunk identity stability**: agents (Claude Code, scheduled
  routines) that cache `chunk_id` across mem_edit calls stop getting
  silent invalidation. This is the bigger qualitative win.
- **Importance scoring**: `importance_score` (which factors in
  access_count, tag_count, relation_count) stops resetting on edits.
  See `server/tools/importance.py:51`.
- **Performance**: the force path does an extra `SELECT id, content_hash,
  ...` before reconcile. Negligible at typical memtomem scale (a single
  source file's chunks fit in memory; ~10–100 rows per file).
- **Tests**: existing tests that asserted on chunk-id change after force
  may break. Audit `tests/test_indexing_engine.py` and
  `tests/test_user_workflows.py` for "id changes after force" expectations
  — none surfaced in the spike's grep, but the impl PR should re-check.

## Trigger criteria for revisiting (Option 4)

Add `force_reset=True` flag only if any of:

1. A user reports needing to reset access stats (e.g., "I want to clear
   the access boost after a corpus migration") and the workaround
   (delete by source + reindex) is too coarse.
2. A future feature (e.g., importance recalibration, namespace migration)
   needs hard-reset semantics as a primitive.

Until then, the diff-aware force path is the only documented contract.

## References

- Issue #582 — Index tab follow-ups umbrella (item 4.2 = this ADR).
- Spike repro: standalone Python script (not committed) verified the
  Case 2 verdict: `B.access_count` 1 → 0, `B.id` changed despite
  unchanged content, on a 2-chunk file edited via `mem_edit`-style
  `index_file(force=True)`.
- `packages/memtomem/src/memtomem/indexing/engine.py:_index_file`
  (force branch).
- `packages/memtomem/src/memtomem/storage/sqlite_backend.py:598`
  (`delete_by_source`).
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:303,363,368`
  (mem_edit / mem_delete force callers).
- `packages/memtomem/src/memtomem/cli/indexing.py:15` (CLI `--force`).
- `packages/memtomem/src/memtomem/web/routes/system.py:687`
  (web `POST /reindex`).
- `packages/memtomem/src/memtomem/search/access.py:access_boost`
  (where access_count actually moves search ranking).
