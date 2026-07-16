# Organization & maintenance

Organize memories into namespaces and keep the index healthy — deduplication, decay, auto-tagging, and `mm memory doctor`.

[← memtomem Reference](../reference.md)

**On this page**

- [4. Namespace — mem_ns_*](#4-namespace--mem_ns_)
- [5. Maintenance — mem_dedup_*, mem_decay_*, mem_auto_tag](#5-maintenance--mem_dedup_-mem_decay_-mem_auto_tag)

---

## 4. Namespace — `mem_ns_*`

Namespaces organize memories into scoped groups.

### Core workflow

```
mem_ns_set(namespace="work")          # set session default
mem_ns_get()                          # check current namespace
mem_ns_list()                         # list all namespaces with counts
→ default: 200 chunks
  work: 150 chunks
  personal: 94 chunks
```

After `mem_ns_set`, all operations (search, add, index) default to that namespace.

### Namespace metadata

```
mem_ns_list()
→ default: 200 chunks
  work: 150 chunks (description="Company project docs", color="#3B82F6")
  personal: 94 chunks

mem_ns_update(namespace="work", description="Company project docs", color="#3B82F6")
```

### Rename and delete

```
mem_ns_rename(old="project-v1", new="project-v2")   # SQL update, no re-indexing
mem_ns_delete(namespace="archived")                  # deletes all chunks
```

### Bulk assign — `ns_assign`

Move existing chunks to a namespace without re-indexing:

```
mem_do(action="ns_assign", params={"namespace": "infra", "source_filter": "k8s"})
mem_do(action="ns_assign", params={"namespace": "archive", "old_namespace": "default"})
```

| Parameter | Description |
|-----------|-------------|
| `namespace` | Target namespace to assign to |
| `source_filter` | Only chunks from paths containing this substring |
| `old_namespace` | Only chunks currently in this namespace |

### Auto-namespace

Derive namespace from subfolder names automatically:

```
mem_config(key="namespace.enable_auto_ns", value="true", persist=True)
```

```
memory_dirs = ["~/notes"]

~/notes/work/api.md       → namespace "work"
~/notes/personal/diary.md → namespace "personal"
~/notes/readme.md          → namespace "default"  (root of memory_dir)
```

Files at the root of a `memory_dir` get the default namespace. Only files inside subfolders get auto-derived namespaces. This prevents the memory_dir folder name itself from becoming an unintended namespace.

For systematic tagging across opt-in provider directories (e.g. the per-project `~/.claude/projects/<project>/memory/` paths added via `mm init`, or cloud-sync roots), declare path → namespace rules in `namespace.rules` instead — see [Namespace rules in the configuration guide](../configuration.md#namespace-rules-path-based-auto-tagging). Rules are evaluated before `enable_auto_ns` and cover use cases where the immediate parent folder name is opaque (UUIDs, generic labels like `memory`).

### Namespace and `agent_id` validation

Caller-supplied `namespace=` and `agent_id=` values are validated
strictly across every public surface (MCP tools, CLI, LangGraph adapter,
context-gateway endpoints) since v0.1.32. Names are split on `:` into
segments; each segment must match `[A-Za-z0-9._-]+` and is rejected if
it contains a slash, backslash, whitespace, comma, or control character,
starts with `-`, or equals `.` / `..`. Empty segments (leading,
trailing, or consecutive `:`) are also rejected.

The `agent-runtime:` prefix is special — it requires exactly one
trailing segment that is itself a valid `agent_id`, so
`agent-runtime:my-agent` is accepted but `agent-runtime:foo:bar`,
`agent-runtime:` (empty), and `agent-runtime:../x` (path traversal) are
not. Any rejection raises `InvalidNameError` with a message starting
`invalid namespace ...` or `invalid agent-id ...`, so log scrapers see
one shape across surfaces.

Accepted namespaces in normal use:

- `default` — the catch-all when nothing is set
- `<word>` — flat names like `work`, `personal`, `project-x`
- `<prefix>:<segment>` — `archive:2026-q1`, `claude-memory:project-foo`,
  `agent-runtime:planner`, `shared:lessons`
- `custom:<...>` — your own scheme, subject to the segment rules above

The `agent-runtime:<id>` shape is what `mem_session_start(agent_id=...)`
auto-derives, so scope-bound writes from session-aware tools land
there without a manual `namespace=` argument (see "Multi-agent
workflow" below).

---

## 5. Maintenance — `mem_dedup_*`, `mem_decay_*`, `mem_auto_tag`

### Deduplication

```
mem_dedup_scan(threshold=0.92, limit=50)
→ 3 candidate pairs:
  1. [0.97] chunk-A ↔ chunk-B (exact match)
  2. [0.94] chunk-C ↔ chunk-D

mem_dedup_merge(keep_id="chunk-A-uuid", delete_ids=["chunk-B-uuid"])
→ Deleted 1, kept chunk-A (tags merged)
```

> **Performance**: `max_scan` controls how many chunks are compared pairwise. For large indexes (500+), start with `max_scan=100` to avoid timeouts (30s limit). Increase gradually if needed.

### Auto-tagging

```
mem_auto_tag(max_tags=5, dry_run=True)              # preview
mem_auto_tag(max_tags=5)                            # apply
mem_auto_tag(source_filter="notes", overwrite=True) # re-tag specific files
```

> **LLM enhancement**: When LLM is enabled (`MEMTOMEM_LLM__ENABLED=true`), `mem_auto_tag` uses semantic analysis for richer tags. Falls back to keyword frequency heuristics when LLM is disabled or fails. See [LLM Providers](../llm-providers.md).

### Entity extraction

Scan indexed chunks and extract structured entities (people, dates, decisions, technologies):

```
mem_entity_scan(dry_run=True)                       # preview
mem_entity_scan()                                   # extract & store
mem_entity_scan(entity_types=["person", "decision"])# specific types only
mem_entity_search(entity_type="person")             # query extracted entities
```

> **LLM enhancement**: When LLM is enabled, `mem_entity_scan` uses LLM-based structured extraction for higher accuracy (especially person names and decisions). Falls back to regex/pattern matching when LLM is disabled or fails.

### Decay and expiration

Score decay reduces relevance of older memories:

```
mem_config(key="decay.enabled", value="true", persist=True)
mem_config(key="decay.half_life_days", value="30", persist=True)
```

TTL-based cleanup:

```
mem_decay_scan(max_age_days=90)                     # preview
mem_decay_expire(max_age_days=90)                   # delete (dry_run=True by default)
```

### Orphan cleanup

Remove chunks whose source files have been deleted:

```
mem_do(action="cleanup_orphans")                    # preview (dry_run=True)
mem_do(action="cleanup_orphans", params={"dry_run": false})  # delete
```

### Memory hygiene — `mm memory doctor`

A `memory_dir` can be *registered* yet barely indexed: the filesystem watcher
only reacts to live events, so files that landed while the server was down stay
invisible to `mem_search` until a forced re-walk. The index/TOC file
(`MEMORY.md`) can also drift from what's on disk, and chunks can linger after a
source file is deleted. `mm memory doctor` surfaces this **3-way drift** between
what's on disk, what the index file points at, and what's actually in the
searchable DB.

It is **read-only by default** — without `--fix` it never writes to disk, the
DB, or `config.json`. The DB is opened in SQLite `mode=ro`; a missing or too-old
DB degrades to disk/index checks instead of being created. The one opt-in write
path is `--fix --apply` (see [Fixing broken links](#fixing-broken-links)), which
touches only the index file and never the DB or config.

```
mm memory doctor                 # inspect every configured memory_dir
mm memory doctor <dir>           # scope to one configured memory_dir
mm memory doctor --json          # structured output for scripting / CI
mm memory doctor --fix           # preview removable broken links (dry-run)
mm memory doctor --fix --apply   # remove them from the index file
```

#### Example output

```
■ /Users/you/.claude/projects/-Users-you-Work-myproj/memory
  claude-memory · index=MEMORY.md · indexed 12/15
  ! 3/15 indexable file(s) have no DB chunks — `mem_search` can't find them (run `mm index <dir> --force`)
      - project_roadmap.md
      - feedback_review_style.md
      - user_role.md
  ✗ 1 DB source file(s) no longer exist on disk — chunks linger after the file was deleted
      - /Users/you/.claude/projects/-Users-you-Work-myproj/memory/old_notes.md
  ! MEMORY.md over budget: 25600 bytes (cap 24400); 212 lines (cap 200); 2 line(s) over 200 chars (L8, L40)
      - L8
      - L40

■ /Users/you/notes
  user · indexed 40/40
  ✓ no issues

Summary: 1 error, 2 warn, 0 info.
```

Each dir header line reads `{category} · [index={index_file} ·] indexed {db_covered}/{disk_indexable}`. Glyphs mark severity: `✗` error, `!` warn, `·` info. Up to 8 sample items are listed per finding (`--json` carries them all).

#### Checks

| Check | Severity | What it means | Remediation |
|-------|----------|---------------|-------------|
| `db_coverage` | warn | On disk and indexable, but zero chunks in the DB — `mem_search` can't find it. | `mm index <dir> --force` |
| `stale_source` | **error** | A DB chunk's source file no longer exists on disk (deleted; chunks linger). | `mem_do(action="cleanup_orphans", params={"dry_run": false})` (see [Orphan cleanup](#orphan-cleanup)) |
| `convention_violation` | **error** | An index/meta file (`MEMORY.md` / `README.md` for a `claude-memory` dir) was indexed as searchable content. | `mm purge --matching-excluded --apply` |
| `broken_link` | **error** | A markdown pointer in the index file (`[title](target.md)`) resolves to a missing target or escapes the memory root. Wikilinks (`[[other-memo]]`) are never pointers — including the `[[memo]](note)` shape, where the parenthetical is prose rather than a destination — and are checked separately as `dangling_wikilink`. | `mm memory doctor --fix --apply` removes the `missing_target` subset (see [Fixing broken links](#fixing-broken-links)); fix or remove `outside_root` links by hand. |
| `db_extra` | warn | A DB source exists on disk but falls outside the current indexable set (unsupported extension or excluded path). | Usually expected. If the path is now excluded, `mm purge --matching-excluded --apply` reclaims it; unsupported-extension residue has no targeted fix yet. |
| `index_orphan` | warn | An indexable file on disk is not listed in the index file (`MEMORY.md`). | Add a pointer line, or leave it — the TOC is curated. |
| `ambiguous_index_line` | warn | The line names something the doctor won't resolve on a guess: a link target that isn't a plain relative path — one carrying a query (`live.md?view=1`), a percent-escape (`live%2Emd`), a scheme (`urn:live.md`) or a space (`<x y.md>`) — or link syntax left unclosed (`- [B](b.md`). Such links are neither link-checked nor counted as listed, so the file one may have meant can also surface as an `index_orphan`. Ordinary filenames needing markdown escaping (`notes_(v2).md`, `notes_\(v2.md`, `notes_&amp;v2.md`) are *not* flagged — they are read as Markdown and resolve normally. | Rewrite the target as a plain relative filename. Reported as warn, not error. `--fix` will not touch such a line: an unreadable link is never *provably* dead, so a line holding only these is not a `--fix` candidate at all, and one sitting beside a genuinely dead link makes that line a [skip](#fixing-broken-links) — the rest of the file is still fixed. |
| `budget` | warn | The index file exceeds its hot-cache budget: 24,400 bytes / 200 lines / 200 chars per line. | Trim the index file. |
| `index_missing` | warn | The provider's index file (e.g. `MEMORY.md`) is absent or unreadable. | Create it, or ignore if the dir has no TOC convention. |
| `dangling_wikilink` | info | A `[[name]]` on an index line naming no `name.md` inside the memory root, or one outside it (`[[name\|alias]]` resolves on `name`). Each item names its class, as `broken_link` does: `missing_target` — a *forward reference* (a memory worth writing later, which the [memory conventions](#dangling-wikilinks) allow) or a stale link to a deleted memo, which the doctor can't tell apart; `outside_root` — a name that escapes the root, and may well exist there. Never fails a run, and `--fix` never acts on it. | `missing_target`: write the memo, fix the name, or leave it as a deliberate forward reference. `outside_root`: correct the name by hand — nothing follows a wikilink, so an escape is a naming slip rather than the hazard it is for a pointer. |
| `cold_candidate` | info | An indexed file never accessed since indexing (`access_count` 0, `last_accessed_at` unset). | Advisory — a future decay/curation candidate. |
| `db_unavailable` | info | No readable memtomem DB at the configured path — only disk/index checks ran. | `mm index` to build the DB. |
| `unowned_chunks` | info | DB sources under no configured `memory_dir` (a dir was removed, or content was added elsewhere). | Re-add the dir to keep it indexed; for a project root that was deleted from disk, `mm gc orphan-projects`. |

`db_unavailable` and `unowned_chunks` are store-wide, so they print as top-level lines (`(database)` / `(unowned)`) rather than under a dir header.

#### Exit codes and JSON

The exit code is `0` when clean or when only advisory (warn/info) findings exist, and `1` when any **error**-severity finding is present (`stale_source`, `convention_violation`, `broken_link`) — so a coverage gap or budget overflow won't fail CI, but a deleted-source leak or broken TOC link will.

`--json` emits a stable payload: a top-level `{status, dirs, summary}`, where `status` is `"issues"` when any error- or warn-severity finding exists and `"ok"` otherwise (info-only stays `"ok"`).

```json
{
  "status": "issues",
  "dirs": [
    {
      "path": "/Users/you/.claude/projects/-Users-you-Work-myproj/memory",
      "category": "claude-memory",
      "index_file": "MEMORY.md",
      "exists": true,
      "disk_indexable": 15,
      "db_covered": 12,
      "findings": [
        {
          "check": "db_coverage",
          "severity": "warn",
          "count": 3,
          "summary": "3/15 indexable file(s) have no DB chunks — `mem_search` can't find them (run `mm index <dir> --force`)",
          "items": ["project_roadmap.md", "feedback_review_style.md", "user_role.md"]
        },
        {
          "check": "stale_source",
          "severity": "error",
          "count": 1,
          "summary": "1 DB source file(s) no longer exist on disk — chunks linger after the file was deleted",
          "items": ["/Users/you/.claude/projects/-Users-you-Work-myproj/memory/old_notes.md"]
        },
        {
          "check": "budget",
          "severity": "warn",
          "count": 2,
          "summary": "MEMORY.md over budget: 25600 bytes (cap 24400); 212 lines (cap 200); 2 line(s) over 200 chars (L8, L40)",
          "items": ["L8", "L40"]
        }
      ]
    }
  ],
  "summary": { "error": 1, "warn": 2, "info": 0 }
}
```

> **Read-only by default.** Without `--fix`, `mm memory doctor` reports and never writes. Apply the per-check remediation above — most commonly `mm index <dir> --force` to close a coverage gap.

#### Fixing broken links

`--fix` is the one opt-in write path (contract: [ADR-0020](../../adr/0020-memory-index-write-contract.md)). It is **subtractive only**: it removes index-file pointer lines whose target is a `missing_target` — a `- [title](target)` link that resolves *inside* the memory root but points at a file that no longer exists on disk — and nothing else. Removing a provably-dead pointer is the one curation move that can't conflict with the agent that owns the index file, so it is the only thing `--fix` does. It never adds, reorders, reformats, or trims for budget, and never edits the DB.

```
mm memory doctor --fix           # dry-run: print the lines that would be removed
mm memory doctor --fix --apply   # rewrite the index file (atomic, mode-preserving)
```

Scope and guarantees:

- **Every link on the line must be dead, or the line is skipped.** `--fix` removes a dead pointer by deleting its **whole line**, so it only deletes a line whose *every* link is a `missing_target`, and which the parse accounts for entirely: a single-line `-`/`*` bullet of links plus inert prose — the shape [Claude Code's own `MEMORY.md` contract](https://docs.claude.com/en/docs/claude-code/memory) specifies (`- [Title](file.md) — hook`, one per line). A dead link on a line that fails either test — it carries a live, out-of-root, or unreadable link beside the dead one; it is an ordered/`+` item; the item continues onto another line; or link syntax on it resolved to no link — is **skipped, named in the report with the reason, and left for you to repair by hand**, while the eligible lines in the same file are still fixed. Deleting such a line would take the pointers beside the dead one with it, and carving one entry out of a line means parsing free-form prose (which ` · ` separator belongs to whom) — curation this tool leaves to the agent. Skipping is per line: one non-conforming line does not block fixing the rest of the file.
- **`missing_target` only.** `outside_root` links (those escaping the memory root) are left alone — the intent is ambiguous (a typo'd path vs. a deliberate out-of-tree reference), so removing them is your call. `budget` (which entries to cut is prose judgement), `index_orphan` (adding a pointer needs a generated title/hook), and the DB-side `stale_source` / `convention_violation` are also out of scope — use their own remediation.
- **Byte-exact otherwise.** Every surviving line keeps its exact content and end-of-line terminator (LF/CRLF) and the file's trailing-newline state is untouched; a `--fix --apply` on a file with no `missing_target` links is a byte-for-byte no-op.
- **A skip is never success.** `--fix` exits `1` whenever it left a dead pointer behind (in dry-run too) or could not read an index file, and `0` when there was nothing to do or every candidate was handled. `--fix --json` keeps the outcomes apart in `status`: `clean` (no dead pointers), `would-fix` / `fixed` (all candidates handled), `would-partial` / `partial` (some candidate skipped), and `error` (an index file could not be read — decode or I/O failure, in either the analysis read or the locked `--apply` re-read). `error` outranks the other statuses whenever any file hit it: a skip is a complete account of what remains, an unread file is no account at all. Each file object carries `removed` and `skipped` (`{line, text, reason}`) lists plus an `error` message (`null` when the file was read), and `summary` counts `{files, lines, skipped, errors}` — so "no dead pointers" never reads the same as "dead pointers this tool won't touch," or as "a file this run never opened."
- **Concurrency-aware, not race-free.** `--apply` re-reads the file fresh under a sidecar lock and re-validates each candidate (still present, still eligible, and still dead) before an atomic replace, so a pointer the agent edited or whose target reappeared is spared. Removals are **count-bounded to the physical line occurrences present and dead at analysis time** (a line's links are collapsed by line number before counting, so an all-dead line carrying two links is one occurrence): distinct pointers the agent added since are never removed, and a raw line whose number of copies changed since analysis is skipped entirely — the count says how many copies should survive but nothing about *which*, and byte-identical lines can sit in different sections, so `--fix` will not guess which copy you meant to keep. Because the agent (the memory hook) doesn't take memtomem's lock, a residual sub-rename window remains; it is accepted as bounded (the agent writes `MEMORY.md` at session boundaries, the edit is a small subtraction) — **`--fix` does not claim "never clobbers."** Every removed line is printed (in both dry-run and `--apply`) so any churn is auditable from your editor / VCS / agent history.
- **The `--apply` report describes the file it leaves behind.** It names every dead pointer still in the index after the write — including one the agent added or rewrote in that window, which `--fix` won't remove on this run (no analysis saw it) but won't hide either; re-run `--fix` to clear it. Conversely, a candidate the agent deleted meanwhile is not reported: it isn't in the file, so there is nothing to repair and no line to name.

#### Dangling wikilinks

A wikilink (`[[other-memo]]`, or `[[other-memo|alias]]`) is a cross-reference between memories, not a TOC pointer — and the agent memory convention **allows forward references by design**: a `[[name]]` that doesn't match an existing memory yet is not an error, it marks something worth writing later. A dangling wikilink therefore has two indistinguishable readings — a forward reference (intended) or a stale link left behind by a deleted memo — and the doctor reports it accordingly: `dangling_wikilink` is **info-severity**, never fails a run, and is never a `--fix` candidate.

**Resolution.** `[[name]]` means `name.md` in the memory root; an `|alias` names the display text, never the file, and an Obsidian `#section` suffix is dropped. This is the doctor's own rule — close to the [Obsidian import convention](data-config-cli.md#importing-from-obsidian), but deliberately more lenient where the two part: an author who writes the suffix means the file, so `[[name.md]]` resolves to `name.md`, where the importer would produce `name.md.md`. A name that escapes the memory root reports as `outside_root` rather than being silently dropped.

**Scope.** Only index-file lines are scanned. Wikilinks inside memo bodies are out of the doctor's scope, which reads the index file only.

**A wikilink does not shield a line from `--fix`.** A wikilink is not a pointer, so a line whose markdown links are all dead stays [eligible for removal](#fixing-broken-links) even though deleting it takes the wikilink with it — otherwise any dead line could be made permanent by adding a cross-reference. Every removed line is printed in both dry-run and `--apply`, so a forward reference that goes with one is auditable rather than lost silently.

---
