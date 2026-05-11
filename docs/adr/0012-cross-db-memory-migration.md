# ADR-0012: Cross-DB memory migration (deferred)

**Status:** Proposed (deferred pending trigger)
**Date:** 2026-05-11
**Context:** ADR-0011 §"Open questions" item 5 deferred chunk-id-stable
rename *across* databases. PR #886 closed the v1 single-DB follow-ups
(lineage regression test, glob input, computed neighborhood display) and
explicitly punted cross-DB to its own design doc. Issue #911 tracks
that deferral. This ADR records the technical surface a real cross-DB
implementation would have to cover, the two plausible product shapes,
and crisp trigger criteria for each — so a future reader can see what
was considered and what would justify flipping this ADR to *Accepted*.

## Background

### What ADR-0011 §4 fixed

ADR-0011 §4 "Memory storage stays single-user-local, schema gains a
scope tag per chunk"
(`docs/adr/0011-canonical-artifact-scope-hierarchy.md:168-175`):

> Memory's SQLite DB at `~/.memtomem/memtomem.db` is derived state —
> embeddings, FTS rowids, dedup hashes, chunk-link graph. Splitting the
> DB across scopes would force per-scope schema evolution, fragment
> dedup, and break `mem_agent_share` (chunk-links would need cross-DB
> FKs). **One DB, per-row scope tag** is the only path that keeps the
> existing dedup, sharing, and embedding contracts intact.

The §4 decision is the load-bearing premise: scope is a per-row tag
inside *one* DB, not a per-DB shard. Any cross-DB feature lives
*outside* that contract.

### What v1 rename actually does

`mm context memory-migrate` (shipped in #882 PR-D) calls
`update_chunks_scope_for_source`
(`packages/memtomem/src/memtomem/storage/sqlite_backend.py:908`).
The function opens one SQLite write transaction (`BEGIN IMMEDIATE`),
updates `chunks.source_file` / `scope` / `project_root` in place, and
rewrites `chunks_fts.source_file` for the same rowids. The FK
neighborhood — `chunk_links`, `access_log`, `chunk_entities`,
`chunk_relations` — keys on `chunks.id` (TEXT PK), which the UPDATE
never touches. `chunks_vec` is implicit-rowid keyed and likewise
untouched. The AI-summary cache rows in `_memtomem_meta`
(prefix `ai_summary:`) are re-keyed by the same function so stale
summaries don't linger. The whole thing is one atomic, one-DB
transaction; rollback on any exception puts the world back exactly
where it started.

Cross-DB breaks every step of that. There are two write transactions
on two SQLite files under two sidecar locks, and the FK neighborhood
has to be **explicitly preserved or remapped** rather than implicitly
carried by chunk-id stability.

### Closest existing prior art: bundle v2

A JSON export/import path already lives at
`packages/memtomem/src/memtomem/tools/export_import.py` — `export_chunks`
(`:83`), `import_chunks` (`:161`), `preserve_ids` opt-in (`:167`),
`content_hash`-based conflict resolution (`:142`). It re-embeds on
import, so it tolerates embedder skew. **It is not a cross-DB
migration**: `_chunk_to_dict` (`:136-153`) drops `scope` and
`project_root`, the FK neighborhood (`chunk_links` etc.) is not
serialized, and the privacy gates (`privacy.py:432` `enforce_write_guard`
Gate A, `server/tools/memory_crud.py:204` Gate B `confirm_project_shared`)
are not threaded through the import path. A cross-DB design must say
explicitly whether it extends bundle v2 or supersedes it.

## Why this needs its own ADR

The v1 rename ships under one premise: chunk ID stability inside one
DB is free. Cross-DB has to design that explicitly. Eight engineering
surfaces emerge from the §4 premise once the DB boundary is crossed.

### Engineering surfaces

1. **FTS5 population path.** `chunks_fts` is a virtual table on the
   source DB; rowids are local. See the definition at
   `storage/sqlite_schema.py:157`. There are no triggers — population
   is explicit. The bundle-v2 import path is fine here because
   `upsert_chunks` (`storage/sqlite_backend.py:437`) inserts the
   matching `chunks_fts` row alongside each new chunk
   (`:579`); a cross-DB design that reuses `upsert_chunks` inherits
   that. A cross-DB design that bypasses `upsert_chunks` (e.g., raw
   `INSERT INTO chunks` for chunk-id preservation under collision
   policy) must call `rebuild_fts` (`storage/sqlite_backend.py:1005`)
   explicitly, because FTS rowids on the source DB are not
   transferable.
2. **Dense-vector backend skew.** Embedder config lives in
   `_memtomem_meta` (`embedding_provider`, `embedding_model`,
   `embedding_dimension`); the protocol is at
   `embedding/base.py:9` (`EmbeddingProvider.model_name` /
   `.dimension`). Receiver may run a different embedder. Two
   resolutions: refuse mixed-embedder transfers pre-flight (safer,
   loses no information), or re-embed on import (matches bundle v2
   `:161` behavior, costs receiver-side compute). The ADR must pick
   one and pin it.
3. **Dedup hash reconciliation.** `content_hash` is deterministic
   across DBs (`models.py:152-154`, NFC-normalized UTF-8 SHA-256), so
   the same content yields the same hash. The single-DB unique
   constraint is `(namespace, source_file, content_hash, start_line)`.
   Cross-DB needs an explicit policy when receiver already has the
   same `content_hash` with a *different* `chunks.id`: skip,
   overwrite, merge-lineage, or refuse. Bundle v2's `on_conflict`
   handles chunk-level conflict but does not address FK-graph
   collisions (one chunk_links row pointing at receiver's old ID vs.
   the incoming ID).
4. **`scope` / `project_root` / `source_file` remap.** `chunks`
   persists all three (`models.py:23`,
   `storage/sqlite_backend.py:519` `_chunk_from_row`), and v1 rename
   updates them atomically. `_chunk_to_dict` (`:136-153`) **does
   not serialize `scope` or `project_root`** today, so a bundle-v2
   roundtrip lands every row in the receiver's default scope —
   mis-tiered relative to ADR-0011 semantics and effectively
   invisible to scope-aware queries. Cross-DB must define the remap
   shape: literal copy, explicit `--target-scope` /
   `--target-project-root` flags, or interactive prompt. The
   simplest gap-closer here is *just adding the two fields to
   bundle v2*; if that satisfies the user need, an ADR isn't
   warranted at all.
5. **Privacy / trust-boundary gating.** ADR-0011 §5 codified two
   gates for `project_shared` writes: Gate A at the chokepoint
   (`privacy.py:432` `enforce_write_guard`, which hard-refuses
   `force_unsafe=True` on `project_shared` because git history is
   permanent — see the `blocked_project_shared` outcome at
   `privacy.py:466-479`) and Gate B at the surface
   (`server/tools/memory_crud.py:204` `confirm_project_shared`
   refusal). Bundle v2's import path does **not** route through
   either gate today. Cross-DB into `project_shared` must specify
   whether import re-runs Gate A scans on every chunk's content,
   hard-refuses bulk import into `project_shared` without per-chunk
   re-curation, or requires a `--confirm-project-shared` flag
   mirroring the v1 CLI. Defaulting to "trust the source DB" is the
   wrong default — git history is permanent and the source DB never
   passed *this repo's* gates.
6. **Schema-version migration.** Receiver may sit on a different
   `_memtomem_meta` shape. memtomem has no explicit `schema_version`
   counter today: idempotent ALTERs at
   `storage/sqlite_schema.py:89-154` infer state from column
   presence. Cross-DB op has to pin receiver-compatible schema in
   one step before any chunk write, or import partial-fails on
   schema drift halfway through the batch.
7. **`_memtomem_meta` AI-summary cache transfer.** Keys carry the
   absolute `source_file` (`storage/sqlite_backend.py:71` prefix
   `ai_summary:`). v1 rename re-keys them inside one DB
   (`sqlite_backend.py:980-994`); cross-DB does the same but across
   two `_memtomem_meta` tables, and the key transform depends on
   whether `source_file` is being remapped (surface 4). The two
   policies have to compose.
8. **Failure-mode policy across two DB locks.** The v1
   `BEGIN IMMEDIATE` + compensating rollback contract does not
   generalize to two SQLite files. The lock-ordering invariant
   must be:
   - **Canonicalize each DB path** via `Path.resolve(strict=False)`
     before deriving the sidecar lock path. `_lock_path_for` at
     `context/_atomic.py:113` derives the sidecar from the path
     *as spelled*, so symlinks or mixed relative/absolute spellings
     can defeat a naive global order.
   - **Acquire both sidecar locks first.** The
     `_acquire_pair_lock` helper at `context/migrate.py:573` sorts
     two `Path`s and takes both before yielding; cross-DB must
     reuse this exact helper after canonicalizing inputs.
   - **Open SQLite write transactions only after both locks are
     held.** Otherwise a writer that loses the lock race holds an
     open writer connection while waiting on the second sidecar,
     starving everything else on that DB.
   - **Document the commit-ordering and compensation contract**
     for the two asymmetric failure shapes. SQLite cannot
     transactionally roll back a commit that already landed on the
     other DB — once a write commits, recovery is replay or
     compensation, not transaction abort. The two shapes:
     - **Receiver-commits-first.** Receiver `COMMIT` succeeds; then
       source-side delete fails before its own `COMMIT`. Source
       reverts cleanly (one-DB rollback). Receiver now holds the
       data and source still does too — duplication, not loss.
       Compensation: re-attempt source delete (idempotent on
       chunk-id) or revert receiver via a compensating delete.
     - **Source-commits-first.** Source delete `COMMIT` succeeds;
       then receiver write fails. Receiver reverts cleanly but
       source data is gone — loss unless the operation staged the
       bytes on disk first. Compensation: replay from staged
       export bundle.
     v1 picks a single answer because it has one transaction;
     cross-DB has to specify the order and whether a staging
     bundle is mandatory before the source-side delete.

## Two product shapes (separate ADRs if either materializes)

The issue body identifies two product shapes with different
semantics. They should be separate proposals because they have
different default scopes, lock contracts, and privacy implications.
Trigger criteria below are written so a future reader can decide
whether the shape has actually materialized or whether the user
need can be solved by a smaller change against bundle v2.

### Shape A — Team onboarding (one-way export)

Teammate A's `scope='user'` notes published into team B's
`project_shared` tier for collective access. Source is read-only
during the export; destination is a different DB that the team
collectively owns.

**Trigger criteria.** An onboarding flow that cannot be satisfied
by `export_chunks` → `import_chunks` today because of:
- `scope` / `project_root` not being serialized in bundle v2
  (surface 4) — and the fix is *not* "add those two fields to
  `_chunk_to_dict`," or
- Gate A/B not running on the import path for `project_shared`
  writes (surface 5) — and the fix is *not* "thread the gates
  through `import_chunks`," or
- FK lineage (`chunk_links` etc.) being dropped on the JSON
  roundtrip (surface 3) — and the fix is *not* "extend
  `_chunk_to_dict` to emit chunk_links rows for the exported chunk
  set and have `import_chunks` re-insert them by mapped chunk ID
  under the chosen collision policy." A bundle-v2 FK extension
  closes the gap if the flow only needs *intra-bundle* lineage
  (links between chunks both contained in the export). It does
  *not* close the gap if the flow needs lineage to receiver-side
  chunks that already exist (cross-DB FK chasing), which is
  inherently a cross-DB design problem.

If all three "and the fix is not …" clauses fail (i.e., a small
bundle-v2 patch *does* solve the user's flow), make the patch and
skip the ADR.

### Shape B — Project archive (bulk move)

Aging project's chunks moved off the main user DB into a separate
archive DB to relieve size pressure on the hot DB. Two-way: source
deletes after receiver commits, with referential integrity
preserved on receiver.

**Trigger criteria.** A user reports `~/.memtomem/memtomem.db`
size pain that **existing compaction / orphan-GC remedies do not
solve**. (ADR-0011 §"Open questions" already has an open item on
orphan project chunk garbage collection — that work likely lands
before any archive shape is justified, and may absorb the use
case entirely.)

Shape B is strictly harder than Shape A: two-way lock (per surface
8), FK-preserving export (surface 3), embedder-match precondition
(surface 2), source-side delete that must replay if receiver-side
commit fails (surface 8 again).

## Out of scope (intentional)

- **`v1` single-DB chunk-id-stable rename.** Shipped in #882 PR-D;
  regression-pinned by the lineage test in #886. The premise of
  this ADR is "after #886's v1 baseline."
- **Glob input.** Landed in #886.
- **Computed `chunk_links` lineage display.** Landed in #886.
- **Extending bundle v2 with a missing field.** Adding `scope` /
  `project_root` to `_chunk_to_dict`, or threading
  `confirm_project_shared` through `import_chunks`, is **not** a
  cross-DB migration feature. Those are normal PRs against
  `tools/export_import.py` and should not invoke this ADR.
- **`mm mem rescan` / `mm context rescan`.** Separate ADR-0011
  open question (item 4).

## Open questions for the future implementation issues

The following are the choices the next ADR (whether
ADR-0013 onboarding-export or ADR-0014 archive-split) would have
to commit to. They stay open here.

- **Embedder-mismatch policy.** Refuse pre-flight, or re-embed on
  import (matching bundle v2)? Refusal is safer; re-embed costs
  receiver-side compute. Pick one; do not leave the user to
  guess from the error message.
- **`(content_hash, different chunks.id)` collision policy.**
  Skip / overwrite / merge-lineage / refuse. Bundle v2 handles
  chunk-level conflict; the FK graph is the open part.
- **CLI surface shape.** Does `mm context memory-migrate` grow a
  `--to-db <path>` flag, or does a new subcommand land
  (e.g., `mm context memory-export-bundle` /
  `mm context memory-import-bundle` with FK preservation)?
- **Bundle v2 evolution vs. supersession.** If the immediate user
  needs are all solvable by extending bundle v2 (fields + gates),
  the cross-DB ADR may never need to ship. If a separate-DB
  archive use case materializes, cross-DB likely supersedes
  bundle v2's JSON path.

## References

**Issues / PRs**
- #911 — this ADR's tracking issue (deferred-with-trigger record).
- #886 — v1 single-DB follow-ups (lineage test + glob input +
  computed lineage display); explicitly punts cross-DB.
- #882 — PR-D shipped v1 chunk-id-stable
  `mm context memory-migrate`.

**ADRs**
- ADR-0010 — `target_scope` 3-tier axis for settings hooks; the
  vocabulary this ADR inherits via ADR-0011.
- ADR-0011 §4 — "Memory storage stays single-user-local, schema
  gains a scope tag per chunk" (load-bearing premise of this ADR).
- ADR-0011 §5 — Privacy gates Gate A / Gate B for
  `project_shared` writes (surface 5).
- ADR-0011 §"Open questions" item 5 — the deferral this ADR
  records.
- ADR-0011 §"Open questions" item 3 — orphan project chunk GC;
  interacts with Shape B trigger criteria.

**Source anchors (by symbol — line numbers reflect HEAD on
`main` at this ADR's date, grep the symbol if drift)**

- `packages/memtomem/src/memtomem/storage/sqlite_backend.py` —
  `update_chunks_scope_for_source` (`:908`), `rebuild_fts`
  (`:1005`), AI-summary key migration block, `_chunk_from_row`
  (`:519`).
- `packages/memtomem/src/memtomem/storage/sqlite_schema.py` —
  `chunks_fts` virtual-table def (`:157`), idempotent-ALTER
  migration block (`:89-154`).
- `packages/memtomem/src/memtomem/tools/export_import.py` —
  `export_chunks` (`:83`), `_chunk_to_dict` (`:136`),
  `import_chunks` (`:161`), `preserve_ids` (`:167`).
- `packages/memtomem/src/memtomem/embedding/base.py:9` —
  `EmbeddingProvider` protocol.
- `packages/memtomem/src/memtomem/models.py` — `Chunk` fields
  including `scope` / `project_root`, `content_hash`
  computation (`:152-154`).
- `packages/memtomem/src/memtomem/privacy.py:432` —
  `enforce_write_guard` (Gate A); `:466-479` documents the
  `project_shared` hard refusal.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:204`
  — `confirm_project_shared` Gate B refusal in `mem_add`.
- `packages/memtomem/src/memtomem/context/migrate.py:573` —
  `_acquire_pair_lock` two-lock ordering helper.
- `packages/memtomem/src/memtomem/context/_atomic.py:113` —
  `_lock_path_for` (sidecar derivation; why canonicalization
  matters before sorted ordering).
