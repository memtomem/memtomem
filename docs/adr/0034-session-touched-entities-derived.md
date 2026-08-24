# ADR-0034: Session-touched entities are derived from write provenance, not stored

**Status:** Proposed (deferred pending trigger)
**Date:** 2026-08-23
**Context:** Issue #2133 asks that a session record which entities it touched,
so handover between sessions and between agents can be a query rather than a
prose summary someone hopes is complete. It filed two open questions — a
derived view versus a stored edge, and what "touched" should mean — and named
its own blocker: entity coverage is opt-in, so on a store where nobody ran a
scan the feature would return nothing and look broken. This ADR answers both
questions and states the trigger that would move it to *Accepted*, so the
deferral is a decision with criteria rather than an idea left lying around.

## Background — what the store already holds

Three facts decided this, and each is checkable in one grep.

**The write half is already recorded.** #1876 gave seven MCP write surfaces a
session event marked `write-v1`: `mem_add`, `mem_batch_add`, `mem_index`,
`mem_fetch`, `mem_agent_share`, formation's candidate review, and
`mem_consolidate_apply` each append a `session_events` row naming the chunk ids
they created (`server/tools/_provenance.py`). Other mutations — edit, delete,
dedup/decay, import/export, namespace moves — record no ids and instead set
`provenance_incomplete`, so the marked set is precise about its own limits
rather than complete. `session_events.chunk_ids` joins
`chunk_entities.chunk_id` through indexes that already exist —
`idx_session_events_session` and `idx_entities_chunk`
(`storage/sqlite_schema.py`). A derived view therefore needs no schema at all:
not a table, not a column, not a relation type.

**There is no first-party read provenance.** `server/tools/search.py`,
`server/tools/recall.py` and `server/tools/browse.py` contain no session
reference of any kind. (A LangGraph caller can hand-attach recall ids to an
event of its own choosing via `integrations/langgraph.py`, which is exactly why
consumers must filter on the `write-v1` marker rather than on `chunk_ids` being
non-empty — it is caller-supplied, not authoritative.) `access_log` is shaped for chunk-level read attribution
but has no `session_id` column and no writer in the entire source tree — only
tests insert into it. `query_history` records `result_chunk_ids` but carries no
session linkage, so session attribution could only be inferred from a time
window, and even then it would miss every `mem_read` and `mem_recall`.

**Entity coverage is worse than "sparse" — it is empty by default and decays.**
(Superseded by #2145 for new writes — see §Deferral. The paragraph is kept as
the argument that motivated the deferral, and describes the store as it stood
when this ADR was written.)
`upsert_entities` has exactly one production call site, inside `mem_entity_scan`
(`server/tools/entity.py`), as does `delete_entities_for_chunk`. Nothing in `indexing/`,
`mem_add`, formation or the scheduler writes the table; `JOB_KINDS` has no
entity job; there is no CLI command and no web page. So on a default install
`chunk_entities` is not sparsely populated, it is empty, permanently, until
someone types a command. And because the table cascades on `chunks(id)`,
re-indexing an edited file deletes that chunk's entities and nothing puts them
back — coverage only shrinks after the one scan somebody ran. The comment in
`storage/sqlite_namespace.py` claiming the state "self-heals the next time that
chunk is re-indexed, since `set_chunk_entities` deletes before inserting" is
wrong twice over: no such method exists, and re-indexing only destroys.

Worth naming as precedent: the bi-temporal assertion layer from #1732
(`canonical_entities`, `assertion_edges`, `add_assertion`/`link_assertions`/
`query_assertions`) has zero production callers — tests only. That is the same
family of investment this issue proposes, one layer up, already sitting unused.

## Decision

### D1 — Shape: derived at read time

Answer "which entities did this session touch" by deriving it, not by storing
it. The derivation is: take the session's `session_events` rows whose
`metadata.provenance` equals `write-v1`, union their `chunk_ids`, join
`chunk_entities`.

Reuse `_collect_provenance_chunk_ids` (`server/tools/session.py`) as the id
source rather than re-implementing the filter. That function already encodes
what "the session wrote" means for auto-summary, including rejecting the whole
set when provenance is truncated or mismatched. Two answers to the same
question must not be able to disagree.

No new table, no new column on `sessions`, no new relation type in
`chunk_relations`.

### D2 — "Touched" means *wrote*

Not read, not referenced. This is the boundary the project already ships and
documents: the session summary covers what the session wrote, and says so with
`summary_provenance` — `exact` when it used the session's own marked write
events, `fallback` for the namespace/time-window path, `manual` for an
author-supplied summary, and absent where the run made no claim at all
(`docs/guides/configuration.md`, §Session Summary).

The derived view inherits that honesty contract. It answers for a session whose
provenance marker is present and complete; for any other session it declines to
answer rather than widening into a namespace or time-window scan. A marked
session with no write events is authoritatively empty — the same rule #1876
established for summaries.

Widening "touched" to include reads is a different feature with a different
cost, and it would need its own provenance marker rather than borrowing this
one. It is explicitly not decided here.

### D3 — Surface: an existing one

When this is implemented, it is exposed through a surface that already exists —
not a new MCP tool. The registered tool count is pinned by a hard guard
(`test_docs_guards.py` asserts exact totals for `_ALL_REGISTERED_TOOLS` and the
documented table), so a new tool is a docs-and-count change before it is a
feature — and the immediate precedent, #2132, added its read path as one
optional parameter on an existing tool instead.

Candidates in order: a sibling of the web route `GET /sessions/{id}/events`,
then an optional parameter on `mem_session_list`. The choice is left to
implementation time; the constraint — no new tool — is not.

### D4 — Staleness is inherited, deliberately

Because the view reads `chunk_entities` live, its answer moves exactly as
coverage moves — a scan can grow it, and can equally shrink it, since
`mem_entity_scan(overwrite=True)` clears rows for content that no longer
extracts anything. That is the intended property: the
view never claims more than the store currently knows, and there is no second
copy that can disagree with the first. A stored edge would have to choose
between going stale and being rewritten on every re-index.

## Deferral

**Status stays *Proposed*. Trigger: entity coverage becomes automatic.**

Concretely — extraction runs as part of indexing, for every chunk, without
anyone invoking a maintenance command, and re-indexing re-extracts rather than
merely cascading the old rows away. The criterion is that every indexed chunk
has had an extraction *attempt*, not that every chunk has rows: a chunk whose
text yields no entities correctly stores nothing (`server/tools/entity.py`), so
"rows exist for every chunk" would be unsatisfiable by construction. Whether
stores written before that change are backfilled or left to a manual scan is
part of #2145's scope, and this trigger is not met while the answer for an
existing store depends on someone having run one.

Issue #2145 tracks that work; it did not exist when #2133 was filed, which is
why #2133 as written could never be unblocked by an event.

**Half of the trigger has now fired.** #2145 wired the regex extractor into the
indexing engine's chunk-write path (`indexing.extract_entities`, on by default),
so every chunk the indexer writes gets an extraction attempt and a re-index
re-extracts rather than only cascading the old rows away. What #2145
deliberately left open is the other half named above: stores written before it
are not backfilled, so for those the answer still depends on someone having run
`mem_entity_scan`. Status therefore stays *Proposed* — flipping it needs the
backfill question decided, not just this note. The `set_chunk_entities` comment
this ADR called out has been corrected in the same change.

Until the trigger fires, implementing D1–D3 would ship a query that returns
nothing on every default install — the failure mode #2133 itself predicted.
After it fires, D1–D3 are a small read-path change with no schema cost.

Note that #2145 landing is necessary but says nothing about ranking:
`entity_boost.enabled` defaults to `false` and flipping it is a separate
decision with its own blast radius (it changes ranking for every existing user
and invalidates Quality Lab fingerprints). This ADR does not depend on that
flag — the derived view reads `chunk_entities` directly.

## Consequences

- Nothing to migrate. ADR-0012 named `chunk_links`, `access_log`,
  `chunk_entities` and `chunk_relations` as the "FK neighborhood" every cross-DB
  or migration path must explicitly preserve or remap, and left that problem
  open. A stored edge keyed on `chunk_id` would have joined that set directly;
  one keyed on `(session_id, entity_type, entity_value)` would have avoided it
  but bought a different problem (see Alternatives). A derived view has neither.
- This ADR's own bias, not a rule inherited from elsewhere: prefer not to add a
  second durable copy of something the store can already answer. ADR-0011 §4
  calls the database derived state — embeddings, FTS rowids, dedup hashes, the
  chunk-link graph — but it does not forbid durable operational state, and the
  database plainly holds some: sessions, session events, query history,
  policies, and (as of #2132) maintenance-run history. The argument here is that
  this particular answer does not need to become one of them, not that it would
  be forbidden.
- Consistent with how ADR-0028 rejected namespace nesting: a schema-level axis
  lost to convention because the existing shape already met the need, and
  threading a new axis through every derivation site was not worth it.
- Reads stay unrecorded. Anyone who wants "what did this session read" must
  file a separate issue proposing hot-path instrumentation, with its own
  marker and its own answer for what a partial record means.
- Sibling issue #2134 (candidate review shows no evidence) takes the same
  derive-at-read-time stance for the structurally identical problem in the
  formation layer. The two are a deliberate pair. Where #2132 went the other
  way and stored a record, the distinction is about what survives the operation
  it describes: `auto_expire` deletes the very rows that would have been its
  evidence, so a maintenance run is unrecoverable the instant it finishes,
  while a session's written chunks normally outlive the session. "Normally" is
  the honest word — delete a written chunk, or re-index it after an edit, and
  the derived answer loses it too. So this is a difference of degree, not of
  kind, and it is the reason the feature is scoped to "entities currently known
  for chunks this session wrote" rather than to a durable historical record. A
  handover answer that must survive deletion of its own subject matter is a
  different feature, and would need the snapshot this ADR declines to build.

## Alternatives considered

**A stored `session_entities(session_id, entity_type, entity_value, …)` row
set, written at `mem_session_end`.** Precise, and — because it names entity
values rather than chunk ids — it would survive the source chunks being deleted,
which is exactly the durability the derived view lacks. Rejected on three
grounds. It disagrees with `chunk_entities` after any re-index: the snapshot
would keep asserting entities the store no longer has, and a handover answer
that is confidently wrong is worse than one that is visibly narrower. It puts a
new write on `mem_session_end`'s effectful phase, which already needed #1571 to
stop it double-writing on retry. And it commits to a second definition of "the
session's entities" that must then be kept in step with the first. Note the
FK-neighborhood objection does *not* apply to this shape — keyed on values, it
would not reference `chunks(id)` at all; a variant keyed on `chunk_id` would,
and would inherit ADR-0012's deferred problem on top of everything above.

**Recording touched entities as tags on the session's chunks.** Already tried
and rejected once in this codebase for provenance: the comment introducing
`chunk_links` (`storage/sqlite_schema.py`) records that tag-only provenance
"does not benefit from an index and breaks on UUID churn (reindex re-issues
chunk ids)". That reasoning applies unchanged here.

**Reusing `chunk_relations` with a new relation type.** It cannot express this
edge at all: both `source_id` and `target_id` are foreign keys to `chunks(id)`
(`storage/sqlite_schema.py`), and neither a session nor an entity is a chunk.
Even setting that aside, the table would have been a poor host — `relation_type`
is free-form TEXT with no validation, and `add_relation` uses `INSERT OR
REPLACE` against a `(source_id, target_id)` primary key, so a second relation
between the same ordered pair silently overwrites the first. (Contrast
`assertion_edges`, which validates a closed vocabulary — the inconsistency
between the two is real and not resolved here.)

**Defining "touched" to include reads.** The most useful answer for handover,
and the most expensive: no capture exists on any read path, so it requires new
instrumentation on the search hot path, and it would make a claim wider than
anything the project currently promises about session summaries. Deferred to
its own issue rather than smuggled in under a word.

## References

- Issue #2133 — this decision's subject; stays open as the tracking issue.
- Issue #2145 — the precondition: extract entities at index time by default.
- Issue #2134 — sibling, same derive-at-read-time stance in the formation layer.
- Issue #2132 — sibling that stored a record instead; see Consequences for why.
- #1876 (write-provenance events), #1913 (`summary_provenance` persisted),
  #1571 (`mem_session_end` effectful phase), #1732 (unused assertion layer).
- [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §4 — the memory DB is
  derived state; one DB, per-row scope tag.
- [ADR-0012](0012-cross-db-memory-migration.md) — the FK neighborhood, deferred.
- [ADR-0028](0028-per-project-agent-team-namespaces.md) — convention over a new
  schema axis; session-level binding deferred to #1478.
