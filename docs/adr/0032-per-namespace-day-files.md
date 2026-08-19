# ADR-0032: One day file per namespace, and namespace preservation on re-index

**Status:** Accepted
**Date:** 2026-08-03
**Context:** A namespace is stored per chunk, but it is applied per
`index_file` call — and `index_file` re-chunks the whole file. Every chunk the
re-chunk *changes* is upserted with the calling namespace, so an append that
merges with an earlier entry takes that entry's chunk with it. The earlier
entry's namespace is overwritten, silently, while both writes report the
namespace they were asked for (#2005).

Merging needs two conditions: chunks short enough to fall under
`min_chunk_tokens`, and a shared `heading_hierarchy`. Untitled entries stopped
sharing one when auto-headings gained a per-entry unique suffix (see the
Unreleased CHANGELOG entry above it), so what remains reachable is entries
written with the same explicit `title`, and any write into an explicitly named
`file`. Both are ordinary: since #2004 a CLI write inherits the active
session's agent namespace, so a day file holding two namespaces is the result
of a normal workflow rather than something a user opts into.

## Decision

### 1. The default day file is per namespace

`{date}.md` is now only the default namespace's file. Any other namespace
writes to `{date}--{slug}-{digest}.md`. Merging cannot cross files, so this
removes the whole class of loss on the path where it is most reachable —
by construction, not by a check that has to be right every time.

The name is *slug plus digest*, and the digest is not optional:

- The slug is lossy. Namespaces may hold characters no filename can
  (`agent-runtime:planner`; `:` is invalid on Windows), so anything outside
  `[A-Za-z0-9._-]` becomes `-`. Without a digest, `a:b` and a literal `a-b`
  would share a file — reintroducing the bug for the pair.
- The slug is not case-distinguishing on the default macOS and Windows
  filesystems, where `Foo` and `foo` name one file. The digest is over the
  exact bytes, so a case-only difference still separates.
- Filename components cap at 255 bytes, so the slug is truncated on a UTF-8
  boundary — one more way two namespaces would collide.

The namespace is only authoritative once resolved *inside* the write lock
(#1991), but the lock is keyed to a file whose name depends on it. So the
surfaces guess pre-lock to pick a file, re-resolve in-lock, and re-acquire on
the right file if the two disagree — bounded, and before anything durable or
any idempotency claim, so an abandoned attempt leaves no trace.

### 2. Writes that would mix namespaces in one file are refused

The default target is now safe by construction, but an explicit `file=` and
the day files written before this change are not. Before appending, and under
the same lock as the write, the surfaces compare the namespace the write will
be stamped with against the ones the target already holds, and refuse on
disagreement. `allow_namespace_mix=true` (`--allow-namespace-mix` on the CLI)
opts into the old behaviour.

The guard reads the file *and* the store, because neither alone is the answer:

| Target | Rows | Outcome |
|---|---|---|
| missing / empty | any | allow — rows are stale, there is no content to protect |
| has content | none | allow — unindexed text has no stored namespace to lose, and this is also the "appended, then indexing failed" state whose keyed retry must report the pending claim |
| has content | agree | allow |
| has content | disagree | **refuse** |
| has content | query failed | **refuse** — the guard cannot tell the safe case from the unsafe one, and the failure it exists to stop is silent data movement |

It runs *before* the idempotency claim. A refusal is an ordinary outcome, and
releasing a won claim is best-effort by design, so claiming first would let a
swallowed release strand the key for the ledger's full TTL.

### 3. A re-index with no caller namespace preserves the stored one

Without this the fix would last exactly one file modification. The watcher and
`mem_edit` / `mem_delete` call `index_file` with no namespace at all, and the
rules/default fallback would restamp every changed chunk — moving a
`{date}--aaa-*.md` file's contents back to `default` on the next edit.

So when a caller passes no namespace, a file whose chunks are **unanimously**
stored under one namespace keeps it. Unanimity is the whole rule: a file
holding several is the ambiguity this issue is about, and there is no
per-line provenance to split it by, so those fall through to normal
resolution.

`force=True` skips preservation deliberately — it is the documented way to
apply changed namespace rules to an already-indexed file, and preserving
there would make that impossible. That leaves a trap for callers who use
`force` for its *other* effect (re-embed everything) without wanting
re-resolution, and the web chunk-delete path was exactly that: deleting one
chunk from an `aaa` file restamped every survivor through today's rules. Such
callers now read the file's namespace with `effective_namespace_for` and pass
it explicitly, so `force` keeps meaning "re-embed everything" without also
meaning "re-namespace everything".

> **This paragraph is superseded (2026-08-19, #2061) by
> [ADR-0033](0033-force-reembed-vs-namespace-reassignment.md).** The trap it
> predicts was sprung by this project's own documented recovery procedure:
> `mm embedding-reset --mode apply-current` followed by `mm index --force
> <memory_dir>` moved every agent-scoped chunk to `default`. Patching callers
> one at a time to pass the namespace explicitly never reaches the two
> surfaces a *user* drives. `force` now preserves for every caller that
> passes no namespace — an explicit one still wins, which includes the
> session namespace `mem_index` inherits — and rule re-application moved to an
> explicit `--reassign-namespaces`. The rest of
> this section — the unanimity rule, the single resolver, and raising rather
> than falling back on a lookup failure — stands unchanged.

One resolver answers "what namespace will this file's chunks get":
`IndexingEngine.effective_namespace_for`. `_index_file` uses it, the add
surfaces' guard uses it — a guard resolving through the rules alone would
refuse writes the indexer would have preserved — and so does the
`resolved_namespaces` echo, which would otherwise report a namespace the
write did not use. A lookup failure raises rather than falling back to the
rules: quietly re-resolving on a transient read error is the exact silent
move this rule exists to prevent.

## Alternatives considered

**Carry the stored namespace onto changed chunks.** Rejected: a merged chunk
holds content from more than one namespace, so there is no single correct
value to carry. Any tie-break re-introduces silent misattribution with extra
machinery on top.

**Warn instead of refuse.** Rejected: the data still moves. A warning after
the fact does not restore the earlier entry's namespace, and the whole
complaint in #2005 is that the move is invisible.

**A directory per namespace.** Rejected: `agent-runtime:planner/` is not a
legal directory name on Windows, and a sanitized one would feed
`enable_auto_ns`'s parent-folder derivation and the `memory_dirs` registry. A
sibling file in the already-registered directory touches neither.

## Consequences

- Non-default namespaces write to new filenames. Nothing reads day-file
  names — `mem_read` / `mem_list` work off stored `source_file` values, and
  recall/timeline format `created_at` — so this is a naming change, not a
  contract change.
- Day files written before this change may hold several namespaces. They are
  not migrated; the guard refuses further mixing into them, and the
  preservation rule leaves a unanimous one alone. Splitting an already-mixed
  file is left for a possible `mm doctor` check.
- `mm agent share` writes to its target namespace's day file rather than the
  plain one. It indexes with `namespace=<target>` and so always had this
  hazard, despite taking no namespace flag of its own.
- One extra session read per CLI add (pre-lock, to pick the file). The in-lock
  read still decides.
