# ADR-0043: A rebuild restores a day file's namespace from its name, and entries' creation time from `> created:`

**Status:** Proposed
**Date:** 2026-11-04
**Tracking:** #3051

## Context

A memory's namespace is stored per chunk row. ADR-0033 recorded the consequence for
`--force` ("the chunk rows the re-index overwrites in place are the only record there
ever was") and fixed that path by not re-resolving stored namespaces. It left the other
way to lose the rows: the database itself is gone. Deleting the store and re-indexing,
or indexing a synced memory directory on a second machine, gives every file the
namespace its path rules give it — or, for `mem_index` in a bound MCP session, the
session's namespace. With the stock configuration that is `default`, so a handoff saved
under `shared:<project-slug>` is no longer found by the handoff workflow's Resume, which
reads an empty result as "the id does not exist" (#3051, reproduced on `8eb4ce69`).

The same rebuild sets every chunk's `created_at` to the re-index time, although each
entry's `> created:` line holds its write time.

ADR-0033 rejected recovering the namespace from disk: "the name encoding is one-way, and
frontmatter would only help files written after it ships". The second half still holds.
The first is weaker than it looked. ADR-0032 names a non-default day file
`{date}--{slug}-{sha256(ns)[:16]}.md`, where the slug replaces every character outside
`[A-Za-z0-9._-]` with `-` and then trims `-` from both ends. The namespaces memtomem
itself produces — `shared:<project-slug>`, `agent-runtime:<id>`, team and rule-derived
names — are built from segment characters and the `:` between segments. For those the
slug *is* the candidate: turn some of its `-` back into `:`, put back a trimmed edge, and
check each candidate against the digest. The write gate accepts more (spaces, Unicode,
`@`), and those names do not decode; §3 lists them. The digest is a 64-bit truncation, so
two candidates matching would take a collision in 64 bits — negligible for accidental
recovery, and no authenticity at all, which §4 relies on.

Measured on `8eb4ce69` with `day_file_name`, recovering by that search:

| namespace | file name | recovered |
|---|---|---|
| `shared:repo-demo` | `…--shared-repo-demo-9e5d158e8745ca45.md` | exactly it |
| `agent-runtime:planner` | `…--agent-runtime-planner-c9e1624597c33d09.md` | exactly it |
| `claude:-Users-pdstudio-Work-agent-harness-memtomem` | `…--claude--Users-…-72283fcd2642baa7.md` | exactly it |
| `a:b` / `a-b` | `…--a-b-6783a31eabf68ccc.md` / `…--a-b-d44362d67d921091.md` | each exactly itself |

Search cost by number of `-` in the slug: 2^12 candidates in 0.006 s, 2^16 in 0.106 s,
2^20 in 1.9 s. A prototype of §1's order (fewest `:` first, trimmed edges put back,
2^16 cap) recovers `shared:repo-demo` in 22 candidates, `shared:repo-copy-` in 23 and
`agent-runtime:planner-` in 44; it gives up on `a-b:c-d:…:s:t` at the cap and on
`shared:my project` after 84, and both fall back as today.

The first draft of this ADR recorded the namespace in each entry's header blockquote
instead. Two design-review rounds found nine defects in that carrier — placement next to
template metadata, header-shaped quotations in bodies, the full-replacement edit path that
drops the header by contract, mixed files through ordinary appends — all of them
consequences of adding a new in-content syntax. It is kept under Alternatives.

## Decision

The two halves ship as two stacked pull requests: the namespace (§1–§4) first, because it
is what the handoff workflow needs, then the creation time (§5), which changes ordinary
edits as well as rebuilds.

### 1. A day file's name declares its namespace

`decode_day_file_namespace(path)` returns the namespace a file name declares, or nothing:

- The name must match `{YYYY-MM-DD}--{slug}-{16 lowercase hex}.md` exactly. Any other
  name — including `{date}.md` and a digest-only `{date}--{digest}.md` — declares nothing.
  A matching name declares its namespace whoever wrote it (§3).
- Candidates are the slug with a subset of its `-` turned into `:`, tried in order of how
  many are turned (fewest first), each also with up to two trimmed trailing characters and
  one leading character put back (`-` or `:`). The handoff workflow trims only leading `-`
  from its project slug, so `shared:repo-copy-` is a real shape. The first candidate whose
  `sha256[:16]` equals the digest wins. The search stops after 2^16 candidates and the
  file declares nothing — a short name with many segments and many hyphens can exhaust it
  (`a-b:c-d:…:s:t` needs ten turns among nineteen positions).
- A slug of `_NS_SLUG_MAX_BYTES` bytes may be truncated and is not decoded.
- A match is accepted only if `validate_writable_namespace` accepts it.
- Results are memoised per `(slug, digest)`: a namespace's day files share both, so a
  rebuild searches once per namespace, not once per day.

The function is pure on the name and reads no file content. It is called from
`_namespace_decision`, the one place preview (`resolve_namespaces_for`), the bulk and
stream prepasses and the in-lock decision share, and not from `_resolve_namespace_directed`,
so `--reassign-namespaces` still means the rules. It decodes the **resolved** path's name,
the one `_index_file` decides on (`decision_path`): `_discover_files` keeps symlink names,
so a day-file-shaped symlink to `notes.md`, or the reverse, would otherwise preview one
namespace and index another. Tests cover the alias both ways for preview, bulk, stream
and single-file indexing.

### 2. Resolution order

For one file:

1. explicit `namespace=` from the caller (unchanged: it wins everywhere);
2. `reassign_namespaces=True` → path rules (unchanged: the user asked for the rules);
3. stored rows (unchanged: ADR-0032 preservation);
4. **the name's declared namespace** (new) — only for a source with no stored rows;
5. session-bound `new_source_namespace` (unchanged, #2104);
6. path rules → auto namespace → `default_namespace` (unchanged).

The declaration sits above the session binding so that `mem_index` in a bound session
rebuilds a tree into the namespaces it was written in. It sits below stored rows, so a
file whose rows were moved keeps the moved namespace for as long as the rows exist.

The decision gets a new reason (`declared_by_name`). `IndexingStats` gains a count of
files restored this way and a count of names that matched the day-file pattern but did
not decode; an undecodable name falls back exactly as today and never fails the file.
The counters are rendered by hand on each surface, as ADR-0033's were: the MCP
`mem_index` result, `mm index` output, the `/api/index` response model, the `/api/reindex`
per-root dict and its JS consumers. `test_index_advisory_consumers.py` only finds the web
reporter call sites, so each surface gets a behavioural test, including an undecodable
name whose final reason is a fallback.

### 3. What it covers, and the name it reserves

Every non-default day file written since ADR-0032 whose namespace is made of segment
characters and `:` — files already on disk included, which no in-file record could reach.
That is the default target of `mem_add`, `mem_batch_add`, `mm add` and the handoff
workflow, which passes a namespace and no `file`.

The pattern becomes reserved routing syntax, whoever wrote the file. A file *named* like a
day file declares its namespace however it got there — an explicit `file=`, a copy, an
import. Two ordinary flows change:

- `mem_add(file=<a name that decodes to shared:x>)` with no namespace and no session or
  current-namespace binding, into a row-less file, now lands in `shared:x` instead of the
  rules' namespace. Under a binding, `mem_add` passes the bound namespace as explicit
  intent, so a different binding meets the mix guard below.
- A write naming another namespace into such a file would be indexed under that namespace
  now and under the decoded one after a rebuild. The ADR-0032 mix guard refuses it instead
  (`namespace_mix_refusal` compares the write's namespace with the name's declaration,
  rows or not), and `allow_namespace_mix` overrides as it does today.

It does not cover:

- files whose names are not day-file shaped: most explicit `file=` targets, and writers
  that name their own files (session summaries, `mem_fetch`, consolidation summaries,
  `langchain-memtomem` projections);
- `{date}.md`, the default namespace's day file. It rebuilds through the rules as today,
  which gives `default_namespace` unless a rule or a bound MCP session says otherwise;
- day files from before ADR-0032, which could hold several namespaces under one name;
- namespaces whose slug was truncated, that contain characters other than `:` outside
  the slug set, that lost more than the edges §1 puts back, or whose search exhausts the
  candidate budget.

### 4. Namespace moves after writing stay database-only

`mem_ns_rename`, `mem_ns_assign`, the archive and promote policies, `--force
--namespace` and `--reassign-namespaces` change rows and do not rename files. After a
rebuild those chunks return to the namespace their day file was named for. That is the
tag model already documented (the file holds what was written; later edits are
database-only). Concretely: rename `shared:old` to `shared:new`, lose the database,
rebuild, and the entries are back in `shared:old`, reachable by queries that name the old
namespace; an archived chunk comes back in its original namespace. Today the same rebuild
puts both in `default`, so this is not a new exposure, but it is a different destination,
and the docs say so.

The declaration is routing, not authority. Anyone who can put a file in a memory
directory can already choose its content; naming it chooses its namespace too, which
changes default visibility (`archive:*`) and which agent's scope it lands in
(`agent-runtime:*`). Namespaces are not an access boundary (ADR-0036; the server
instructions call them "per-agent routing scope (not an access boundary)"), they grant no
project scope, and the privacy gate runs on every write regardless. In a multi-user
deployment, which namespaces a source may land in has to stay subordinate to the
deployment's tenant and source authority; a file name cannot establish it.

### 5. `created_at` comes from `> created:` (second pull request)

The chunker gives a chunk built from an entry with a parseable `> created:` line that time
as `created_at`. A chunk that merges entries takes the earliest; chunks split from one
entry inherit it; an entry without the line keeps the index time. An entry whose content
opens with its own `## ` heading has its header written above that heading, where it leads
no section of the entry; its tags already go unapplied (#2762), and its time survives only
when chunk merging happens to join the header's preamble to the entry.
Fixing that placement is #2762's, not this ADR's. **Amended 2026-11-04 (#2762):** the
writer now puts that header directly under the content's heading, so entries written from
then on are dated and tagged like any other; entries already on disk keep the old layout.
The time travels as
`ChunkMetadata.entry_created_at`, not on `Chunk`: `_add_overlap` and hard-budget splitting
in `chunking/bounded.py` rebuild `Chunk` objects but carry metadata through
`dataclasses.replace`, `_merge_pair` takes the earlier of its two, and `chunk_content`
copies the value onto `Chunk.created_at` once, at the end.

SQLite's update path does not write `created_at`, so a chunk whose id is reused keeps its
stored value. But editing an entry's body changes its hash and gives it a new id, so this
is **not** rebuild-only. Today an edited entry gets the edit time as `created_at`; after
this change an edit that keeps the header group keeps the time it was written. A
full-replacement edit whose content opens with `## ` drops the header by contract
(`test_mem_edit_header_preservation.py`), so that entry still gets the edit time; the
second pull request's acceptance cases include it. What reads `created_at`:

- date-ranged recall (`created_from` / `created_before`) and newest-first ordering — an
  entry edited this week no longer shows up as written this week;
- dedup's keep-the-oldest choice;
- age-based policies whose `age_field` is `created_at` (the default) — editing an old
  entry no longer resets its age.

Time decay reads `updated_at` and is unchanged. The CHANGELOG states the edit behaviour
under `Upgrading`.

## Consequences

- No writer, file format, or edit path changes. Older releases are unaffected.
- A rebuild, a second machine, or a bound session's `mem_index` restores the namespace of
  every covered day file, including files written before this release.
- `docs/guides/reference.md` "What a rebuild restores" changes with each pull request:
  which files get their namespace back, and the rename case of §4.
- Follow-ups, each its own issue if wanted: covering explicit `file=` targets and the
  self-naming writers; deciding whether `{date}.md` should declare `default_namespace`.

## Alternatives considered

**Docs only.** Shipped as PR #3053. It does not stop the handoff workflow from reporting a
saved handoff as missing after a rebuild.

**A `> namespace:` line in each entry's header group.** Covers explicit `file=` targets,
but only for entries written after it ships, and adds an in-content syntax with
obligations on every writer and editor: placement beside template metadata, header-shaped
quotations inside bodies, the full-replacement edit path that drops the header by
contract (`test_mem_edit_header_preservation.py`), and mixed lines from an ordinary append
after `mem_ns_assign`. Two review rounds produced nine defects in it.

**Frontmatter `namespace:` key.** Measured on `8eb4ce69` to become a chunk of its own
(`'---\nnamespace: shared:repo-demo\n---'`) in every released reader.

**A sidecar map of file → namespace.** File-level, but a second record that every move
and every sync has to keep consistent with the rows, and one more file in the user's
memory directory.

**A reversible encoding in new day-file names.** Would also decode truncated or unusual
namespaces, but changes the name of every new day file and does nothing for files already
on disk, which the digest search recovers.
