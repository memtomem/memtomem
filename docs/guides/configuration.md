# Configuration Reference

memtomem reads configuration from environment variables. All variables use the `MEMTOMEM_` prefix, with nested sections separated by `__` (double underscore). Unprefixed names are never read, with one documented exception: the Langfuse SDK credentials described in [Session Trace](#session-trace).

```bash
# Example: switch from Ollama to OpenAI
export MEMTOMEM_EMBEDDING__PROVIDER=openai
export MEMTOMEM_EMBEDDING__MODEL=text-embedding-3-small
export MEMTOMEM_EMBEDDING__DIMENSION=1536
export MEMTOMEM_EMBEDDING__API_KEY=sk-...
```

For interactive setup, run `mm init` instead of editing env vars by hand.

**On this page**

- [Precedence and merge behaviour](#precedence-and-merge-behaviour)
- [Storage](#storage)
- [Embedding](#embedding)
- [Reset Flow](#reset-flow)
- [Search](#search)
- [Query Expansion](#query-expansion)
- [Context Window](#context-window)
- [Indexing](#indexing)
- [Rerank (Cross-Encoder)](#rerank-cross-encoder)
- [Access Frequency Boost](#access-frequency-boost)
- [Importance Boost](#importance-boost)
- [Decay](#decay)
- [MMR (Maximal Marginal Relevance)](#mmr-maximal-marginal-relevance)
- [Namespace](#namespace)
- [Policy](#policy)
- [Webhook](#webhook)
- [Consolidation Schedule](#consolidation-schedule)
- [Health Watchdog](#health-watchdog)
- [Scheduler](#scheduler)
- [LLM](#llm)
- [Session Summary](#session-summary)
- [Session Trace](#session-trace)
- [Tool Mode](#tool-mode)
- [Web UI Mode](#web-ui-mode)
- [Context Gateway](#context-gateway)
- [Hooks](#hooks)
- [Advanced / operator environment variables](#advanced--operator-environment-variables)
- [Querying and Modifying at Runtime](#querying-and-modifying-at-runtime)

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
| `search.system_namespace_prefixes` | APPEND | Integrations can add further hidden namespaces on top of the `archive:` / `agent-runtime:` defaults |
| `webhook.events` | APPEND | Fragments can subscribe to additional event types |
| `search.rrf_weights` | REPLACE | Positional tuning knob — appending would misalign `[BM25, Dense]` slots |
| `importance.weights` | REPLACE | Same positional constraint |

`config.json` always replaces, regardless of strategy — it's the
explicit-user-override layer. Use a fragment in `config.d/` if you want
APPEND semantics.

### External edits while the Web UI is running

The Web UI server re-reads `config.json` and `config.d/*.json` on every
`GET /api/config` and at the top of every config-writing endpoint
(`PATCH /api/config`, `POST /api/config/save`, `POST /api/memory-dirs/add`,
`POST /api/memory-dirs/remove`). This means:

- `mm config set ...` or a manual editor save while the server is running
  becomes visible on the next UI interaction (or when the tab regains
  focus), without a restart.
- A subsequent UI save merges against the *current* disk state rather
  than overwriting the external change with a stale in-memory copy.
- If `config.json` is truncated or otherwise invalid when the server
  tries to reload it, the Web UI keeps the last-known-good in-memory
  config, surfaces a red banner on the Config tab, and refuses to save
  (HTTP 409) until the file is fixed. Run `mm init --fresh` or edit
  the file by hand to recover.

Change detection is a cheap `os.stat` on `config.json` plus every
fragment in `config.d/`, so GET latency is effectively unchanged. No
filesystem watchdog is involved.

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

Path-typed fields (`storage.sqlite_path`, `indexing.memory_dirs`)
under `$HOME` serialize as `~/...` on write, so a config copied to a
machine with a different `$HOME` resolves correctly via
`Path.expanduser()` on read. Paths *outside* `$HOME` (`/var/...`,
`/opt/...`) stay absolute because their meaning is genuinely
machine-specific.

`indexing.memory_dirs` participates in delta-only save, so on the
machine where it was set the file typically omits it. When copying an
existing `config.json` to a new machine, any `indexing.memory_dirs`
entry that points at provider-specific paths (e.g.
`~/.claude/projects/<project-A>/memory/`) carries over as-is — the
project-A path won't exist on the destination and won't be replaced
by detection on the target. Reset it explicitly when migrating:

```bash
# Option 1: targeted removal of the carried-over entry
mm config unset indexing.memory_dirs

# Option 2: re-run the wizard with --fresh
mm init --fresh

# Option 3: remove the indexing section by hand
#          (edit ~/.memtomem/config.json)
```

> **Backward compatibility.** Configs written before home-relative
> serialization landed (≤ 0.1.36) carry absolute paths. Loading them
> on the same machine still works. The next save through any writer
> (`mm config set`, the Web UI, `mm init`) rewrites home-rooted paths
> into `~/...` form automatically.

> **Syncing memories across personal devices?** See
> [Multi-device sync](multi-device-sync.md) for the recommended
> namespace-aligned layout, a `.gitignore` recipe that keeps `*.db`
> out of the synced tree, and `mm sync-doctor` for catching the
> common footguns.

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
| `MEMTOMEM_EMBEDDING__THREADS` | `4` | ONNX intra-op thread cap for the local `fastembed` provider |
| `MEMTOMEM_EMBEDDING__PROGRESS_THRESHOLD` | `32` | Show an embedding progress indicator once a batch exceeds this many texts |

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
  uv run mm index <memory_dir>                     # re-embed (repeat per memory_dir)
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

> **Stop other `mm` processes first.** Run `embedding-reset` against an
> idle DB — shut down `mm web`, the MCP server, and any background `mm
> index` runs before invoking it. If two processes briefly co-exist with
> different embedding models pointing at the same SQLite file, race-loser
> chunk inserts are silently dropped at the storage layer (the unique key
> is content-only, so different-model embeddings for the same content are
> indistinguishable). The startup dimension gate (issue #298) catches
> the common case where the new model has a different dimension, but
> **same-dimension** model swaps slip past it. See issue #707 for the
> full failure mode.

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

Search fuses two retrievers: **BM25** (keyword/lexical matching, via SQLite FTS5)
and **dense** (semantic vector) search, combined with **RRF** (Reciprocal Rank
Fusion). The variables below tune each retriever and the fusion.

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
| `MEMTOMEM_SEARCH__SYSTEM_NAMESPACE_PREFIXES` | `["archive:", "agent-runtime:"]` | Namespace prefixes excluded from default search (max 10) |

Chunks in system namespaces (e.g. `archive:*` and the per-agent `agent-runtime:*` buckets) are hidden from `namespace=None` searches but remain retrievable with an explicit namespace argument. Set to `[]` to make all namespaces searchable by default.

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

### `memory_dirs` — reactive watch vs one-shot seed

`indexing.memory_dirs` is the source-of-truth list for the file watcher
that the running MCP server (`memtomem-server`) starts on boot. The
watcher is **reactive only** — it
reindexes files when the filesystem emits modify / create / move events
for paths under these directories. Pre-existing files on disk at the
time the watcher starts are **NOT auto-scanned**; you seed them once
with either

- `mm index <dir>` from the CLI, or
- the **Reindex** button per memory_dir in `mm web`.

Both paths are idempotent: chunks are content-hashed, so unchanged files
are skipped on re-runs. This is why the `mm init` wizard's `Next steps`
prints `mm index {memory_dir}` as step 1 — once seeded, subsequent edits
flow through the watcher automatically (as long as the MCP server is
running).

> **Adding a folder via the Web UI** registers and indexes in one call:
> `POST /api/memory-dirs/add` defaults to `auto_index=true`, so the
> watcher sees the directory **and** the existing files are seeded
> immediately. Pass `auto_index=false` if you want register-only
> behavior (config-write without the seed scan) — useful for staging a
> large folder before a controlled `mm index` run.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_INDEXING__MEMORY_DIRS` | `["~/.memtomem/memories"]` (+ provider folders selected in `mm init`) | Directories watched for reactive re-index (see above) |
| `MEMTOMEM_INDEXING__SUPPORTED_EXTENSIONS` | `[".md",".json",".yaml",".yml",".toml",".py",".js",".ts",".tsx",".jsx"]` | File types accepted by the indexer and file watcher |
| `MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS` | `512` | Maximum tokens per chunk |
| `MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS` | `128` | Merge threshold for short chunks |
| `MEMTOMEM_INDEXING__CHUNK_OVERLAP_TOKENS` | `0` | Token overlap between adjacent chunks |
| `MEMTOMEM_INDEXING__STRUCTURED_CHUNK_MODE` | `original` | JSON/YAML/TOML chunking: `original` or `recursive` |
| `MEMTOMEM_INDEXING__PARAGRAPH_SPLIT_THRESHOLD` | `800` | Split long prose into paragraphs above this token count (must be ≥ 0) |
| `MEMTOMEM_INDEXING__EXCLUDE_PATTERNS` | `[]` | Pathspec (gitignore-style) globs for files the indexer should skip |
| `MEMTOMEM_INDEXING__STARTUP_BACKFILL` | `false` | When `true`, `mm web` runs a one-shot backfill scan over `memory_dirs` on boot to catch files added while the server was down. Off by default — multi-minute embed jobs blocked the server on a multi-GB memory_dir during 0.1.24 testing, so the wizard offers it as opt-in. `mm index <dir>` and the Web UI per-dir Reindex button cover ad-hoc backfills idempotently without flipping this. |
| `MEMTOMEM_INDEXING__TARGET_CHUNK_TOKENS` | `384` | Pass-2 semantic-packing target chunk size; `0` disables packing |
| `MEMTOMEM_INDEXING__PROJECT_MEMORY_DIRS` | `[]` | Additional project-tier index roots (ADR-0011); APPEND-merged across `config.d/` fragments |
| `MEMTOMEM_INDEXING__AUTO_SUMMARIZE` | `false` | Generate a per-source LLM summary chunk at index time (requires LLM enabled) |
| `MEMTOMEM_INDEXING__SUMMARY_LANGUAGE` | `en` | Language for auto-generated per-source summaries |
| `MEMTOMEM_INDEXING__SUMMARY_MAX_INPUT_CHARS` | `3000` | Skip the per-source summary when the source body exceeds this many characters |
| `MEMTOMEM_INDEXING__SUMMARY_MAX_TOKENS` | `256` | Output token cap for each per-source summary |

### Exclude patterns

`indexing.exclude_patterns` is a `list[str]` of pathspec/gitignore-style
globs evaluated against each file's path **relative to its `memory_dirs`
root**. Built-in denylists for credentials and noise (`oauth_creds.json`,
`*.pem`, `**/.ssh/**`, etc.) are always applied on top — user patterns can
extend them but cannot override them.

Save the following as `~/.memtomem/config.d/noise.json` (APPEND
semantics — fragments layer on top of the defaults, they don't replace
them):

```json
{
  "indexing": {
    "exclude_patterns": [
      "**/subagents/**",
      "**/antigravity-browser-profile/**",
      "**/.gemini/**/*.json",
      "**/.obsidian/**"
    ]
  }
}
```

| Pattern | Why |
|---|---|
| `**/subagents/**` | Claude Code subagent metadata |
| `**/antigravity-browser-profile/**` | Antigravity browser profile data |
| `**/.gemini/**/*.json` | Defensive — only relevant if you manually add `~/.gemini/` to `memory_dirs` |
| `**/.obsidian/**` | Obsidian vault metadata (`workspace.json`, plugin state) when a vault is itself a `memory_dir` |

The fragment loader uses strict `json.loads`, so the file must be pure
JSON — no `//` comments, no trailing commas, no `jsonc` extensions.

> **Caveats:**
> - **Not retroactive.** Adding a pattern only stops *future* indexing. Files
>   already in the index stay until you remove them with
>   `mem_do(action="delete", params={"source_file": "<path>"})`. Force re-index
>   alone (`mem_index force=true`) does not prune.
> - **Match against root-relative paths.** Patterns are evaluated against
>   `path.relative_to(memory_dir)`, so `**/*.json` works, but a pattern that
>   assumes a specific parent (e.g. `**/.claude/**/*.json`) may miss matches
>   when a Claude Code per-project memory dir is itself the `memory_dir`
>   root. When in doubt, add both root-relative (`oauth_creds.json`) and
>   `**/X` (`**/oauth_creds.json`) forms.

### Provider memory folders (opt-in via `mm init`)

memtomem can index AI tool memory folders alongside `~/.memtomem/memories`,
but only when you explicitly opt in during `mm init`. The wizard's
"Provider memory folders" step shows whichever of these are detected on
your machine and lets you accept them per category:

| Category | Source | Scope |
|----------|--------|-------|
| `claude-memory` | `~/.claude/projects/<project>/memory/` | Claude Code per-project auto-memory ([official docs](https://code.claude.com/docs/en/memory)) |
| `claude-plans` | `~/.claude/plans/` | Claude Code plan files (local convention) |
| `codex` | `~/.codex/memories/` | Codex memories ([official docs](https://developers.openai.com/codex/memories)) |

#### How provider memory namespaces map back to each tool

`mm init` adds namespace rules so provider memory folders stay searchable by
origin instead of collapsing into one generic namespace. These are memtomem
indexing labels; they do not replace each tool's own memory or instruction
scope model.

| Tool | Upstream memory shape | memtomem namespace shape |
|------|-----------------------|--------------------------|
| Claude Code | Auto memory is per repository at `~/.claude/projects/<project>/memory/`, with a `MEMORY.md` index plus topic files. Claude also has explicit `CLAUDE.md` / `.claude/rules/` instruction scopes. | `claude:<project>` via the `<project>` folder above `memory/`. `MEMORY.md` is treated as a provider index file, so the topic files carry most searchable detail. |
| Codex | Generated memories live under `~/.codex/memories/` and include summaries, durable entries, recent inputs, and supporting evidence from prior threads ([official docs](https://developers.openai.com/codex/memories)). | `codex:rollout_summaries` for per-session recap files, `codex:extensions` for ad-hoc/manual note extensions, and `codex:global` for the remaining consolidated top-level memory files. The split follows the on-disk `rollout_summaries/` and `extensions/` subdirectories — an observed layout that Codex's docs don't formally specify, so re-check it if a Codex release reshapes the directory. |
| Antigravity CLI | Successor to Gemini CLI ([transition notice](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)). Its global instructional memory is the single file `~/.gemini/GEMINI.md` — the same hardcoded path the legacy Gemini CLI used ([docs](https://antigravity.google/docs/agent-features)). | Not added as a watched `memory_dirs` provider because the canonical memory surface is a file, not a directory. Use `mm ingest gemini-memory` for a one-shot import with `gemini-memory:<slug>` namespaces — the command keeps the `gemini-memory` name because Antigravity's global memory file is still `~/.gemini/GEMINI.md`. |

Accepted categories get appended directly to `indexing.memory_dirs` in
`~/.memtomem/config.json`. Per-project Claude memory subdirs without any
`*.md` files are skipped so empty session scaffolding doesn't pollute your
index. New Claude Code projects created after the wizard runs are **not**
auto-indexed — re-run `mm init` or use
`mm config set indexing.memory_dirs` to add them when you want them
searchable.

Non-interactive mode supports `--include-provider` (repeatable):

```bash
mm init -y --include-provider claude-memory --include-provider codex
```

Asking for a category with no detected dirs is a silent no-op, not an
error.

#### Why Gemini is not in the list

Gemini CLI's memory surface is the single file `~/.gemini/GEMINI.md`,
which doesn't fit a `memory_dirs` (directory) abstraction, and the parent
`~/.gemini/` directory contains secrets like `oauth_creds.json`. For
Gemini users — and Antigravity CLI (`agy`) users, which read the same
`~/.gemini/GEMINI.md` — run `mm ingest gemini-memory` for a one-shot import;
it applies tool-specific tags and skips the noise.

#### Migrating from `auto_discover` (legacy)

Earlier releases used a runtime flag (`indexing.auto_discover`, default
True) that silently appended provider home directories on every startup.
That flag is now **deprecated** and serves only as a one-shot migration
trigger:

- If your existing `~/.memtomem/config.json` carries `auto_discover: true`
  (or omits it, in which case it defaults True), the next CLI/server
  startup converts the canonical provider memory dirs that exist on your
  machine into explicit `memory_dirs` entries, then flips the flag to
  False and persists both changes atomically.
- The migration prints a single INFO log line. Subsequent startups see
  `auto_discover: false` and do nothing.
- Brand-new installs (no `config.json` yet) skip migration entirely —
  the wizard is the only path that adds provider dirs.

If your old install was indexing `~/.claude/projects/` wholesale (session
JSONL transcripts, staging dirs, etc.), the migration narrows that to the
canonical `*/memory/` subdirs only. The migration only narrows what gets
indexed *going forward* — it does not retroactively delete chunks already
stored from the wider scan. To reclaim those, run `mm purge
--matching-excluded`: it deletes stored chunks whose source the indexer would
now exclude (built-in noise/secret denylist, `indexing.exclude_patterns`, and
provider index-file conventions such as a `claude-memory` root's `MEMORY.md`).
It prints a dry-run summary by default; re-run with `--apply` to delete. For
any leftover source the exclude rules don't cover, remove it directly with
`mem_delete(source_file=...)`.

> **Tip:** `mm ingest claude-memory`, `mm ingest gemini-memory`, and
> `mm ingest codex-memory` apply per-tool tagging and namespace assignment
> on top of indexing — useful when you want richer metadata than the plain
> `memory_dirs` path-based indexing provides.

> **Cloud-sync mounts** (Google Drive Stream, OneDrive Files-On-Demand ON,
> iCloud Optimize Storage) generally do **not** emit fs watcher events to
> macOS/Linux, so the indexer will not auto-pick-up new files placed there
> by the sync client. Either pin the folder offline in your cloud client's
> settings or trigger `mem_index` manually after files appear.

## Rerank (Cross-Encoder)

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_RERANK__ENABLED` | `false` | Enable cross-encoder reranking after fusion |
| `MEMTOMEM_RERANK__PROVIDER` | `fastembed` | `fastembed` (local ONNX), `cohere` (cloud), or `local` (sentence-transformers) |
| `MEMTOMEM_RERANK__MODEL` | `Xenova/ms-marco-MiniLM-L-6-v2` | Reranker model name (provider-specific — see below) |
| `MEMTOMEM_RERANK__OVERSAMPLE` | `2.0` | Candidate-pool multiplier applied to response `top_k` |
| `MEMTOMEM_RERANK__MIN_POOL` | `20` | Lower bound on the candidate pool (floor for small queries) |
| `MEMTOMEM_RERANK__MAX_POOL` | `200` | Upper bound on the candidate pool (cost cap for large queries) |
| `MEMTOMEM_RERANK__API_KEY` | _(empty)_ | API key (required for Cohere) |

Reranking runs as Stage 3b in the search pipeline — after BM25 + dense fusion, before source/tag filters. The candidate pool passed to the cross-encoder is

```
pool = max(min_pool, min(max_pool, int(oversample * response_top_k)))
```

so the pool scales with the caller's requested `top_k` while staying bounded by both the floor (rescues small queries) and the cap (controls cost on large ones). The reranker then returns the caller's `top_k` — pool sizing only controls how many items it gets to choose from. If reranking fails with a runtime error the pipeline falls back to the original fused order, trimmed to the caller's `top_k`, with a warning; configuration errors (unsupported model name, missing fastembed install) surface directly so the misconfiguration is visible.

`rerank.enabled`, `rerank.oversample`, `rerank.min_pool`, and `rerank.max_pool` are runtime-tunable via `mm config set` or the Web UI Settings panel — no restart required. `rerank.provider` / `rerank.model` / `rerank.api_key` are load-time only because the reranker instance is cached on startup.

> **Deprecated:** earlier releases exposed `MEMTOMEM_RERANK__TOP_K` / `rerank.top_k` as an absolute candidate-pool size. The field still loads (legacy configs are migrated to `rerank.min_pool` with a `DeprecationWarning`) but is slated for removal in a future release. Use `rerank.oversample` + `rerank.min_pool` + `rerank.max_pool` instead.

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
> ([Reference](reference.md)) for a manual pass on accumulated overlap.

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
opt-in provider roots like `~/.claude/projects/<project>/memory/`.

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
      { "path_glob": "~/.codex/memories/rollout_summaries/**", "namespace": "codex:rollout_summaries" },
      { "path_glob": "~/.codex/memories/extensions/**",        "namespace": "codex:extensions" },
      { "path_glob": "~/.codex/memories/**",                   "namespace": "codex:global" },
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
- Placeholder whitelist: `{parent}` (the immediate parent folder, equivalent
  to `{ancestor:0}`) and `{ancestor:N}` (the folder `N` levels above the
  immediate parent — `{ancestor:0}` is the parent, `{ancestor:1}` the
  grandparent) are supported. Unknown placeholders (e.g. `{unknown}`), or a
  non-integer / negative `ancestor` index, cause config load to fail so typos
  are caught at startup.

**Verifying your rules:**

```bash
# Show effective config including merged rules:
mm config show | grep -A 20 namespace

# After editing rules, force re-index so existing chunks pick up the
# new namespace:
mm index ~/.claude/projects --force

# Inspect namespace distribution — open the Web UI Sources view:
#   http://localhost:8080/#sources    (colon prefixes group into collapsible
#                                      sections). There is no dedicated CLI
#   listing for rule-derived namespaces; the search below shows the label.
```

Search results surface the namespace label, so you can confirm a rule
fired:

```bash
mm search "your query"
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

## Scheduler

Cron scheduler for memory-lifecycle jobs — compaction, importance decay,
dead-link cleanup, and dedup scans (see
[`mm schedule`](reference/automation.md#9-scheduled-jobs--mm-schedule-schedule_)). Both
`scheduler.enabled` **and** `health_watchdog.enabled` must be true for
schedules to fire: the dispatcher rides the watchdog loop, so the watchdog
gate wins.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_SCHEDULER__ENABLED` | `false` | Master switch for cron-driven scheduled jobs (also gated by `health_watchdog.enabled`) |
| `MEMTOMEM_SCHEDULER__MAX_CONCURRENT_JOBS` | `1` | Maximum scheduled jobs running at once (must be ≥ 1) |
| `MEMTOMEM_SCHEDULER__DEFAULT_TIMEZONE` | `utc` | Schedule timezone. Phase A honors only `utc`; other values warn at startup and fall back to UTC |
| `MEMTOMEM_SCHEDULER__RUNNER_TIMEOUT_SECONDS` | `300.0` | Per-job wall-clock timeout in seconds (must be > 0) |

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

The `openai` provider works with any OpenAI-compatible endpoint (LM Studio, vLLM, OpenRouter, etc.) — set `MEMTOMEM_LLM__BASE_URL` to the server's address. See [LLM Providers](llm-providers.md) for setup examples.

## Session Summary

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_SESSION_SUMMARY__AUTO` | `true` | When `mem_session_end` is called without `summary=`, run an LLM auto-summary over chunks added during the session |
| `MEMTOMEM_SESSION_SUMMARY__MIN_CHUNKS` | `5` | Minimum chunks added during the session before auto-summary fires |
| `MEMTOMEM_SESSION_SUMMARY__MAX_SUMMARY_TOKENS` | `500` | Output cap for the generated summary |
| `MEMTOMEM_SESSION_SUMMARY__MAX_INPUT_CHARS` | `60000` | Skip auto-summary when the assembled chunk body exceeds this; pass an explicit `summary=` instead |
| `MEMTOMEM_SESSION_SUMMARY__MAX_SUMMARY_LINKS` | `50` | Cap on `chunk_links` rows (`link_type="summarizes"`) written from the summary chunk back to the source chunks. Newest first, tail dropped. |
| `MEMTOMEM_SESSION_SUMMARY__EXPANSION_LOOKUP_TOP_K` | `3` | Rescue leg: top-K session-summary chunks examined to find source files to re-retrieve |
| `MEMTOMEM_SESSION_SUMMARY__EXPANSION_SCORE_THRESHOLD` | `0.3` | Minimum summary-chunk score before its summarized source files are pulled into the rescue leg |
| `MEMTOMEM_SESSION_SUMMARY__EXPANSION_RESCUE_WEIGHT` | `0.5` | RRF weight applied to the rescue-leg result list (past-session source chunks) |

Requires `MEMTOMEM_LLM__ENABLED=true` and a configured provider. Generated summaries are persisted as `archive:session:<id>` chunks (hidden from default `mem_search`). Skip reasons (`disabled`, `no llm`, `below min_chunks`, `too large`, `empty output`, `llm error`) surface in the `mem_session_end` response so operators can see why auto-summary did not fire.

## Session Trace

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_SESSION_TRACE__ENABLED` | `false` | Enable session CLI command execution tracing. |
| `MEMTOMEM_SESSION_TRACE__JSONL_ENABLED` | `true` | Enable JSONL tracing output. |
| `MEMTOMEM_SESSION_TRACE__JSONL_PATH` | `~/.memtomem/traces/session-traces.jsonl` | File path where JSONL session traces are stored. |
| `MEMTOMEM_SESSION_TRACE__LANGFUSE_ENABLED` | `false` | Enable Langfuse integration for tracing session CLI commands (requires the `langfuse` package). |
| `MEMTOMEM_SESSION_TRACE__LANGFUSE_PUBLIC_KEY` | _(empty)_ | Public key for your Langfuse project. |
| `MEMTOMEM_SESSION_TRACE__LANGFUSE_SECRET_KEY` | _(empty)_ | Secret key for your Langfuse project. |
| `MEMTOMEM_SESSION_TRACE__LANGFUSE_HOST` | _(empty)_ | Langfuse host URL (e.g. `https://cloud.langfuse.com` or self-hosted endpoint). |
| `MEMTOMEM_SESSION_TRACE__SAMPLING_RATE` | `1.0` | Sampling rate for Langfuse spans, from `0.0` (no spans) to `1.0` (send all). Note: local JSONL logging is not affected by sampling (disable output via `MEMTOMEM_SESSION_TRACE__ENABLED=false` or `MEMTOMEM_SESSION_TRACE__JSONL_ENABLED=false`). |
| `MEMTOMEM_SESSION_TRACE__PAYLOAD_MODE` | `metadata` | Payload logging mode: `metadata` (no payloads logged / None), `redacted` (replaces secret-looking fields like passwords/keys with `***` while leaving ordinary values intact), or `full` (log complete input/output). |
| `MEMTOMEM_SESSION_TRACE__MAX_PAYLOAD_CHARS` | `10000` | Maximum character length for payload properties before truncation. |

When `MEMTOMEM_SESSION_TRACE__LANGFUSE_ENABLED=true` is set, both public and secret keys must be supplied, and the `langfuse` Python package must be installed (e.g., via `pip install 'memtomem[langfuse]'` or `uv tool install 'memtomem[all]'` which includes it).

The keys may come either from the config surface above or from the Langfuse SDK's own standard variables — `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optionally `LANGFUSE_HOST`. This is the only place memtomem honours env vars without the `MEMTOMEM_` prefix, and it is credentials-only: the values are read by the Langfuse SDK itself, never copied into memtomem config, and `LANGFUSE_ENABLED` alone never turns tracing on — enabling always requires the explicit `MEMTOMEM_SESSION_TRACE__LANGFUSE_ENABLED=true` (or `config.json`) opt-in.

## Tool Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_TOOL_MODE` | `core` | Which MCP tools are exposed: `core` (9 tools), `standard` (38 incl. `mem_do`), `full` (87) |

In `core` mode, use `mem_do(action="...", params={...})` to access any of the 70+ non-core actions. Fewer tools means less context usage for AI agents.

## Web UI Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_WEB__MODE` | `prod` | Web UI surface: `prod` shows the polished page set; `dev` adds opt-in maintainer pages |
| `MEMTOMEM_WEB__HOST` | `127.0.0.1` | Bind address for `mm web` (overridden by `--host`) |
| `MEMTOMEM_WEB__PORT` | `8080` | Bind port for `mm web` (overridden by `--port`) |
| `MEMTOMEM_WEB__CSRF_ENFORCE` | `true` | CSRF protection for the Web UI's mutating endpoints; set `0`/`false`/`no` only for emergency rollback |

`mm web --mode {prod,dev}` overrides the env. `mm web --dev` is a shortcut for `--mode dev` and is mutually exclusive with `--mode`. An invalid value fails fast rather than silently falling back.

Tab classification changes over time — run `mm web --dev` to see the full surface of your installed version. Dev-only API endpoints (for example `/api/sessions`, `/api/scratch`, `POST /api/namespaces/{ns}/rename`, `DELETE /api/namespaces/{ns}`) return 404 in `prod` mode; switch to `dev` mode if you're scripting against them. The namespace list (`GET /api/namespaces`) and cosmetic metadata edit (`PATCH /api/namespaces/{ns}`) are prod-tier and respond in both modes — see ADR-0007.

## Context Gateway

Settings for the multi-project context UI (Skills, Custom Commands, and
Subagents) that ships with `mm web`. The discovery surface enumerates every
project root the gateway knows about — the server's current working
directory, any roots registered via the Web UI, and (opt-in) decoded paths
from `~/.claude/projects/`. These project roots populate `project_shared`
and `project_local` tier entries; the `user` tier (per ADR-0011 §1) is a
separate, always-visible axis whose canonical lives under
`~/.memtomem/<artifact>/`.

> For a task-first walkthrough of the Store → Sync → Runtime model, see the
> [Context Gateway](context-gateway.md) guide. This section is the
> environment-variable reference.

When an artifact row is `Not yet imported`, the Web Context Gateway shows a
scope-aware remediation block. `project_shared` can be bootstrapped with the
web Import action or with `mm context init --include=agents,commands,skills
--scope project_shared --confirm-project-shared` followed by `mm context sync
--include=agents,commands,skills --scope project_shared`. The `user` tier is
read-only in the Web UI, so use the matching `--scope user` CLI flow. The
`project_local` tier is a gitignored draft tier; use `--scope project_local`
to seed drafts, and expect sync to report the no-runtime-fan-out skip.

All writes into the git-tracked `.memtomem/` tree — sync fan-out, `mm context
install`/`update` from the wiki, version create, and the web hook-rule
promote — pass the ADR-0011 §5 privacy gate first: a detected secret
hard-refuses the write with no bypass flag, because git history cannot be
retracted.

For multi-device sync, treat project-shared Context Gateway files as part of
the project repo: commit `<project>/.memtomem/context.md`,
`<project>/.memtomem/{agents,skills,commands}/`, and
`<project>/.memtomem/settings.json` when you want them shared. Do not sync
`~/.memtomem/known_projects.json`; it is the Web UI's per-machine Add Project
registry and stores local absolute paths.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_CONTEXT_GATEWAY__KNOWN_PROJECTS_PATH` | `~/.memtomem/known_projects.json` | Where the Web UI persists "Add Project" registrations. The Sources, Skills, Custom Commands, and Subagents tabs render one collapsible group per registered project root. |
| `MEMTOMEM_CONTEXT_GATEWAY__EXPERIMENTAL_CLAUDE_PROJECTS_SCAN` | `false` | When `true`, the gateway also reverse-decodes `~/.claude/projects/<encoded>` directory names into project roots and surfaces them as discovered roots. Off by default — the encoding is fragile around dash-containing paths, so this stays gated behind explicit consent. |
| `MEMTOMEM_CONTEXT_GATEWAY__AUTO_DISPLAY_CONFIGURED_PROJECTS` | `true` | Auto-surface `~/.claude/projects/` scan candidates that already carry a runtime marker, without an explicit Add Project step |

## Hooks

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_HOOKS__TARGET_SCOPE` | `user` | Tier that `mm context` writes memtomem-managed Claude Code hook rules into: `user` (`~/.claude/settings.json`), `project_shared` (`<project>/.claude/settings.json`), or `project_local` (`<project>/.claude/settings.local.json`). See ADR-0010 §3. |

## Advanced / operator environment variables

These are read directly from the process environment (not the layered
`config.json` / `config.d/` sources) and are rarely changed. They still
carry the `MEMTOMEM_` prefix.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_LOG_LEVEL` | `INFO` | `memtomem-server` log level (`DEBUG`, `INFO`, `WARNING`, ...) |
| `MEMTOMEM_LOG_FORMAT` | `text` | `memtomem-server` log format: `text` or `json` |
| `MEMTOMEM_WIKI_PATH` | `~/.memtomem-wiki` | Override the wiki store location (ADR-0008) |
| `MEMTOMEM_FASTEMBED_CACHE` | _(platform cache dir)_ | Override the ONNX / `fastembed` model cache directory |
| `MEMTOMEM_INDEX_DEBOUNCE_QUEUE` | _(state dir)_ | Override the file-watcher debounce queue file path |

## Querying and Modifying at Runtime

You can also inspect and change settings at runtime via the `mem_config` MCP tool (requires `MEMTOMEM_TOOL_MODE=full`; in `core` or `standard` mode, use `mm config` CLI or the Web UI Settings tab):

```
mem_config()                                      # Output all settings as JSON
mem_config(key="search.default_top_k")            # Query a single value
mem_config(key="search.default_top_k", value="20")  # Change and persist
```
