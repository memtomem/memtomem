# Core memory tools

The day-to-day memory operations: building the index, searching it, and creating or editing notes.

[← memtomem Reference](../reference.md)

**On this page**

- [1. Indexing — mem_index](#1-indexing--mem_index)
- [2. Search — mem_search, mem_recall](#2-search--mem_search-mem_recall)
- [3. Memory CRUD — mem_add, mem_batch_add, mem_edit, mem_delete](#3-memory-crud--mem_add-mem_batch_add-mem_edit-mem_delete)

---

## 1. Indexing — `mem_index`

### Index a directory

```
mem_index(path="~/notes")
→ Indexing complete:
  - Files scanned: 47
  - Total chunks: 312
  - Indexed: 312
  - Skipped (unchanged): 0
  - Deleted (stale): 0
  - Blocked (redaction): 0
  - Duration: 2340ms
```

Supported files and their chunking strategies:

| File Type | Strategy |
|-----------|----------|
| `.md` | Heading-aware split (`#`, `##`, `###`) |
| `.json` / `.yaml` / `.toml` | Top-level key split |
| `.py` | Functions and classes (tree-sitter) |
| `.js` / `.ts` / `.tsx` | Functions and classes (tree-sitter) |

### Incremental re-indexing

memtomem tracks what changed via a SHA-256 hash per chunk. A second
call on the same path only re-embeds chunks whose hash is new:

```
mem_index(path="~/notes")
→ Indexing complete:
  - Files scanned: 47
  - Total chunks: 315
  - Indexed: 5
  - Skipped (unchanged): 308
  - Deleted (stale): 2
  - Blocked (redaction): 0
  - Duration: 180ms
```

How to read the stats:

- **Indexed** — chunks whose content hash is new (brand-new sections
  *or* edited sections whose hash changed). Only these hit the embedder.
- **Skipped (unchanged)** — hash matched an existing chunk, no
  embedding call made.
- **Deleted (stale)** — chunks that used to exist in a file but are no
  longer produced. An edited section contributes to **both**
  `Indexed` (new hash) and `Deleted (stale)` (old hash), because the
  diff is hash-based, not UUID-based.

### Force re-index

After switching embedding models or when recovering suspected vector/index
corruption, pass `force=True` — every chunk is re-embedded regardless of hash
match, so they all show up under `Indexed`. A first index or ordinary content
edit does not need force; the incremental path embeds new hashes and refreshes
shifted line metadata without rewriting unchanged chunks:

```
mem_index(path="~/notes", force=True)
```

**Chunk identity is preserved when content is unchanged.** As of v0.1.33
([ADR-0005](../../adr/0005-force-reindex-metadata-contract.md)), force-reindex
keeps the existing `id` (UUID), `access_count`, `last_accessed_at`,
`importance_score`, and `chunk_links` rows for any chunk whose content
hash still matches what the file produces. Only embeddings are
recomputed. This means agents that cache chunk IDs, scheduled
re-embedding jobs, and personalization signals all survive a force
rebuild — previously every force pass regenerated UUIDs and silently
zeroed access stats.

### Secret-redaction gate

Every indexing entrypoint scans file content for secret-shaped patterns
(API keys, tokens, private-key headers — the same set `mem_add` / `mem_edit`
enforce) before storing it. A hit skips that file — it is **not** indexed —
and is reported via the `Blocked (redaction)` line above, plus a listing of
the blocked paths when the count is nonzero. Other files in the same run are
unaffected.

`mm index --force-unsafe` bypasses the gate for a direct CLI index run
(audit-logged), and the Web UI offers the same bypass as an explicit
opt-in: a blocked run shows a toast pointing at the "Index without privacy
gate" checkbox, and re-running with it enabled indexes the file. Both are
explicit request controls on human-facing surfaces, and the checkbox is
one-shot: it clears itself when the run starts, so each bypass requires a
fresh opt-in. The `mem_index` MCP tool has no `force_unsafe` parameter, so
an agent calling `mem_index` cannot request this bypass. Both bypasses are
hard-refused for files that resolve to the git-tracked `project_shared`
scope regardless of caller.

### Documenting the patterns in a note

The gate's two broad label rules match on the keyword plus `=` or `:`,
independent of what follows — so a note that *documents* them (writing
`api_key=` in prose) trips its own guard. Because a blocked file is skipped
rather than failed, its old chunks stay in the DB and `mem_search` keeps
answering from pre-trip content indefinitely.

A Markdown note can declare an exemption for itself
([#2076](https://github.com/memtomem/memtomem/issues/2076)):

```markdown
---
redaction: documents-patterns
---

The guard matches `api_key=` on the keyword alone.
```

Unlike `--force-unsafe`, this reaches every indexing path — including the
ones that have no bypass flag at all (`mem_index`, the file watcher, the
debounce drain) — because it travels with the content the gate already
reads. In exchange it is deliberately narrow:

- **Markdown only, exact literal, fails closed.** One *unindented* top-level
  `redaction: documents-patterns` key in the leading frontmatter block, written
  literally. Indented, quoted, tagged, aliased, commented, nested under another
  field, duplicated, or any other value means no exemption. The block itself
  must be one memtomem already recognises — same rule as the frontmatter your
  `tags:` and `valid_from:` keys live in, so a file whose frontmatter memtomem
  does not read (CRLF line endings, a leading byte-order mark, a `---` opener
  with trailing spaces) cannot declare either.
- **Label hits only.** It waives only the two unquoted `api_key`/`password`
  label rules. A provider token, private-key header, AWS key, or a quoted-JSON
  credential (`"password": "…"`) re-blocks the file even with the declaration
  — so a secret pasted into an exempt note later is still refused.
- **Never for `project_shared`.** Hard-refused, exactly like `--force-unsafe`.
- **Not silent.** Every honoured *and* refused declaration writes an audit
  line naming which it was. An honoured one increments the `exempted` counter
  (`mem_add_redaction_stats`, Settings → Redaction) and is named per run by
  `mm index`, the shell, and `mem_index`; a refused one counts as `blocked`
  (or `blocked_project_shared`) and appears in the blocked list, like any
  other refusal.

Note the asymmetry with `--force-unsafe`: a declaration is persistent, so it
keeps applying on unattended re-indexes until someone removes it from the file.
That is the point — it is how the note stays searchable — but it means the
declaration should describe why the file needs it, and only files that really
document the patterns should carry one.

See [ADR-0006](../../adr/0006-web-ui-folder-upload-redaction.md) (Axis E,
Axis E.5) for the full trust-boundary design.

### Namespace-scoped indexing

```
mem_index(path="~/work/docs", namespace="work")
mem_index(path="~/personal/notes", namespace="personal")
```

### Auto-watch vs manual seed

`MEMTOMEM_INDEXING__MEMORY_DIRS` feeds a file watcher that runs inside
the `memtomem-server` (MCP) process. The watcher is **reactive only** — it
reindexes files when the filesystem emits modify / create / move events.
Two cases it does NOT cover:

- **Pre-existing files on disk** when you first configure a `memory_dir`.
  Run `mm index <dir>` (or `mem_index(path="<dir>")`) once to seed them;
  after that, the watcher picks up further edits.
- **Files outside `memory_dirs`.** Call `mem_index` / `mm index` manually
  with the path you want indexed ad-hoc.

Both are idempotent — chunks are content-hashed, so unchanged files are
skipped on re-runs. This is why the `mm init` wizard's `Next steps` lists
`mm index {memory_dir}` as step 1.

### Hook integration — debounce queue

For editor / hook callers (PostToolUse[Write] in Claude Code, etc.) that
fire on every save, `mm index` ships three mutually-exclusive flags that
share a small on-disk queue at `~/.memtomem/index_debounce_queue.json`:

```bash
mm index --debounce-window 5 PATH   # record PATH; drain entries silent ≥5s
mm index --flush                    # synchronously drain everything queued
mm index --status                   # snapshot queue depth + oldest entry
```

- `--debounce-window <SECONDS>` records the path and re-indexes only
  entries that have been silent for at least `SECONDS`. Rapid consecutive
  writes restart the window so a burst is indexed once at the end.
- `--flush` blocks until every queued file has been indexed (or recorded
  as an error). Use this when correctness matters — e.g. a `Stop` hook
  draining before session end. Worst-case latency ≈ queue depth ×
  per-file index cost.
- `--status` is informational only. Concurrent hooks may modify the
  queue between this read and any later action; for correctness use
  `--flush`, not status-then-flush.

All three accept `--json` for one-line scripted output.

`--debounce-window` and `--flush` enforce the same redaction gate as direct
indexing, and there is no flag to opt out (`--force-unsafe` errors if
combined with any of the three debounce flags, since the queue only carries
`path` / `namespace` / `force`). A Markdown file's own
`redaction: documents-patterns` declaration *is* honoured here — it lives in
the content, not in the queue entry — and is the only way such a file drains
cleanly. A blocked file is not silently marked
indexed: it surfaces as an `Errors` entry in the drain result and **stays
queued**, retried on every subsequent drain. The gate re-runs on each retry
(it fires before the content-hash skip), so the entry keeps erroring until
the file no longer trips it — **remove or rotate the secret** and the next
`--flush` drains it cleanly and clears the entry. A direct
`mm index --force-unsafe <path>` indexes the content but does **not** dequeue
the entry (the drain path never threads `--force-unsafe`), so it keeps
reporting on flush until the file stops tripping the gate or you clear the
queued entry yourself (it's a plain path key in
`~/.memtomem/index_debounce_queue.json`).

---

## 2. Search — `mem_search`, `mem_recall`

### `mem_search` — Hybrid search

```
mem_search(query="deployment checklist")
```

Combines keyword matching (exact words) with meaning-based search (similar concepts), then merges the results for the best of both worlds.

**Parameters**:

| Parameter | Description | Example |
|-----------|-------------|---------|
| `query` | Natural language search query | `"authentication flow"` |
| `top_k` | Number of results (default 10, max 100) | `20` |
| `source_filter` | File path substring (recommended) or glob | `"docs/adr"`, `".yaml"` |
| `tag_filter` | Comma-separated tags, OR logic | `"redis,cache"` |
| `namespace` | Scope to namespace | `"work"` |
| `as_of` | Temporal validity query — only return chunks valid on this date (default = current time). Date-only `YYYY-MM-DD` or quarter `YYYY-QN`. Chunks without `valid_from`/`valid_to` frontmatter are always-valid and unaffected. | `"2024-Q3"` |
| `bm25_weight` / `dense_weight` | Override RRF weights (default `1.0`) | `2.0` |
| `context_window` | Expand each result with ±N adjacent chunks (`0` = disabled) | `1` |
| `output_format` | `"compact"` (default), `"verbose"`, or `"structured"` (JSON with `hints` field) | `"structured"` |
| `scope` | Memory tier filter: one value, comma list, or glob; omitted uses user plus current-project tiers | `"user,project_local"`, `"project_*"` |
| `rerank` | Per-call rerank control: `false` skips the cross-encoder rerank stage (fast path for latency-bounded callers); omitted/`true` follows server config — `true` cannot enable reranking the server has disabled | `false` |
| `record` | Per-call replay control (default `true`): `false` makes the search a background read for fan-out callers — no access-count increments, no query history, caches neither read nor written, and dense retrieval runs exhaustive, so results can differ | `false` |

```
mem_search(query="caching strategy", tag_filter="redis,cache", namespace="work")
mem_search(query="auth", source_filter="docs/adr", top_k=5)
mem_search(query="deploy pipeline", as_of="2025-Q3")    # historical query
```

> **Result count with filters**: `mem_search` returns *up to* `top_k` results.
> Increase `top_k` when one call needs more results. When reranking is enabled,
> the candidate pool is automatically computed from `rerank.oversample`,
> `rerank.min_pool`, and `rerank.max_pool`; passing `rerank=false` skips
> reranking for the call and collapses that pool to `top_k`. Post-rank filters
> can still reduce the final count.

> **source_filter tip**: Use substrings like `"docs/adr"` or `".py"` for filtering. Glob patterns (`*`, `?`) are matched against the **full absolute path** via `fnmatch`, so `"*.py"` won't work as expected — use `".py"` instead.

> **Trust-UX hints**: when you don't pin a namespace, results are followed by a parenthesized hint if chunks were hidden in system namespaces (e.g. `archive:*`) or if the configured embedding dimension disagrees with what's in the database. A third hint — independent of namespace selection — appears when you pass `rerank=true` but the server has reranking disabled (`rerank.enabled=false`), since the parameter cannot force-enable it. In `output_format="structured"` those hints are emitted as a `hints` array instead.

> **Score scale**: `score` values are only comparable within one scale, and the
> scale follows server config. Structured output names the base scale in a
> top-level `score_scale` key: `"rerank"` (cross-encoder output — range depends
> on the model, reported alongside in a `reranker` key), `"rrf"`
> (reciprocal-rank fusion), `"bm25"` / `"dense"` (unfused single-retriever
> scores when only one retriever is enabled), or `"none"` (filter-only
> enumeration — no relevance scale; the filter is the selector). Optional
> modifier stages (time decay, access/importance/entity boosts; all off by default)
> multiply on top of the base scale when enabled. Pick score thresholds per
> scale — or skip score gating for a scale you don't recognize — instead of
> inferring the scale from the value range. Both keys are omitted when there
> are no results. `mm search --format json` carries the same values as
> per-item `score_scale` / `reranker` keys.

> **Quality Lab run ID**: every ranked search persisted by the local SQLite
> backend receives a `query_run_id`. MCP structured output and the Web search
> API expose it as soon as the search answers — the observation row is written
> in the background, off the response path — so it can be used to attach later
> feedback without guessing which invocation produced a result set. The ID is
> provisional in the narrow sense that a failed observation write leaves it
> unresolvable: history listings never show it and feedback on it is rejected.
> The tools and endpoints that read a run (`mem_search_feedback`,
> `mem_search_history`, the Quality Lab run routes) settle that write first, so
> a run ID is usable in the very next call to the same server process. A run
> answered by a *different* process against the same database (the MCP server
> while you read the Web UI) can be briefly invisible, which surfaces as a 404
> rather than a wrong answer.
> Cache hits receive distinct IDs. Filter-only browsing does not
> create an observation, and an observation write failure never fails search. A
> call passing `record=false` creates none either: it returns no `query_run_id`
> and writes no history row, which is the point of the switch.
> The local snapshot stores ranks, scores, chunk IDs, content hashes, heading
> hierarchy, namespaces, languages, and source **basenames**—not result content
> or absolute paths. Existing query history still records the query text and is
> pruned by the existing 90-day history policy. `mm search --format json` does
> not carry the run ID; use MCP structured output or the Web API when it is
> needed. (The per-result `chunk_id` *is* in the CLI JSON payload — see below.)

> **Capturing a `chunk_id` from the CLI**: every item of `mm search --format
> json` carries `chunk_id`, the same key and canonical UUID string the MCP
> structured payload uses, so the promote-to-shared flow is scriptable without
> reading SQLite:
>
> ```bash
> # jq -e exits non-zero on an empty result set, so a query that hit
> # nothing stops here instead of running `mm agent share null`.
> id=$(mm search "deployment" --format json | jq -er '.[0].chunk_id') \
>   && mm agent share "$id" --target shared
> ```
>
> The other CLI formats (`table`, `plain`, `context`, `smart`) stay
> id-free — `json` is the machine-readable surface.

> **Relevance feedback**: a committed run ID accepts explicit judgments for
> chunks that appear in that run's snapshot, via
> `mem_do(action="search_feedback", params={"run_id": "...", "chunk_id": "...",
> "judgment": "relevant"})` — the closed vocabulary is `relevant` /
> `not_relevant`. Resubmitting the same judgment is a no-op; a different
> judgment is rejected until the call is repeated with `"replace": true`, and
> replacements are audited by a strictly increasing `updated_at` timestamp
> while `created_at` marks the original. Omit `judgment` to list a run's
> current judgments. Unknown run IDs and chunks outside the run's snapshot are
> rejected without partial writes. Feedback rows store only IDs, the judgment,
> and timestamps — never result content or paths — follow the same 90-day
> retention as the observation they belong to, and are never read by search
> itself. `mm web` (dev mode) adds a Settings → Search Runs panel for the same
> loop.

### Tuning search weights

Use `bm25_weight` and `dense_weight` to shift between keyword and semantic matching:

```
mem_search(query="쿠버네티스", bm25_weight=2.0, dense_weight=0.5)   # keyword-heavy
mem_search(query="container alerts", bm25_weight=0.5, dense_weight=2.0) # meaning-heavy
```

### Cross-language search

memtomem supports searching across languages (e.g., querying in English to find Korean content), but quality depends on the embedding model:

#### Embedding model choice

| Model | KR→EN cross-search | EN→KR cross-search | KR semantic accuracy |
|-------|:---:|:---:|:---:|
| `nomic-embed-text` (768d) | Weak (often misses) | Good (#2) | Moderate |
| `bge-m3` (1024d) | **Good (#2)** | **Good (#2)** | **High (#1)** |

**Recommendation**: Use `bge-m3` if you work with Korean or other non-English content. Switch with:
```
mm embedding-reset --mode apply-current   # after updating config
mm index ~/notes --force                  # re-embed all files
```

Or in `~/.memtomem/config.json`:
```json
{"embedding": {"model": "bge-m3", "dimension": 1024}}
```

#### BM25 and language

- **Keyword (BM25) search** is language-bound — Korean keywords only match Korean text, English keywords only match English text. This is expected.
- For **Korean-heavy workloads**, switch the tokenizer to `kiwipiepy` for better BM25 results:
  ```
  mm config set search.tokenizer kiwipiepy
  ```
  This requires `pip install kiwipiepy` and provides morphological analysis for Korean text. The default `unicode61` tokenizer splits Korean text at character boundaries rather than morpheme boundaries.

### `mem_recall` — Date-range retrieval

Find memories by *when* they were created, without a search query:

```
mem_recall(since="2026-03", limit=10)
mem_recall(since="2026-01", until="2026-03")
mem_recall(since="2026-03-15", source_filter="meeting*")
mem_recall(namespace="project:*", limit=5)
```

**Parameters**:

| Parameter | Description | Format |
|-----------|-------------|--------|
| `since` | Inclusive start date | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, ISO datetime |
| `until` | Exclusive end date | same formats |
| `source_filter` | File path substring or glob | `"notes"`, `"*.md"` |
| `tag_filter` | Comma-separated tags, matching ANY | `"handoff,decision"` |
| `namespace` | Single, comma-separated, or glob | `"work"`, `"project:*"` |
| `limit` | Max results (default 20, max 500) | `10` |
| `output_format` | `"compact"` (default) or `"structured"` (JSON with `hints` field) | `"structured"` |
| `scope` | Memory tier filter: one value, comma list, or glob | `"project_shared"` |

`tag_filter` is applied in SQL *before* `limit`, so no tagged row can be crowded out. `mem_search` also selects on the tag before its ranking cap, with one caveat: its keyword leg filters in SQL, but its semantic (dense) leg can only apply the tag within the bounded neighbour set it retrieves, so a tagged chunk that is neither a keyword match nor a near-neighbour of the query may still be missed there. Use `mem_recall` when a tagged record must be reachable regardless of the query, and for newest-first ordering; `mem_search` ranks by relevance.

Like `mem_search`, `mem_recall` hides system namespaces (`archive:*` by default) when no namespace is pinned and appends a trust-UX hint if any chunks were filtered or if an embedding dimension mismatch is detected. `output_format="structured"` exposes those as a `hints` array for programmatic consumers.

---

## 3. Memory CRUD — `mem_add`, `mem_batch_add`, `mem_edit`, `mem_delete`

### `mem_add` — Add a note

```
mem_add(content="Redis LRU→LFU migration reduced cache misses by 40%", tags=["redis", "performance"])
→ Saved to ~/.memtomem/memories/2026-03-30.md (1 chunk indexed)
```

| Parameter | Description |
|-----------|-------------|
| `content` | The note text |
| `title` | Optional heading (becomes `## title` in the file) |
| `tags` | List of tags (`list[str]`) |
| `file` | Target file path (auto-generates date-stamped file if omitted) |
| `namespace` | Namespace assignment |
| `template` | Structured template (`adr`, `meeting`, `debug`, `decision`, `procedure`) |
| `scope` | Write tier: `user`, `project_local`, or `project_shared` |
| `confirm_project_shared` | Required `true` consent for Git-tracked shared writes |
| `force_unsafe` | Bypass a reviewed false-positive privacy match; forbidden for shared-tier writes |
| `idempotency_key` | Optional client key (max 256 chars) preventing duplicate successful writes for 24 hours |

```
mem_add(content="New rate limit: 1000 req/min", file="api-notes.md", tags=["api"])
mem_add(content="Sprint decision: use GraphQL", title="Sprint 12", namespace="work")
```

Tags are persisted as a per-entry `> tags: [...]` blockquote header on the
markdown entry and promoted to chunk metadata at index time so
`mem_search(tag_filter=...)` can match. See
[ADR-0002](../../adr/0002-mem-add-blockquote-tags.md) for the on-disk format
and reader/writer contract.

#### Structured Templates

Use `template` to create formatted entries:

```
mem_add(template="adr", content='{"title":"Use GraphQL","context":"REST API hitting limits","decision":"Migrate to GraphQL","consequences":"Need to retrain team"}')
```

| Template | Fields | Use case |
|----------|--------|----------|
| `adr` | title, status, context, decision, consequences | Architecture decision records |
| `meeting` | title, date, attendees, agenda, decisions, action_items | Meeting notes |
| `debug` | title, symptom, root_cause, fix, prevention | Debugging logs |
| `decision` | title, options, chosen, rationale | Decision records |
| `procedure` | title, trigger, steps, tags | Reusable workflows |

You can also pass plain text as `content` — it will be placed in the template body directly. Fields not provided in the JSON are automatically omitted from the output.

#### How `mem_add` stores entries

- Without `file`: entries are appended to a date-stamped file (`~/.memtomem/memories/YYYY-MM-DD.md`).
- **The date is UTC**, as is the `> created:` timestamp on the entry and the
  time column in `mm recall`. East of UTC this means an early-morning note
  lands in the previous day's file — before 09:00 KST (UTC+9) the UTC date
  has not rolled over yet. Pass `file` explicitly if you need a particular
  local grouping.
- Each entry gets its own `## ` heading and is indexed as a separate chunk.
- Tags are applied only to the new entry, not to existing entries in the same file.
- The file is re-indexed after each add, but unchanged chunks are skipped (incremental indexing).
- A `force_unsafe` write is a one-time bypass, but the file it leaves behind
  is permanent. Later CRUD writes (`mm add`, `mem_add`, `mem_edit`) do not
  rescan it, but every automated re-index path — the `mem_index` MCP tool,
  the file watcher, the debounce queue — scans the content again and blocks
  on it again; re-forcing it takes an explicit opt-in on a human-facing
  surface, either `mm index --force-unsafe` or the Web UI's "Index without
  privacy gate" checkbox. If it was a real secret, rotate it and edit the
  file. If it was a false positive in a Markdown note that *documents* the
  patterns, the standing answer is a `redaction: documents-patterns`
  frontmatter declaration (see "Documenting the patterns in a note" above),
  which the automated paths do honour — for label-shaped hits only.

### `mem_batch_add` — Add multiple notes

```
mem_batch_add(entries=[
  {"key": "python-tip", "value": "Use walrus operator := for assignment expressions"},
  {"key": "docker-tip", "value": "Use multi-stage builds to reduce image size"}
])
```

Entries become `## key` headings in a single markdown file.

### `mem_edit` — Edit a chunk

Use the chunk ID from `mem_search` results:

```
mem_edit(chunk_id="abc123-...", new_content="Updated content")
```

Modifies the source `.md` file and re-indexes it.

> **Note**: After editing, the chunk gets a new UUID (the old one is replaced during re-indexing). If you need to reference it again, search for the updated content.

### `mem_delete` — Delete

```
mem_delete(chunk_id="abc123-...")                # single chunk
mem_delete(source_file="/path/to/notes.md")      # all chunks from a file
mem_delete(namespace="old-project")              # all chunks in a namespace
```

The three forms differ in what they touch on disk, and that difference decides
whether the deletion survives:

- `source_file=` and `namespace=` remove **index rows only** — the `.md` files
  stay exactly as they are. Because the content is still on disk inside an
  indexed directory, the next indexing pass puts the chunks back: a watcher
  event when the file is next written, an explicit `mm mem rescan`, or any
  discovery walk. Use them to clear stale rows, not to make a memory stay gone.
- `chunk_id=` is the outlier: it removes the chunk's line range from the
  markdown file itself and re-indexes. The content is gone from disk, so
  re-indexing has nothing to restore.

> **Note**: to keep a file out of the index for good, exclude it and then
> reclaim the rows it already has — add a matching glob to
> `indexing.exclude_patterns` and run `mm purge --matching-excluded` (dry-run
> by default; `--apply` performs the deletion). See
> [Configuration](../configuration.md) for how the exclude patterns are
> evaluated. Deleting rows without an exclude rule is a point-in-time cleanup,
> not an opt-out.

---
