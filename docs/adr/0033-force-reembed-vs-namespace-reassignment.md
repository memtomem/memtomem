# ADR-0033: `--force` re-embeds; reassigning namespaces is its own flag

**Status:** Accepted
**Date:** 2026-08-19
**Supersedes:** [ADR-0032](0032-per-namespace-day-files.md) §3, final paragraph
**Context:** `--force` carried two unrelated meanings. It re-embeds every
chunk — the documented recovery after an embedding change — *and* it skipped
namespace preservation so changed path rules would take effect. ADR-0032 named
this "a trap for callers who use `force` for its *other* effect" and patched
the internal callers one at a time by having them pass the preserved namespace
explicitly.

The trap was then sprung by the project's own documented procedure (#2061).
`mm embedding-reset --mode apply-current` followed by `mm index --force
<memory_dir>` is what the guides tell a user to run after an embedding switch,
and on a store holding agent-session notes it silently moved every one of them
to `default` — collapsing exactly the isolation the session feature exists to
provide. Non-force indexing escaped only by accident, via the unchanged-hash
skip.

Nothing on disk can undo it. The day-file name encodes the namespace one way
(`{date}--{slug}-{sha256[:16]}.md`; the slug is lossy and the digest is a
checksum), and no frontmatter carries it, so the chunk rows the re-index
overwrites in place are the only record there ever was.

## Decision

### 1. The two meanings split

`force` re-embeds without re-resolving namespaces. (It still re-resolves
ADR-0011 *scope* — a separate axis, unchanged here.) A file whose chunks are
stored under one unanimous namespace keeps it, for every caller that passes no
namespace. A new `reassign_namespaces` flag (`mm index --reassign-namespaces`)
is the opt-in that re-resolves through the current path rules.

The direction of the split follows from which mistake is recoverable:

| accident | consequence |
|---|---|
| re-embed that also re-namespaces | silent, unrecoverable data movement |
| rule application that does not happen | rules are not applied; re-run with one more flag |

The destructive direction is the one that has to be asked for by name. A
deprecation cycle would have meant shipping the data-moving default for
another release to protect the recoverable half, which is backwards — and
this is pre-1.0.

### 2. `reassign_namespaces` implies `force`

Unchanged files skip the upsert, so applying rules without force would migrate
only the files that happened to have changed since the last index: a silently
partial migration, worse than either whole. The cost is that a namespace-only
migration re-embeds everything — precisely what the old `--force` workflow
already cost, so no one is worse off. A metadata-only upsert bucket that
retains existing vectors is a possible follow-up; it is not required for
correctness here.

### 3. A forced re-index of a multi-namespace file is refused

`force` promotes every unchanged chunk into `to_upsert`, and one upsert
carries one namespace. On a file whose rows span several — the pre-existing
ambiguity ADR-0032 §3 leaves to normal resolution — that would rewrite all of
them to a single value, which is the #2061 collapse in miniature and can move
agent-scoped content into a searchable namespace.

So it fails closed, permanently rather than retryably (re-running changes
nothing), naming the outs: an explicit `--namespace` to choose the target,
`--reassign-namespaces` to apply the rules on purpose, or splitting the file.
Placement matters twice:

- The refusal lives in the *write*, not in the bulk prepass. Raising during
  the prepass would abort the whole run before per-file error handling, so one
  unsplittable legacy file would block indexing every other file in the tree.
- It runs *after* the empty-source cleanup. An emptied file writes no
  namespace, so there is nothing to collapse, and refusing first would strand
  a deleted file's chunks as permanently searchable content.

The message carries the file's bare name. Bulk error strings are echoed
verbatim through the web complete event and API responses, which redact host
paths; the absolute path goes to the server log.

A caller that pins the namespace explicitly *bypasses* this refusal — an
explicit namespace is caller intent and always wins. That is a hazard for the
one internal caller that pins on a forced re-index: web chunk deletion, whose
pin exists for the §3 reason above. On a legacy multi-namespace source the
resolver hands it the rule-resolved target, and pinning that would rewrite
every survivor into it — the collapse, reached through an ordinary delete. So
that caller asks for the *decision* rather than the value
(`namespace_decision_for`) and refuses the delete (409, before touching the
file) when the re-index would have refused on its own. Any future caller that
pins under `force` inherits the same obligation.

### 4. What a run did to namespaces is reported, not inferred

Each file's authoritative in-lock resolution produces a decision record
(target, stored namespaces, reason), and the run rolls those up into
`IndexingStats.namespaces_preserved_against_rules`,
`namespaces_reassigned`, and a `namespace_moves` summary. Three consequences
are deliberate:

- The pre-write prepass is a preview and never the reporting source; a
  concurrent writer can invalidate it, and counting from it would double-count
  or report files that never wrote.
- A move counts only once its upsert has committed, and the decision is
  captured immediately after that commit — the AI-summary refresh that follows
  is fail-soft, so a failure there cannot erase a move that really happened.
- `None` and `"default"` are canonicalized before comparison. They are one
  state under two names, and a raw comparison would report files that never
  moved.

`preserved_against_rules` is the migration bridge that replaces a deprecation
window: a user whose rule edit no longer takes effect under `--force` is told
so, and told which command applies it. Because the counters live in
`IndexingStats`, every force surface can report them — but each hand-built
adapter (the MCP result string, the per-root `/api/reindex` entries, the
`/api/index` response model) had to be updated to actually do so.

### 5. Reassignment stays CLI-only for now

`mem_index` gains no parameter: the core tool descriptions are at their
character budget (`test_core_tool_descriptions`), and a rare, destructive
migration does not earn budget that every call pays for. MCP and web callers
keep `force` with its now-honest meaning and receive the preservation
advisory, which names the CLI command.

One caller once bounded the "every caller" claim: inside an agent session,
`mem_index` resolved the session's namespace and passed it as an *explicit*
one (#2004), which short-circuited preservation the way any explicit namespace
does, so `mem_index(force=true)` under a session stamped everything it indexed
with `agent-runtime:<id>`. That predated this ADR and was not what #2061
reported — the reported path is the CLI recovery command, which passes no
namespace — and it is why this ADR originally said preservation holds "for
callers that pass no namespace" rather than "always".

#2104 closed it by splitting the two intents at the tool boundary. A namespace
the caller *names* is still explicit and still wins; a namespace inherited from
session context now travels in a separate `new_source_namespace` slot that the
engine applies only to sources with no stored rows. The #2004 contract is
intact — a file the session writes is still bound to `agent-runtime:<id>` —
while a bulk re-index can no longer move content the session never wrote, and a
mixed-namespace file reached from a session gets the same force refusal as
everywhere else. Preservation now holds for every caller that does not name a
namespace, session or not.

## Alternatives considered

**Preserve only system-scoped namespaces (`agent-runtime:`, `archive:`) under
force.** Rejected. It fixes the headline of #2061 while leaving the same bug
class for everyone else — a file indexed as `personal` still gets restamped to
`default` by the documented recovery — and it would make a *search* setting
(`search.system_namespace_prefixes`) decide *write* behavior.

**Keep the behavior and warn.** Rejected for the reason ADR-0032 rejected it
for #2005: the data still moves. A warning mid-scroll of a bulk-index progress
log is not a control, and this is a procedure users copy verbatim from the
guides.

**Recover the namespace from disk** (parse the day-file name, or write a
`namespace:` frontmatter key). Rejected as the fix for this issue: the name
encoding is one-way, and frontmatter would only help files written after it
ships — neither restores what a re-index is about to overwrite.

## Consequences

- **Breaking:** `mm index --force`, `mem_index(force=true)`, and the web bulk
  force paths no longer re-resolve namespaces. Users applying changed rules
  must pass `--reassign-namespaces`; the advisory names it on the first run
  that would have moved something.
- **Breaking (Python API):** `IndexEngine.effective_namespace_for` keeps its
  `force` keyword with the new meaning and gains `reassign`; the bulk and
  single-file entrypoints gain `reassign_namespaces`. No compatibility alias
  — pre-1.0, and a silently reinterpreted flag is worse than an error.
- `--reassign-namespaces` is rejected alongside `--namespace` (they name
  different targets) and alongside the debounce modes (the queue entry carries
  only path/namespace/force, so it would drain as a plain forced index — which
  now preserves, the opposite of the request). The engine enforces the first
  of those itself: its entrypoints are public API.
- The web chunk-delete path's explicit preserved-namespace pass (ADR-0032 §3)
  is now redundant but still correct, and is kept.
- Legacy multi-namespace day files are surfaced rather than silently
  flattened: a forced re-index now names them. Splitting them remains the
  possible `mm doctor` check ADR-0032 anticipated.
