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

### Auto-discovered memory directories

In addition to `~/.memtomem/memories`, memtomem automatically adds well-known
AI tool directories to `memory_dirs` when they exist on the machine:

| Directory | Tool | Scope |
|-----------|------|-------|
| `~/.claude/projects` | Claude Code | per-project auto-memory |
| `~/.gemini` | Gemini CLI | global `GEMINI.md` |
| `~/.codex/memories` | Codex CLI | global memories |

Auto-discovered directories are appended after `config.json` overrides, so
they are always available even if you override `memory_dirs` manually. This
means `mem_index` and the file watcher accept paths under these directories
without requiring explicit `MEMORY_DIRS` configuration.

> **Tip:** Use `mm ingest claude-memory`, `mm ingest gemini-memory`, or
> `mm ingest codex-memory` for richer ingestion with per-tool tagging and
> namespace assignment. Auto-discovery only removes the path restriction —
> it does not apply tool-specific tags or namespaces.

## Rerank (Cross-Encoder)

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_RERANK__ENABLED` | `false` | Enable cross-encoder reranking after fusion |
| `MEMTOMEM_RERANK__PROVIDER` | `cohere` | `cohere` (cloud) or `local` |
| `MEMTOMEM_RERANK__MODEL` | `rerank-english-v3.0` | Reranker model name |
| `MEMTOMEM_RERANK__TOP_K` | `20` | Candidates passed to the reranker (must be > 0) |
| `MEMTOMEM_RERANK__API_KEY` | _(empty)_ | API key (required for Cohere) |

Reranking runs as Stage 3b in the search pipeline — after BM25 + dense fusion, before source/tag filters. If the reranker call fails, the pipeline falls back to the original fused order with a warning.

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

## Namespace

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE` | `default` | Default namespace for new chunks |
| `MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS` | `false` | Auto-derive namespace from folder name |

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
