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

After switching embedding models, upgrading memtomem, or for a clean
rebuild, pass `force=True` — every chunk is re-embedded regardless of
hash match, so they all show up under `Indexed`:

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
(audit-logged). This flag is **CLI-only** — the `mem_index` MCP tool has no
`force_unsafe` parameter, so an agent calling `mem_index` cannot bypass the
gate; a false positive needs a human running `mm index --force-unsafe` from
a terminal. The bypass is hard-refused for files that resolve to the
git-tracked `project_shared` scope regardless of caller.

See [ADR-0006](../../adr/0006-web-ui-folder-upload-redaction.md) for the
full trust-boundary design.

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
indexing — there's no way to opt out (`--force-unsafe` errors if combined
with any of the three debounce flags, since the queue only carries
`path` / `namespace` / `force`). A blocked file is not silently marked
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
> modifier stages (time decay, access/importance boosts; all off by default)
> multiply on top of the base scale when enabled. Pick score thresholds per
> scale — or skip score gating for a scale you don't recognize — instead of
> inferring the scale from the value range. Both keys are omitted when there
> are no results. `mm search --format json` carries the same value as a
> per-item `score_scale` key.

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
| `namespace` | Single, comma-separated, or glob | `"work"`, `"project:*"` |
| `limit` | Max results (default 20, max 500) | `10` |
| `output_format` | `"compact"` (default) or `"structured"` (JSON with `hints` field) | `"structured"` |
| `scope` | Memory tier filter: one value, comma list, or glob | `"project_shared"` |

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
- Each entry gets its own `## ` heading and is indexed as a separate chunk.
- Tags are applied only to the new entry, not to existing entries in the same file.
- The file is re-indexed after each add, but unchanged chunks are skipped (incremental indexing).

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

---
