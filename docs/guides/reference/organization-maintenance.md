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
| `broken_link` | **error** | A markdown pointer in the index file (`[title](target.md)`) resolves to a missing target or escapes the memory root. Wikilinks (`[[other-memo]]`) are **not** link-checked — including the `[[memo]](note)` shape, where the parenthetical is prose rather than a destination. | `mm memory doctor --fix --apply` removes the `missing_target` subset (see [Fixing broken links](#fixing-broken-links)); fix or remove `outside_root` links by hand. |
| `db_extra` | warn | A DB source exists on disk but falls outside the current indexable set (unsupported extension or excluded path). | Usually expected. If the path is now excluded, `mm purge --matching-excluded --apply` reclaims it; unsupported-extension residue has no targeted fix yet. |
| `index_orphan` | warn | An indexable file on disk is not listed in the index file (`MEMORY.md`). | Add a pointer line, or leave it — the TOC is curated. |
| `ambiguous_index_line` | warn | The line names something the doctor won't resolve on a guess: a link target that isn't a plain relative path — one carrying a query (`live.md?view=1`), a percent-escape (`live%2Emd`), a scheme (`urn:live.md`) or a space (`<x y.md>`) — or link syntax left unclosed (`- [B](b.md`). Such links are neither link-checked nor counted as listed, so the file one may have meant can also surface as an `index_orphan`. Ordinary filenames needing markdown escaping (`notes_(v2).md`, `notes_\(v2.md`, `notes_&amp;v2.md`) are *not* flagged — they are read as Markdown and resolve normally. | Rewrite the target as a plain relative filename. Reported as warn, not error. `--fix` will not touch such a line — and because it fails closed rather than skipping, one of them **aborts the whole `--fix` run**, so repair them before fixing dead links elsewhere. |
| `budget` | warn | The index file exceeds its hot-cache budget: 24,400 bytes / 200 lines / 200 chars per line. | Trim the index file. |
| `index_missing` | warn | The provider's index file (e.g. `MEMORY.md`) is absent or unreadable. | Create it, or ignore if the dir has no TOC convention. |
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

- **One entry per line, or it refuses.** `--fix` removes a dead pointer by deleting its **whole line**, which is only sound while the index holds one entry per line — the shape [Claude Code's own `MEMORY.md` contract](https://docs.claude.com/en/docs/claude-code/memory) specifies (`- [Title](file.md) — hook`, one per line). If a line to be removed carries more than one link, `--fix` **fails closed with an error naming that line** and writes nothing, in both dry-run and `--apply`: deleting the line would take the *live* entries beside the dead one with it, and the parser cannot even see them to report the loss. Repair such a line by hand, or split the index to one entry per line and re-run.
- **`missing_target` only.** `outside_root` links (those escaping the memory root) are left alone — the intent is ambiguous (a typo'd path vs. a deliberate out-of-tree reference), so removing them is your call. `budget` (which entries to cut is prose judgement), `index_orphan` (adding a pointer needs a generated title/hook), and the DB-side `stale_source` / `convention_violation` are also out of scope — use their own remediation.
- **Byte-exact otherwise.** Every surviving line keeps its exact content and end-of-line terminator (LF/CRLF) and the file's trailing-newline state is untouched; a `--fix --apply` on a file with no `missing_target` links is a byte-for-byte no-op.
- **Concurrency-aware, not race-free.** `--apply` re-reads the file fresh under a sidecar lock and re-validates each candidate (still present *and* still dead) before an atomic replace, so a pointer the agent edited or whose target reappeared is spared. Removals are **count-bounded to the links present and dead at analysis time**: distinct pointers the agent added since are never removed, and an exact byte-duplicate of a dead link keeps the right number of copies (which identical copy survives is unspecified, since they're equal). Because the agent (the memory hook) doesn't take memtomem's lock, a residual sub-rename window remains; it is accepted as bounded (the agent writes `MEMORY.md` at session boundaries, the edit is a small subtraction) — **`--fix` does not claim "never clobbers."** Every removed line is printed (in both dry-run and `--apply`) so any churn is auditable from your editor / VCS / agent history.

---
