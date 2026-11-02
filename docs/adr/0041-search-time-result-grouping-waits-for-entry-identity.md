# ADR-0041: Search-time result grouping waits for an indexing-time entry identity

**Status:** Proposed
**Date:** 2026-11-02
**Tracking:** #3001 (grouping proposal), #3002 (writer fix), #3003 (section-occurrence identity), #3004 (dead fields)

## Context

#3001 reports that one fact can take several `top_k` slots of a `mem_search`
and proposes a pipeline stage that groups results without deleting anything:
chunks of one entry collapse into one result, exact copies across sources
collapse into one result that cites every source, near duplicates stay apart.

The proposal was taken through a design gate (two Codex review rounds on a
written design, no code) and measured against a real store of 21,980 chunks.
The measurements changed the problem:

- **The observed duplicate is a writer defect, not a retrieval one.** The
  issue's first query showed ranks 1 and 2 as "two chunks of the same
  `project_local` entry: the body and its trailing citation paragraph". That
  paragraph is a `Host-native matches (written anyway): …` line the agent
  wrote, after a blank line, when following step 3 of the `update` workflow
  (the step requires the citation; it does not say where it goes). It quotes
  the matched memory's name, so it carries the body's keywords and ranks
  directly under it. 36 such paragraphs sit as chunks of their own across 9
  `project_local` files (#3002).
- **Exact copies are rare and bimodal.** 27 `content_hash` values are shared
  by more than one chunk. The 16 with bodies of 200 or more characters are
  all cross-file, the cross-tier copies the issue expects. The short ones
  include a bare code-fence line shared by four files: identical text, not an
  identical fact. The issue's second query did not observe a cross-tier copy.
- **There is no entry identity to group on.** A chunk carries
  `(source_file, heading_hierarchy, start_line)` and nothing that says which
  *occurrence* of a heading path it belongs to. 236 of the 4,625 multi-chunk
  `(source_file, heading_hierarchy)` keys are a heading path that recurs in
  non-adjacent runs of one file; in Codex memory files it is 66 of 265. Two
  adjacent sections with the same title cannot be told apart at all, by line
  order or by a storage probe for an intervening heading, and that is the
  shape of a `## Decision` that reverses an earlier `## Decision` (#3003).

The review also established what a grouping stage would have to carry, listed
under *Consequences* so it is not re-derived.

## Decision

1. **No search-time entry grouping keyed on the heading path.** Collapsing
   "chunks of one entry" requires a section-occurrence key written at
   indexing time (#3003). Until that key exists and legacy rows have been
   re-indexed, a stage that merges on `heading_hierarchy` would merge
   contradictory sections that share a title, which the issue itself forbids.
2. **Fix the writer first.** #3002 is the proposed mitigation: the `update`
   workflow says where the host-native citation goes so the item is written
   as one paragraph. Whether it then stays one chunk depends on the chunk
   settings and the paragraph splitter, so #3002's acceptance includes a
   regression fixture under the affected settings; the measured case counts
   as removed only when that fixture passes.
3. **Exact-copy grouping is the only search-time grouping with a sound
   identity today, and it is deferred.** It is keyed on `content_hash` with a
   minimum body length, matched member to member, and it is not built until a
   cross-tier copy is observed in a real search. The two queries in #3001 did
   not show one.
4. **Near-duplicate grouping stays out of scope** until a threshold has been
   evaluated against real examples, as #3001 already says.

## Consequences

A future grouping stage, exact-copy or entry-level, inherits these
requirements from the review. They are the contract a spec for it has to
state:

- **Position and budget.** The stage runs after the rerank and after the
  source, metadata and validity filters, before time decay, over the
  already-filtered candidate list only. The final `top_k` cut moves to right
  after grouping. Retrieval legs, the fusion pool and the reranker's output
  limit are widened together; with grouping disabled every budget and
  truncation point is byte-identical to today
  (`TestRerankCandidatePool::test_pool_size_scaling_table` and
  `TestPerCallRerankBypass` keep passing unchanged).
- **Propagation.** Every stage after grouping rebuilds `SearchResult`
  objects, so the group is re-stamped after context expansion at the
  boundary where `via_session_summary` is restored today, and each stage gets
  a test that the group survives it.
- **Membership.** Members come only from the filtered pool, so a chunk a
  scope, namespace, source, tag or validity filter excluded is never a
  representative, sibling or alternate. Exact copies keep which member they
  copy; nothing merges transitively through a shared paragraph. Context-window
  neighbours are reachability, not group members, and are not deduplicated
  against groups.
- **Identity of the result.** The representative is the best pre-modifier
  score, then the storage scope order, then `start_line`, canonical source
  path and chunk id. The group's score is the representative's; a group of
  five copies does not outrank one better match. Ranks are renumbered after
  the cut. Observations and access counts record representatives only.
- **Configuration.** Settings live in a top-level config model beside `mmr`
  and `rerank`, are part of the ranking-profile fingerprint and the
  observation profile, and the cache key carries the effective per-call
  decision. Ranked keyword search only: the tag/source-only enumeration,
  `mem_recall`, web recall, similar-chunk listings and `mem_dedup_*` are
  unchanged.
- **Consumers that change with it**, each with a test: `server/formatters.py`,
  `cli/search.py`, `cli/shell.py`, `server/tools/ask.py`,
  `server/tools/procedure.py`, `server/tools/temporal.py`,
  `integrations/langgraph.py`, `pinned.py`, `web/schemas/core.py`,
  `web/routes/search.py`, `web/static/app.js`, `quality/fingerprints.py`,
  `quality/replay.py`, the retrieval-eval tools, step 3 of the `update`
  workflow (it reads structured output and must look inside groups), and the
  STM surfacing parser in memtomem-stm `surfacing/mcp_client.py`: its compact
  parser takes everything after a result's header line as content, and its
  structured parser reads only the keys it knows, so a new key is dropped.
- **Tests run offline** on `bm25_only_components` or a `SearchPipeline` with
  a stub embedder, pin the fixture's chunk count before searching, assert the
  grouping-off baseline before the grouped result, and compare filter
  eligibility as flattened member ids over a non-truncating limit rather than
  as row-set equality.

## Open questions

- Which occurrence key #3003 persists (heading line number or an ordinal of
  the path within the file), and how legacy rows without it are treated by a
  grouping stage: never as "same occurrence".
- Whether `ContextInfo.ranked_siblings` is removed (#3004) or becomes the
  typed home of entry siblings once #3003 lands. This ADR recommends a
  separate `SearchResult.group` field, because grouping is independent of the
  context window and `context` is `None` when the effective context window
  is zero.

## Alternatives considered

- **Group before the rerank** (between fusion and the cross-encoder). Cheaper
  when a reranker is wired, but the representative is chosen by fusion rank
  rather than rerank score, and grouping would run before the filters, so an
  alternate could be a chunk the caller's filter excludes.
- **Group after all score modifiers**, before context expansion. Every stage
  between would run on the widened pool, and MMR before grouping spreads
  copies apart on purpose.
- **A storage probe for intervening headings** as an occurrence test. Blind
  to adjacent repeats and to an intervening heading with no indexed body.
- **Reusing `search/dedup.py` or `mem_dedup_merge`.** Merging is storage
  mutation, and host-memory sources are read-only; the keeper rule there
  (oldest copy) is the wrong representative for search.
- **Enabling MMR.** It reorders for diversity and removes nothing.

## References

- #3001 — the proposal and its two measured queries
- #3002, #3003, #3004, #3005 — issues filed from the gate
- `search/pipeline.py` module docstring — the fixed stage order (CLAUDE.md,
  *Invariants*)
- ADR-0011 — the project-scope boundary that membership must respect
