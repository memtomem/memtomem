# ADR-0036: Chunk ids address, they do not authorize — scope is a boundary, namespace hiding is not

**Status:** Accepted
**Date:** 2026-08-29
**Extends:** [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §6 — the
project-aware search default, from retrieval to id-addressed access
**Qualifies:** [ADR-0028](0028-per-project-agent-team-namespaces.md)
§"Consequences" — the "routing scope, not an access boundary" statement, now
with the scope axis contrasted against it

## Context

memtomem screens what a search returns along two axes:

- **System-namespace hiding.** `search.system_namespace_prefixes` (default
  `archive:`, `agent-runtime:`) keeps those buckets out of a `namespace=None`
  search, and an explicit `namespace=` reveals them again
  (`constants.py`, `models.py:NamespaceFilter.parse`).
- **The ADR-0011 project boundary.** A scope-context fragment is appended to
  every `bm25_search` / `dense_search` / `recall_chunks`, even when
  `scope_filter=None`: inside a project, `scope = 'user' OR project_root = <X>`;
  outside one, `scope = 'user'` (`storage/sqlite_scope.py`).

#2192 found that context-window neighbours reached the caller without either
rule, because neighbours are read in bulk by source file rather than through a
filtered query. #2233 fixed that by re-stating both rules in `search/visibility.py`
for adjacency-reached chunks, and carved out `mem_expand`'s anchor: naming an id
was read as an explicit request for that chunk's surroundings. #2236 tested that
premise and it did not hold — several surfaces hand out full ids for chunks the
default rules hide, with no argument to opt in with — so #2239 removed the
carve-out.

That left the question those three changes kept deferring, filed as #2238: is
the **id-addressed** surface supposed to be a boundary at all? Today it is not.
`mem_read`, `mem_related` / `mem_link` / `mem_unlink`, `mem_agent_share`,
`mem_expand`'s anchor, `mem_edit` / `mem_delete`, the
`memtomem://chunks/{chunk_id}` resource, the LangGraph store adapter, and web
`PATCH` / `DELETE /chunks/{id}` all resolve a raw `get_chunk` and return or
mutate whatever comes back. Web `GET /chunks/{id}` is the one exception: it
hydrates through `recall_chunks(chunk_ids=…, project_context_root=…)` under the
comment "Knowing an id is not authorization". The codebase therefore ships both
answers, on two routes of the same web router.

Ids are not guessable — `Chunk.id` is `uuid4` — but they do not have to be.
`mem_dedup_scan` prints full ids and content previews over a corpus built from
every source file; `mem_export` emits full content with an exact-equality
namespace filter and no scope filter; `mem_consolidate_candidates` persists full
ids to scratch, readable through `mem_scratch_get`; web `GET /chunks?source=`
returns complete records. Possession of a UUID says nothing about what the
holder was ever allowed to see.

## Decision

**The two axes get different answers, matching their documented purposes.**

### 1. The ADR-0011 project boundary applies to id-addressed access

Every ordinary (agent-tier) surface that takes a chunk id — read *and* write —
resolves it through the same boundary the retrievers apply:

> Outside a project, only `scope = 'user'` rows resolve by id. Inside project
> `<X>`, `scope = 'user'` rows and rows with `project_root = <X>` resolve.

An id outside that boundary is answered exactly as a nonexistent id is:
byte-identical message, same status code, no field that distinguishes them.
Ids address rows; they do not certify that the holder may read one.

This makes the MCP tools agree with web `GET /chunks/{id}`, which has behaved
this way since it was written, and with `mem_search` / `mem_recall` / the CLI /
web `/search` / `/timeline`, all of which have never returned a foreign
project's rows without an explicit `scope=`.

### 2. System-namespace hiding and temporal validity do not

Id-addressed access reaches `archive:*` and `agent-runtime:*` chunks, and
chunks outside their validity window, deliberately. `mem_read` of an archived
chunk keeps working; so does expanding one.

This is not an oversight left unfixed. Those two axes are *retrieval-relevance
defaults*, and the codebase has said so consistently:

- `constants.py` — `agent-runtime:<id>` is "a *convenience* isolation boundary,
  not a security boundary"; the default prefixes are "excluded from default
  `mem_search` … but stay reachable when an explicit namespace is passed", and
  setting `search.system_namespace_prefixes: []` restores pre-multi-agent
  behaviour.
- `server/instructions.py` — "per-agent routing scope (not an access boundary)".
- `server/tools/meta.py` — "anything that can call the server can read it".
- ADR-0028 §"Consequences" repeats it for the `shared:<project>` buckets.

A rule any caller may switch off with one config line, and which an explicit
argument already lifts, is a default and not a boundary. Enforcing it on id
access would break workflows that read archived chunks by id while buying
nothing an attacker cannot undo with `namespace=`.

Temporal validity travels with this half for the same reason: an expired chunk
is stale for ranking, not confidential.

### 3. Maintenance and browse surfaces stay whole-store

The following are **operator-tier** and are not screened:
`mem_dedup_scan` / `mem_dedup_merge`, `mem_decay_scan` / `mem_decay_expire`,
`mem_cleanup_orphans`, `mem_export` / `mem_import`, the consolidation
candidate/apply pair, and the web browse routes `GET /chunks?source=` and
`GET /sources`. They exist to service the one physical store — a single SQLite
database whose rows span every project — and scoping their corpora would make
them unable to do the job they are named for.

Whether they should grow an *optional* scope filter is a real question, deferred
with a TRACKER row rather than answered here.

## What this guarantee is, and is not

**It is a context boundary and leak hygiene.** It stops a foreign project's
content from arriving through an ordinary read, a link preview, or an
`mem_agent_share` copy, and it makes one rule true across search, browse, and
id access instead of two rules that disagree by route.

**It is not confidentiality against the calling agent.** `mem_do` is exposed in
the default core tool mode and dispatches every registered maintenance action,
so a caller that can reach `mem_read` can also reach `mem_export` and read the
whole store. Nothing in this ADR changes that, and no text elsewhere should be
read as claiming otherwise. Real isolation between an agent and another
project's memory would require capability separation over the operator tier —
a different decision, not a stricter version of this one.

The threat this addresses is accident: an id that arrives from a maintenance
listing, a stale note, or another agent's output, pasted into `mem_read` or
`mem_delete` from a session that has no business with that project.

**It is not a race-tight check on every write.** Where a write path already
holds the source file's lock — `mem_edit`, `mem_delete`, and the web
`PATCH`/`DELETE` routes, through `_locked_chunk` / `locked_source_chunk` — the
boundary is judged on the chunk re-fetched under that lock, so a concurrent
`memory-migrate` cannot slip between the check and the write. The remaining
write surfaces are check-then-act: `mem_link` / `mem_unlink`,
`mem_reflect_save`'s relation writes, `mem_increment_access`, the LangGraph
store's `delete`, and per-chunk tag replacement. A migration landing in that
window can re-scope a chunk after it was judged. Closing those needs
boundary-qualified atomic operations in the storage layer (and, for tags, fixes
a separate pre-existing staleness bug — #2241), which is a write-integrity
change rather than a visibility one. So the accident this rule was written
for — a stale id reused in a session with no concurrent migration — is
covered, and an accident that coincides with a migration is not. Neither is a
caller who can time one deliberately, but that caller was never the adversary
here.

**It is a boundary on chunk ids, not on every identifier.** `run_id` — the
handle `mem_search_feedback` and the history tools take — has no boundary of
its own, so one caller's run and its judgments are readable by another.
Recording feedback about a chunk is screened, because that names a chunk id;
scoping the run axis itself is a separate decision, tracked in #2243.

## Consequences

- A server started outside any project can no longer `mem_read` a project-tier
  chunk by id. The way to reach one is to run from that project's directory, or
  to search with an explicit `scope=project_*` — the same requirement retrieval
  has always had.
- `mem_expand` on a foreign-project anchor returns not-found rather than the
  anchor plus a trimmed window. This finishes the direction #2239 started.
- `mem_related` and `mem_reflect` drop out-of-boundary relations from their
  output *and* from their counts, rather than rendering them as dangling ids.
  Relations are `ON DELETE CASCADE`, so a rendered dangling id normally means
  "deleted"; reusing that rendering for a live-but-hidden row would leak both
  the row's existence and its id.
- Write paths (`mem_edit`, `mem_delete`, tag mutation, web `PATCH` / `DELETE`)
  screen too. "Cannot read it but can delete it" is not a defensible split, and
  it costs nothing: the dedup workflow's follow-up is `mem_dedup_merge`, an
  operator-tier tool, never `mem_delete`.
- The check on a mutation path is applied to the fresh chunk re-fetched under
  the lock, not only to the probe before it, so a scope change that lands while
  the lock is held is judged on the value that will actually be written.

## Alternatives considered

**Screen both axes everywhere (issue #2238 option 1).** Coherent, and the
strictest answer, but it breaks `mem_read` on `archive:*` — a workflow that
exists today — and it only means something if the id-emitting surfaces grow
filters or opt-in arguments as well. That is a large change justified by a
confidentiality claim this project cannot make while `mem_do` reaches the
maintenance tier.

**Screen neither; write down that ids bypass everything (option 2).** Cheapest,
zero regression risk, and honest about the maintenance surfaces. Rejected
because it makes ADR-0011 §6's "prevents cross-project leak" false for any
caller holding an id, permanently strands web `GET /chunks/{id}` as an
inconsistent exception, and leaves `mem_agent_share` able to copy another
project's content into a shared namespace — the one id path that does not just
show a foreign row but republishes it.

**Route every id read through `recall_chunks(chunk_ids=…)`,** as web `GET` does.
Right for display reads, wrong as the single mechanism: mutation paths must
re-judge the chunk they re-fetch under the lock, `recall_chunks` rows carry no
embedding (web `/chunks/{id}/similar` needs one), and `mem_expand` needs the
anchor object itself. The boundary is therefore a predicate in
`search/visibility.py` — the module that already exists to be the one place this
rule is re-stated — shared by both flavors.

## References

- Issue #2238 — this decision's subject.
- Issue #2236 / PR #2239 — `mem_expand`'s anchor opt-in, removed; the evidence
  that possessing an id implies nothing was gathered there.
- Issue #2192 / PR #2233 — neighbour visibility; origin of `search/visibility.py`
  and of the visibility-vs-selection split this ADR extends by one axis.
- Issue #2232 — `namespace=[]` disables system-namespace hiding at retrieval;
  a defect on the axis this ADR declines to enforce, and unaffected by it.
- [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §6 — the always-on
  scope fragment, and §4 for one DB with a per-row scope tag.
- [ADR-0028](0028-per-project-agent-team-namespaces.md) — namespaces as
  convention; "not an access boundary".
- `SECURITY.md` — the local-machine trust model these boundaries sit inside.
