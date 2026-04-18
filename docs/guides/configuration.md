# Configuration Reference

memtomem reads configuration from environment variables. All variables use the `MEMTOMEM_` prefix, with nested sections separated by `__` (double underscore).

```bash
# Example: switch from Ollama to OpenAI
export MEMTOMEM_EMBEDDING__PROVIDER=openai
export MEMTOMEM_EMBEDDING__MODEL=text-embedding-3-small
export MEMTOMEM_EMBEDDING__DIMENSION=1536
export MEMTOMEM_EMBEDDING__API_KEY=sk-...
```

For interactive setup, run `mm init` instead of editing env vars by hand.

## Precedence and merge behaviour

memtomem resolves each field from up to four sources at startup, in order
of increasing priority:

1. **Built-in defaults** — the values in `config.py`.
2. **`~/.memtomem/config.d/*.json`** — drop-in fragments, applied in
   lexicographic filename order. Intended for integration installers
   (`mm init <client>` drops one fragment; removing the file reverses
   the change). For `list[*]` fields, each fragment respects a per-field
   merge strategy (see below).
3. **`~/.memtomem/config.json`** — the user-managed override layer that
   `mm init` writes to. Every key here replaces whatever earlier layers
   produced for that field (REPLACE semantics across the board).
4. **`MEMTOMEM_*` environment variables** — highest priority. If an
   env var is set, the corresponding entries in `config.d/` and
   `config.json` are skipped.

### List field merge strategies

`list[*]` fields declare either `APPEND` or `REPLACE` in the type
annotation, and that strategy governs how `config.d/` fragments layer on
top of the default:

| Field | Strategy | Notes |
|-------|----------|-------|
| `indexing.memory_dirs` | APPEND | Each fragment contributes more roots, dedup by path string |
| `indexing.exclude_patterns` | APPEND | Multiple denylists merge cleanly |
| `search.system_namespace_prefixes` | APPEND | Integrations can add further hidden namespaces on top of `archive:` |
| `webhook.events` | APPEND | Fragments can subscribe to additional event types |
| `search.rrf_weights` | REPLACE | Positional tuning knob — appending would misalign `[BM25, Dense]` slots |
| `importance.weights` | REPLACE | Same positional constraint |

`config.json` always replaces, regardless of strategy — it's the
explicit-user-override layer. Use a fragment in `config.d/` if you want
APPEND semantics.

### Delta-only save semantics

`config.json` stores only values that differ from the merged lower
layers (defaults + env vars + `config.d/` fragments). When you save
through any path — `mm config set`, `PATCH /api/config`, the Web UI's
section "Save" buttons, or `memory-dirs/add|remove` — memtomem
computes the difference against a freshly built comparand and writes
only the delta. Three kinds of silent leftovers this prevents:

- **Default leftovers.** Toggling "MMR enabled" on and back off in
  the Web UI no longer pins `mmr.enabled=false` into `config.json`
  (where it would shadow a `config.d/` fragment that set it True).
- **Environment leftovers.** Running once with `MEMTOMEM_MMR__ENABLED=true`
  and saving does not bake the env value into `config.json`; the
  moment the env var is unset, the field reverts correctly.
- **Fragment leftovers.** Saving an unrelated field does not copy
  `config.d/` fragment values into `config.json`. Fragment edits stay
  the source of truth and take effect on the next load.

On-disk leftovers from older versions are cleaned up automatically on
the next save, provided the stale value now matches the comparand.

### Moving `config.json` between machines

`indexing.memory_dirs` participates in delta-only save, so on the
machine where it was set the file typically omits it. When copying an
existing `config.json` to a new machine, any `indexing.memory_dirs`
entry carries over as-is and is **not** auto-replaced by the new
machine's auto-discovered paths. Reset it explicitly when migrating:

```bash
# Option 1: targeted removal of the carried-over entry
mm config unset indexing.memory_dirs

# Option 2: re-run the wizard with --fresh
mm init --fresh

# Option 3: remove the indexing section by hand
#          (edit ~/.memtomem/config.json)
```

### Removing individual overrides (`mm config unset`)

`mm config unset <key>` drops a single pinned entry from
`~/.memtomem/config.json`. Each key is `section.field` form and the
command is idempotent — running it on a key that isn't pinned exits 0
with an `(already at default)` note so scripts can re-run safely.
Unknown keys exit 1 with a typo suggestion when one is nearby. When
every override is removed the config file itself is deleted.

```bash
mm config unset mmr.enabled                    # drop one key
mm config unset mmr.enabled search.default_top_k  # best-effort multi-key
```

Because `config.json` is delta-only (see above), the underlying
`config.d/` fragment or built-in default immediately takes effect on
the next load. For a wholesale reset of wizard-untouched keys, prefer
`mm init --fresh`.

### Resetting wizard-untouched leftovers (`--fresh`)

`mm init --fresh` resets every wizard-untouched canonical key whose
value differs from the built-in default, then proceeds with the
normal wizard. Credentials (`api_key`, `secret`), endpoints
(`base_url`, `webhook.url`), and user-curated lists
(`indexing.exclude_patterns`, `namespace.rules`, etc.) are preserved
unconditionally; user-added keys outside the canonical
`Mem2MemConfig` shape are also preserved. A timestamped backup
(`config.json.bak-<unix-ts>`) is written before any drop so the
previous state is recoverable.

If the web UI is running, restart it after `--fresh` so its
in-memory cache doesn't re-pin the dropped values on the next save.

## Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_STORAGE__BACKEND` | `sqlite` | Storage backend |
| `MEMTOMEM_STORAGE__SQLITE_PATH` | `~/.memtomem/memtomem.db` | SQLite database path |
| `MEMTOMEM_STORAGE__COLLECTION_NAME` | `memories` | Collection name |

## Embedding

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_EMBEDDING__PROVIDER` | `none` | `none` (BM25 only), `onnx` (local), `ollama` (local server), or `openai` (cloud) |
| `MEMTOMEM_EMBEDDING__MODEL` | _(empty)_ | Embedding model name (depends on provider) |
| `MEMTOMEM_EMBEDDING__DIMENSION` | `0` | Vector dimension (must match the model; 0 = BM25 only) |
| `MEMTOMEM_EMBEDDING__BASE_URL` | _(empty)_ | API endpoint URL (Ollama defaults to `http://localhost:11434` when unset) |
| `MEMTOMEM_EMBEDDING__API_KEY` | _(empty)_ | API key (required for OpenAI) |
| `MEMTOMEM_EMBEDDING__BATCH_SIZE` | `64` | Texts per embedding API call |
| `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` | `4` | Max parallel embedding requests |

See [Embedding Providers](embeddings.md) for the supported model list and the dimension values you must use with each one.

## Reset Flow

Changing the embedding provider, model, or dimension *after* content is
indexed produces a **dimension mismatch**: the DB stores vectors of one
shape, the runtime computes another, so semantic search silently falls
back to BM25 only. The tool surface advertises the fix via a `fix` hint,
and `mem_status` reports the mismatch under `warnings[]` (see below).

Resolving it is a two-step process — pick **one** of:

- **Re-index from scratch (destructive, recommended when you really are
  switching models):**

  ```bash
  uv run mm embedding-reset --mode apply-current   # drops old vectors
  uv run mm index                                  # re-embed all files
  ```

  MCP equivalent: `mem_embedding_reset(mode="apply_current")` followed by
  `mem_index(path="...")`.

- **Revert the runtime to the stored model (non-destructive, useful if the
  config drift was accidental):**

  ```bash
  uv run mm embedding-reset --mode revert-to-stored
  ```

  MCP equivalent: `mem_embedding_reset(mode="revert_to_stored")`. The DB
  stays untouched; the server swaps its embedder to match what the DB
  already contains.

`mem_status` emits a `warnings[]` array entry with this schema when a
mismatch is detected:

```
{"kind": "embedding_dim_mismatch",
 "stored":  {"provider": "...", "model": "...", "dimension": N},
 "configured": {"provider": "...", "model": "...", "dimension": M},
 "fix": "uv run mm embedding-reset --mode apply-current",
 "doc": "docs/guides/configuration.md#reset-flow"}
```

The `kind` field is an open enum — new warning kinds (e.g. `stale_index`,
`orphan_vectors`) may be added in future releases without changing the
envelope shape.

## Search

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_SEARCH__DEFAULT_TOP_K` | `10` | Default number of search results |
| `MEMTOMEM_SEARCH__BM25_CANDIDATES` | `50` | BM25 pre-filter candidate count |
| `MEMTOMEM_SEARCH__DENSE_CANDIDATES` | `50` | Dense vector pre-filter candidate count |
| `MEMTOMEM_SEARCH__RRF_K` | `60` | RRF fusion smoothing constant |
| `MEMTOMEM_SEARCH__ENABLE_BM25` | `true` | Enable keyword (FTS5) retriever |
| `MEMTOMEM_SEARCH__ENABLE_DENSE` | `true` | Enable semantic vector retriever |
| `MEMTOMEM_SEARCH__RRF_WEIGHTS` | `[1.0, 1.0]` | RRF weights for `[BM25, Dense]` — adjust to favor one retriever |
| `MEMTOMEM_SEARCH__TOKENIZER` | `unicode61` | FTS tokenizer (`unicode61` or `kiwipiepy`) |
| `MEMTOMEM_SEARCH__CACHE_TTL` | `30.0` | Search result cache TTL in seconds |
| `MEMTOMEM_SEARCH__SYSTEM_NAMESPACE_PREFIXES` | `["archive:"]` | Namespace prefixes excluded from default search (max 10) |

Chunks in system namespaces (e.g. `archive:*`) are hidden from `namespace=None` searches but remain retrievable with an explicit namespace argument. Set to `[]` to make all namespaces searchable by default.

## Query Expansion

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_QUERY_EXPANSION__ENABLED` | `false` | Enable query expansion (pre-retrieval) |
| `MEMTOMEM_QUERY_EXPANSION__MAX_TERMS` | `3` | Maximum terms to add to the query |
| `MEMTOMEM_QUERY_EXPANSION__STRATEGY` | `tags` | `tags`, `headings`, `both`, or `llm` |

The `llm` strategy uses an LLM to generate semantic synonyms (requires `MEMTOMEM_LLM__ENABLED=true`). Other strategies use index metadata and do not need LLM. See [LLM Providers](llm-providers.md).

## Context Window

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_CONTEXT_WINDOW__ENABLED` | `false` | Enable context expansion for all searches |
| `MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE` | `2` | Number of adjacent chunks (±N) to include |

When enabled, search results include surrounding chunks from the same source file. Also available per-call via `mem_search(context_window=N)` or `mem_do(action="expand", params={"chunk_id": "...", "window": 2})`.

## Indexing

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_INDEXING__MEMORY_DIRS` | `["~/.memtomem/memories"]` + auto-discovered | Directories to index (see below) |
| `MEMTOMEM_INDEXING__SUPPORTED_EXTENSIONS` | `[".md",".json",".yaml",".yml",".toml",".py",".js",".ts",".tsx",".jsx"]` | File types accepted by the indexer and file watcher |
| `MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS` | `512` | Maximum tokens per chunk |
| `MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS` | `128` | Merge threshold for short chunks |
| `MEMTOMEM_INDEXING__CHUNK_OVERLAP_TOKENS` | `0` | Token overlap between adjacent chunks |
| `MEMTOMEM_INDEXING__STRUCTURED_CHUNK_MODE` | `original` | JSON/YAML/TOML chunking: `original` or `recursive` |
| `MEMTOMEM_INDEXING__PARAGRAPH_SPLIT_THRESHOLD` | `800` | Split long prose into paragraphs above this token count (must be ≥ 0) |
| `MEMTOMEM_INDEXING__EXCLUDE_PATTERNS` | `[]` | Pathspec (gitignore-style) globs for files the indexer should skip |

### Exclude patterns

`indexing.exclude_patterns` is a `list[str]` of pathspec/gitignore-style
globs evaluated against each file's path **relative to its `memory_dirs`
root**. Built-in denylists for credentials and noise (`oauth_creds.json`,
`*.pem`, `**/.ssh/**`, etc.) are always applied on top — user patterns can
extend them but cannot override them.

```jsonc
// ~/.memtomem/config.d/noise.json — APPEND semantics, layers on defaults
{
  "indexing": {
    "exclude_patterns": [
      "**/subagents/**",         // Claude Code subagent metadata
      "**/antigravity-browser-profile/**",
      "**/.gemini/**/*.json"     // .gemini auto-discover noise
    ]
  }
}
```

> **Caveats:**
> - **Not retroactive.** Adding a pattern only stops *future* indexing. Files
>   already in the index stay until you remove them with
>   `mem_do(action="delete", params={"source_file": "<path>"})`. Force re-index
>   alone (`mem_index force=true`) does not prune.
> - **Match against root-relative paths.** Patterns are evaluated against
>   `path.relative_to(memory_dir)`, so `**/*.json` works, but a pattern that
>   assumes a specific parent (e.g. `**/.claude/**/*.json`) may miss matches
>   when `~/.claude/projects` itself is the auto-discovered memory_dir root.
>   When in doubt, add both root-relative (`oauth_creds.json`) and `**/X`
>   (`**/oauth_creds.json`) forms.

### Auto-discovered memory directories

In addition to `~/.memtomem/memories`, memtomem automatically adds well-known
AI tool directories to `memory_dirs` when they exist on the machine:

| Directory | Tool | Scope |
|-----------|------|-------|
| `~/.claude/projects` | Claude Code | per-project auto-memory |
| `~/.gemini` | Gemini CLI | global `GEMINI.md` |
| `~/.codex/memories` | Codex CLI | global memories |

Auto-discovered directories are appended after `config.d/` and
`config.json` overrides, so they are always available even if you
override `memory_dirs` manually. This means `mem_index` and the file
watcher accept paths under these directories without requiring explicit
`MEMORY_DIRS` configuration.

> **Tip:** Use `mm ingest claude-memory`, `mm ingest gemini-memory`, or
> `mm ingest codex-memory` for richer ingestion with per-tool tagging and
> namespace assignment. Auto-discovery only removes the path restriction —
> it does not apply tool-specific tags or namespaces.

> **Watch out for noise under auto-discovered roots.** `~/.claude/projects`
> sweeps in subagent metadata (`*/subagents/*.meta.json`) and `~/.gemini`
> sweeps in browser profile + OAuth artifacts. The built-in denylist covers
> common credentials but does not filter Claude subagent metadata or
> browser-profile JSON — add a `config.d/` fragment with the
> [`exclude_patterns`](#exclude-patterns) rules above so those don't bloat
> your index.

> **Cloud-sync mounts** (Google Drive Stream, OneDrive Files-On-Demand ON,
> iCloud Optimize Storage) generally do **not** emit fs watcher events to
> macOS/Linux, so the indexer will not auto-pick-up new files placed there
> by the sync client. Either pin the folder offline (per
> [Cloud Sync Client Setup](cloud-sync.md)) or trigger `mem_index` manually
> after files appear.

## Rerank (Cross-Encoder)

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_RERANK__ENABLED` | `false` | Enable cross-encoder reranking after fusion |
| `MEMTOMEM_RERANK__PROVIDER` | `fastembed` | `fastembed` (local ONNX), `cohere` (cloud), or `local` (sentence-transformers) |
| `MEMTOMEM_RERANK__MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | Reranker model name (provider-specific — see below) |
| `MEMTOMEM_RERANK__TOP_K` | `20` | Candidates passed to the reranker (must be > 0) |
| `MEMTOMEM_RERANK__API_KEY` | _(empty)_ | API key (required for Cohere) |

Reranking runs as Stage 3b in the search pipeline — after BM25 + dense fusion, before source/tag filters. If reranking fails with a runtime error the pipeline falls back to the original fused order with a warning; configuration errors (unsupported model name, missing fastembed install) surface directly so the misconfiguration is visible.

### Provider-specific models

- **`fastembed`** (default): local ONNX via the `memtomem[onnx]` extra — no external service, no PyTorch. Built-in catalog includes `Xenova/ms-marco-MiniLM-L-6-v2` (EN, ~80 MB), `jinaai/jina-reranker-v2-base-multilingual` (multilingual, ~1.1 GB), `jinaai/jina-reranker-v1-tiny-en` (EN, 8K context). Custom ONNX exports must be registered via `TextCrossEncoder.add_custom_model()` before the server starts.
- **`cohere`**: Cohere Rerank API (`rerank-english-v3.0`, `rerank-multilingual-v3.0`). Requires `MEMTOMEM_RERANK__API_KEY`.
- **`local`**: sentence-transformers `CrossEncoder` (e.g. `cross-encoder/ms-marco-MiniLM-L-6-v2`). Requires `sentence-transformers` to be installed separately — the `fastembed` provider is usually preferable.

> **Multilingual content:** the default `Xenova/ms-marco-MiniLM-L-6-v2` is English-only. For Korean, Chinese, Japanese, or other non-English content set `MEMTOMEM_RERANK__MODEL=jinaai/jina-reranker-v2-base-multilingual` — the English default noticeably degrades non-English reranking quality.

## Access Frequency Boost

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_ACCESS__ENABLED` | `false` | Enable access-frequency score boost |
| `MEMTOMEM_ACCESS__MAX_BOOST` | `1.5` | Maximum score multiplier (must be ≥ 1.0) |

Frequently accessed chunks get a log-scale score multiplier: 0 accesses → 1.0×, ~10 → ~1.3×, ~100 → max_boost. Runs as Stage 6 in the search pipeline.

## Importance Boost

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_IMPORTANCE__ENABLED` | `false` | Enable multi-factor importance scoring |
| `MEMTOMEM_IMPORTANCE__MAX_BOOST` | `1.5` | Maximum score multiplier (must be ≥ 1.0) |
| `MEMTOMEM_IMPORTANCE__WEIGHTS` | `[0.3, 0.2, 0.3, 0.2]` | Factor weights: `[access, tags, relations, recency]` |

Computes a composite importance score from four factors:

| Factor | Weight (default) | Calculation |
|--------|-------------------|-------------|
| Access count | 0.3 | `log(1 + count)` normalized to ~1.0 at 100 |
| Tag count | 0.2 | `min(tags / 5, 1.0)` — well-tagged = curated |
| Relation count | 0.3 | `log(1 + relations)` normalized to ~1.0 at 20 |
| Recency | 0.2 | Exponential decay (`e^(-0.01 × age_days)`) |

The composite score (0–1) maps to a boost of `[1.0, max_boost]`. Runs as Stage 7 in the search pipeline.

## Decay

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_DECAY__ENABLED` | `false` | Enable time-based score decay |
| `MEMTOMEM_DECAY__HALF_LIFE_DAYS` | `30.0` | Days until decay factor = 0.5 |

## MMR (Maximal Marginal Relevance)

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_MMR__ENABLED` | `false` | Enable result diversification |
| `MEMTOMEM_MMR__LAMBDA_PARAM` | `0.7` | `0.0` = max diversity, `1.0` = pure relevance |

> **When to enable.** Indexes that mix overview + detail files for the same
> topic (e.g. a `MEMORY.md` index plus the underlying `feedback_*.md` files
> it summarizes) tend to surface near-duplicate hits in the top results.
> Turning MMR on with the default `LAMBDA_PARAM=0.7` favors relevance but
> drops obvious duplicates, with negligible cost. memtomem does not dedup
> at index time — see also `mem_dedup_scan` / `mem_dedup_merge`
> ([User Guide](user-guide.md)) for a manual pass on accumulated overlap.

## Namespace

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE` | `default` | Default namespace for new chunks |
| `MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS` | `false` | Auto-derive namespace from folder name |

`enable_auto_ns=true` uses the file's **immediate parent folder name** as
the namespace, except for files sitting directly in a `memory_dirs` root
(those fall back to `default_namespace`). This works well for shallow
folder trees like `memtomem-memories/team/X.md` → `team`, but produces
low-signal namespaces (`subagents`, `<UUID>`) when applied blindly under
auto-discovered roots like `~/.claude/projects`.

> **Recommendation.** Filter noise via `exclude_patterns` *before* enabling
> `auto_ns`, otherwise opaque parent-folder names (like a Claude Code
> session UUID) end up as namespaces.

For richer ingestion, prefer the explicit `namespace` argument on
`mem_index` to encode source/tool/content in the namespace itself —
colon-prefix labels group well in the Web UI Sources view:

```
mem_index(path="~/Library/CloudStorage/.../memtomem-memories/team",
          namespace="gdrive:team")
mem_index(path="~/.claude/projects/<...>/memory",
          namespace="claude:memory")
```

### Namespace rules (path-based auto-tagging)

Instead of passing `namespace=` on every `mem_index` call, declare
path → namespace rules in your config so the indexer applies them
automatically. Rules match **before** `enable_auto_ns` and lose to an
explicit `namespace` argument.

Example `~/.memtomem/config.d/10-namespace-rules.json`:

```json
{
  "namespace": {
    "rules": [
      { "path_glob": "~/.claude/projects/*/memory/**",      "namespace": "claude:memory" },
      { "path_glob": "~/.claude/projects/*/*/subagents/**", "namespace": "claude:subagents" },
      { "path_glob": "~/.codex/memories/**",                "namespace": "codex:memories" },
      { "path_glob": "~/.gemini/**",                        "namespace": "gemini:{parent}" },
      { "path_glob": "~/Library/CloudStorage/GoogleDrive-*/**/memtomem-memories/*/**",
        "namespace": "gdrive:{parent}" }
    ]
  }
}
```

**Semantics:**

- Patterns use **gitignore syntax** (`**` for recursive, `*` for a
  single segment). Leading `~/` is expanded at load time.
- Matching is **case-insensitive** and runs against the absolute
  resolved file path — the same engine as `indexing.exclude_patterns`.
- **First match wins.** Order rules from most specific to least within
  a fragment.
- `{parent}` in the namespace string expands to the immediate parent
  folder name. If that name would be empty, the rule is skipped and the
  next rule / `auto_ns` / `default_namespace` is tried.
- Merge strategy is **APPEND**: multiple `config.d/*.json` fragments
  contribute rules without overwriting. **Fragments load in
  alphabetical filename order**, so use numeric prefixes
  (`10-claude.json`, `20-gdrive.json`, `99-override.json`) to control
  precedence across fragments.
- Placeholder whitelist: only `{parent}` is supported in this release.
  Unknown placeholders (e.g. `{unknown}`) cause config load to fail so
  typos are caught at startup.

**Verifying your rules:**

```bash
# Show effective config including merged rules:
mm config show | grep -A 20 namespace

# After editing rules, force re-index so existing chunks pick up the
# new namespace:
mm mem index ~/.claude/projects --force

# Inspect namespace distribution:
mm session list             # CLI
# http://localhost:8080/#sources   # Web UI Sources view (colon prefixes
                                   # group into collapsible sections)
```

Search results surface the namespace label, so you can confirm a rule
fired:

```bash
mm mem search "your query"
# → "[claude:memory] …"
```

## Policy

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_POLICY__ENABLED` | `false` | Enable the background policy scheduler |
| `MEMTOMEM_POLICY__SCHEDULER_INTERVAL_MINUTES` | `60.0` | Minutes between policy runs |
| `MEMTOMEM_POLICY__MAX_ACTIONS_PER_RUN` | `100` | Cumulative action cap per scheduled run (checked between policies) |

When enabled, all policies created via `mem_policy_add` are executed periodically. Policies can always be run on demand via `mem_policy_run` regardless of this setting. The action count semantics vary by policy type (e.g. archived chunks vs consolidated groups).

### Policy type config keys

Each policy has a `config` JSON dict passed to `mem_policy_add`. The keys
depend on `policy_type`:

**`auto_archive`**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_age_days` | int | _(required)_ | Chunks older than this are archived |
| `archive_namespace` | str | `"archive"` | Destination namespace |
| `age_field` | str | `"created_at"` | `"created_at"` or `"last_accessed_at"` |
| `min_access_count` | int\|null | null | Only archive if `access_count ≤` this |
| `max_importance_score` | float\|null | null | Only archive if `importance_score <` this |
| `archive_namespace_template` | str\|null | null | Per-chunk expansion, e.g. `"archive:{first_tag}"` |

**`auto_promote`** (inverse of auto_archive)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `source_prefix` | str | `"archive"` | Namespace prefix to search for candidates |
| `target_namespace` | str | `"default"` | Destination namespace for promoted chunks |
| `min_access_count` | int | `3` | Minimum access count to qualify |
| `min_importance_score` | float\|null | null | Minimum importance score (AND with access count) |
| `recency_days` | int\|null | null | Only promote if accessed within this many days |

**`auto_consolidate`**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `min_group_size` | int | `3` | Minimum chunks per source to trigger consolidation |
| `max_groups` | int | `10` | Maximum source groups to process per run |
| `max_bullets` | int | `20` | Maximum bullet points in heuristic summary |
| `keep_originals` | bool | `true` | Keep original chunks after consolidation (recommended) |
| `summary_namespace` | str | `"archive:summary"` | Namespace for generated summary chunks |

## Webhook

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_WEBHOOK__ENABLED` | `false` | Enable webhook notifications |
| `MEMTOMEM_WEBHOOK__URL` | _(empty)_ | HTTP(S) endpoint to receive POST requests |
| `MEMTOMEM_WEBHOOK__EVENTS` | `["add", "delete", "search"]` | Event types to fire (currently emitted: `add`, `search`, `ask`) |
| `MEMTOMEM_WEBHOOK__SECRET` | _(empty)_ | HMAC-SHA256 signing key — when set, each request includes `X-Webhook-Signature: sha256=<hex>` |
| `MEMTOMEM_WEBHOOK__TIMEOUT_SECONDS` | `10.0` | HTTP request timeout per attempt |

Webhooks fire asynchronously with up to 3 retries on failure. The URL must be `http` or `https` — private/loopback IPs are rejected at startup.

### Minimal working example

```bash
export MEMTOMEM_WEBHOOK__ENABLED=true
export MEMTOMEM_WEBHOOK__URL=https://example.com/hooks/memtomem
export MEMTOMEM_WEBHOOK__SECRET=my-signing-key
```

The webhook body is JSON:

```json
{
  "event": "add",
  "data": {
    "file": "/path/to/memory.md",
    "chunks_indexed": 1
  }
}
```

To verify the signature in your handler:

```python
import hashlib, hmac

expected = hmac.new(
    b"my-signing-key", request.body, hashlib.sha256
).hexdigest()
assert request.headers["X-Webhook-Signature"] == f"sha256={expected}"
```

## Consolidation Schedule

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_CONSOLIDATION_SCHEDULE__ENABLED` | `false` | Enable periodic auto-consolidation |
| `MEMTOMEM_CONSOLIDATION_SCHEDULE__INTERVAL_HOURS` | `24.0` | Hours between consolidation runs |
| `MEMTOMEM_CONSOLIDATION_SCHEDULE__MIN_GROUP_SIZE` | `3` | Minimum chunks per source to trigger consolidation |
| `MEMTOMEM_CONSOLIDATION_SCHEDULE__MAX_GROUPS` | `10` | Maximum source groups to process per run |

## Health Watchdog

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_HEALTH_WATCHDOG__ENABLED` | `false` | Enable periodic health monitoring |
| `MEMTOMEM_HEALTH_WATCHDOG__HEARTBEAT_INTERVAL_SECONDS` | `60.0` | Lightweight heartbeat check frequency |
| `MEMTOMEM_HEALTH_WATCHDOG__DIAGNOSTIC_INTERVAL_SECONDS` | `300.0` | Diagnostic check frequency |
| `MEMTOMEM_HEALTH_WATCHDOG__DEEP_INTERVAL_SECONDS` | `3600.0` | Deep/expensive check frequency |
| `MEMTOMEM_HEALTH_WATCHDOG__MAX_SNAPSHOTS` | `1000` | Maximum historical health snapshots to retain |
| `MEMTOMEM_HEALTH_WATCHDOG__ORPHAN_CLEANUP_THRESHOLD` | `10` | Orphaned files before auto-cleanup triggers |
| `MEMTOMEM_HEALTH_WATCHDOG__AUTO_MAINTENANCE` | `true` | Perform auto-maintenance actions on critical alerts |

The watchdog runs three tiers of checks at different intervals. Use `mem_watchdog` (or `mem_do(action="watchdog")`) to query health status on demand.

## LLM

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_LLM__ENABLED` | `false` | Enable LLM provider (required for LLM-powered features) |
| `MEMTOMEM_LLM__PROVIDER` | `ollama` | `ollama` (local), `openai` (cloud/compatible), or `anthropic` |
| `MEMTOMEM_LLM__MODEL` | _(empty)_ | Model name (empty uses provider default: ollama→gemma4:e2b, openai→gpt-4.1-mini, anthropic→claude-haiku-4-5-20251001) |
| `MEMTOMEM_LLM__BASE_URL` | `http://localhost:11434` | API endpoint URL |
| `MEMTOMEM_LLM__API_KEY` | _(empty)_ | API key (required for OpenAI/Anthropic/OpenRouter) |
| `MEMTOMEM_LLM__MAX_TOKENS` | `1024` | Maximum response tokens |
| `MEMTOMEM_LLM__TIMEOUT` | `60.0` | Request timeout in seconds |

The `openai` provider works with any OpenAI-compatible endpoint (LM Studio, vLLM, OpenRouter, etc.) — set `BASE_URL` to the server's address. See [LLM Providers](llm-providers.md) for setup examples.

## Tool Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_TOOL_MODE` | `core` | Which MCP tools are exposed: `core` (9 tools), `standard` (~32 incl. `mem_do`), `full` (74) |

In `core` mode, use `mem_do(action="...", params={...})` to access any of the 65+ non-core actions. Fewer tools means less context usage for AI agents.

## Querying and Modifying at Runtime

You can also inspect and change settings at runtime via the `mem_config` MCP tool (requires `MEMTOMEM_TOOL_MODE=full`; in `core` or `standard` mode, use `mm config` CLI or the Web UI Settings tab):

```
mem_config()                                      # Output all settings as JSON
mem_config(key="search.default_top_k")            # Query a single value
mem_config(key="search.default_top_k", value="20")  # Change and persist
```
