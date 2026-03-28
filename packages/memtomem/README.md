# memtomem

Markdown-based long-term memory infrastructure for AI agents. Fast and accurate with hybrid search.

**Core philosophy**: `.md` files are the source of truth and the vector DB is a derived cache.
Manage memories as plain text files — memtomem makes them instantly searchable.

## Key Features

- **Hybrid search**: BM25 (keyword) + dense vector + RRF fusion — exact terms via keyword, meaning via semantic
- **Semantic chunking**: heading-aware Markdown, structured data (JSON/YAML/TOML), code (Python/JS/TS)
- **Incremental indexing**: chunk-level SHA-256 diff — re-embed only changed chunks, transaction atomicity guaranteed
- **Chunk tuning**: min/max token bounds, overlap, two structured chunking modes (original / recursive)
- **Namespace**: organize memories into scoped groups, auto-derive from folder names
- **Dedup & decay**: near-duplicate detection with merge, time-based score decay and TTL expiration
- **Export / import**: JSON bundle backup and restore with re-embedding
- **Auto-tagging**: keyword-based tag extraction for untagged chunks
- **MMR**: Maximal Marginal Relevance for result diversification
- **File watcher**: auto re-indexing when files change
- **Web UI**: full-featured SPA dashboard (search, sources, indexing, tags, settings)
- **MCP server**: supports all MCP-compatible clients including Claude Code, Cursor, Windsurf, Claude Desktop
- **CLI**: terminal commands for search, indexing, and memory management

---

## Quick Start

### MCP Server (for AI clients)

No installation needed — `uvx` handles everything automatically.

```bash
ollama pull nomic-embed-text
```

Add to your MCP client config (`.mcp.json`):

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "/path/to/your/notes"
      }
    }
  }
}
```

Or for Claude Code:

```bash
# PyPI
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Then in your MCP client:

```
mem_index("/path/to/notes")
mem_search("deployment checklist")
```

### CLI (for terminal, optional)

Install only if you want to use memtomem from the terminal. Not required for MCP server usage.

```bash
# PyPI
uv tool install memtomem    # or: pipx install memtomem
# Source (if running from git clone): uv run mm ...

ollama pull nomic-embed-text
```

### Web UI

```bash
# PyPI
uv tool install memtomem[web]
# Source (if running from git clone): uv run memtomem-web

memtomem-web                 # opens http://localhost:8080
```

---

## MCP Tool Reference

### Core Tools (11)

| Tool | Description |
|------|-------------|
| `mem_search` | BM25 + semantic hybrid search with filters |
| `mem_recall` | Retrieve memories by date range (newest first) |
| `mem_stats` | Total chunks, sources, storage backend statistics |
| `mem_status` | Index statistics and current configuration summary |
| `mem_index` | Index file or directory / re-index |
| `mem_add` | Add new entry to markdown file and index immediately |
| `mem_batch_add` | Add multiple entries at once via KV batch |
| `mem_edit` | Replace lines in chunk's source file and re-index |
| `mem_delete` | Delete chunk, source file chunks, or namespace chunks |
| `mem_config` | Query / modify runtime settings |
| `mem_embedding_reset` | Check/resolve embedding mismatch (status/apply_current/revert_to_stored) |

### Namespace Tools (6)

| Tool | Description |
|------|-------------|
| `mem_ns_list` | List all namespaces and their chunk counts |
| `mem_ns_set` | Set session-default namespace for subsequent operations |
| `mem_ns_get` | Get current session namespace |
| `mem_ns_update` | Update namespace description and/or color |
| `mem_ns_rename` | Rename a namespace (SQL update, no re-indexing) |
| `mem_ns_delete` | Delete all chunks in a namespace from the index |

### Maintenance Tools (5)

| Tool | Description |
|------|-------------|
| `mem_dedup_scan` | Scan for near-duplicate chunk candidates (dry-run) |
| `mem_dedup_merge` | Merge duplicates: keep one, delete others, merge tags |
| `mem_decay_scan` | Preview chunks that would be expired by TTL |
| `mem_decay_expire` | Delete chunks older than max_age_days (default dry_run=True) |
| `mem_auto_tag` | Extract and apply keyword-based tags to chunks |

### Data Tools (2)

| Tool | Description |
|------|-------------|
| `mem_export` | Export chunks to JSON bundle with filters |
| `mem_import` | Import chunks from JSON bundle with re-embedding |

---

## Key Tool Usage Examples

### mem_search

```
mem_search(query, top_k=10, source_filter=None, tag_filter=None, namespace=None)
```

- `source_filter`: filter by source file path — **substring recommended** (e.g., `"docs/adr"`, `".py"`). Glob patterns (`*`, `?`) match the full absolute path via `fnmatch`
- `tag_filter`: comma-separated tags — matches chunks with ANY listed tag (OR logic)
- `namespace`: scope search to a specific namespace

### mem_recall

Retrieve memories by date range. Useful when finding memories by "when they were recorded" without a query.

```
mem_recall(since=None, until=None, source_filter=None, namespace=None, limit=20)
```

- `since` / `until`: `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or full ISO datetime
  - `until` operates as an **exclusive** upper bound
- `source_filter`: filter by source file path — **substring recommended** (e.g., `"docs/adr"`, `".py"`). Glob patterns (`*`, `?`) match the full absolute path via `fnmatch`
- `namespace`: single value, comma-separated, or glob pattern (e.g. `"project:*"`)

```
# Retrieve memories recorded from January to March 2025
mem_recall(since="2025-01", until="2025-04")

# Last 20 from notes files since today
mem_recall(since="2026-03-01", source_filter="notes")

# Most recent 10 without filters
mem_recall(limit=10)

# Recall from all project namespaces
mem_recall(namespace="project:*")
```

### mem_add

```
mem_add(content, title=None, tags=[], file=None, namespace=None)
```

- `file`: relative path from first `memory_dir` or absolute path
- When `file` is omitted, auto-creates `YYYY-MM-DD.md` file in first memory directory

### mem_edit / mem_delete

Use chunk UUID shown in `mem_search` results:

```
mem_edit(chunk_id="<uuid>", new_content="updated content")
mem_delete(chunk_id="<uuid>")
mem_delete(source_file="/path/to/notes.md")
mem_delete(namespace="old-project")
```

### mem_config

```
mem_config()                                       # Output all settings as JSON
mem_config(key="search.default_top_k")             # Query value
mem_config(key="search.default_top_k", value="20") # Change value (persisted)
```

### mem_embedding_reset

```
mem_embedding_reset()                              # Compare DB vs config (default: status)
mem_embedding_reset(mode="apply_current")          # Reset DB to current config (vectors deleted, re-indexing needed)
mem_embedding_reset(mode="revert_to_stored")       # Switch runtime embedder to DB values (non-destructive)
```

### mem_ns_set / mem_ns_list

```
mem_ns_set(namespace="work")                       # Set session default
mem_ns_list()                                      # Show all namespaces with counts
mem_ns_rename(old="project:v1", new="project:v2")  # Rename namespace
```

### mem_dedup_scan / mem_dedup_merge

```
mem_dedup_scan(threshold=0.92, limit=50)           # Find near-duplicate pairs
mem_dedup_merge(keep_id="<uuid>", delete_ids=["<uuid1>", "<uuid2>"])
```

### mem_export / mem_import

```
mem_export(output_file="~/backup.json", namespace="work")
mem_import(input_file="~/backup.json", namespace="imported")
```

### mem_auto_tag

```
mem_auto_tag(max_tags=5, dry_run=True)             # Preview tags
mem_auto_tag(source_filter="notes", overwrite=False)
```

---

## CLI Usage

`mm` is a shorthand alias for `memtomem`. Both work identically.

```bash
mm init                          # set up memtomem with interactive wizard
mm search "deployment"           # search from terminal
mm index ~/notes                 # index markdown files
mm add "some note" --tags "tag1" # add a memory entry
mm recall --since 2026-03-01     # recall recent memories
mm config                        # view/modify configuration
mm web                           # launch web UI
```

---

## Environment Variables

All variables use the `MEMTOMEM_` prefix, with nested sections separated by `__`.

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_STORAGE__BACKEND` | `sqlite` | Storage backend |
| `MEMTOMEM_STORAGE__SQLITE_PATH` | `~/.memtomem/memtomem.db` | SQLite database path |
| `MEMTOMEM_STORAGE__COLLECTION_NAME` | `memories` | Collection name |

### Embedding

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_EMBEDDING__PROVIDER` | `ollama` | `ollama` (local) or `openai` (cloud) |
| `MEMTOMEM_EMBEDDING__MODEL` | `nomic-embed-text` | Embedding model name |
| `MEMTOMEM_EMBEDDING__DIMENSION` | `768` | Vector dimension (must match model) |
| `MEMTOMEM_EMBEDDING__BASE_URL` | `http://localhost:11434` | API endpoint URL |
| `MEMTOMEM_EMBEDDING__API_KEY` | _(empty)_ | API key (required for OpenAI) |
| `MEMTOMEM_EMBEDDING__BATCH_SIZE` | `64` | Texts per embedding API call |
| `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` | `4` | Max parallel embedding requests |

### Search

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_SEARCH__DEFAULT_TOP_K` | `10` | Default number of search results |
| `MEMTOMEM_SEARCH__BM25_CANDIDATES` | `50` | BM25 pre-filter candidate count |
| `MEMTOMEM_SEARCH__DENSE_CANDIDATES` | `50` | Dense vector pre-filter candidate count |
| `MEMTOMEM_SEARCH__RRF_K` | `60` | RRF fusion smoothing constant |
| `MEMTOMEM_SEARCH__ENABLE_BM25` | `true` | Enable keyword (FTS5) retriever |
| `MEMTOMEM_SEARCH__ENABLE_DENSE` | `true` | Enable semantic vector retriever |
| `MEMTOMEM_SEARCH__RRF_WEIGHTS` | `[1.0, 1.0]` | RRF weights for [BM25, Dense] — adjust to favor one retriever |
| `MEMTOMEM_SEARCH__TOKENIZER` | `unicode61` | FTS tokenizer (`unicode61` or `kiwipiepy`) |

### Indexing

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_INDEXING__MEMORY_DIRS` | `["~/.memtomem/memories"]` | Directories to index |
| `MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS` | `512` | Maximum tokens per chunk |
| `MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS` | `128` | Merge threshold for short chunks |
| `MEMTOMEM_INDEXING__CHUNK_OVERLAP_TOKENS` | `0` | Token overlap between adjacent chunks |
| `MEMTOMEM_INDEXING__STRUCTURED_CHUNK_MODE` | `original` | JSON/YAML/TOML chunking: `original` or `recursive` |

### Decay

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_DECAY__ENABLED` | `false` | Enable time-based score decay |
| `MEMTOMEM_DECAY__HALF_LIFE_DAYS` | `30.0` | Days until decay factor = 0.5 (float) |

### MMR

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_MMR__ENABLED` | `false` | Enable result diversification |
| `MEMTOMEM_MMR__LAMBDA_PARAM` | `0.7` | 0.0 = max diversity, 1.0 = pure relevance |

### Namespace

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_NAMESPACE__DEFAULT_NAMESPACE` | `default` | Default namespace for new chunks |
| `MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS` | `false` | Auto-derive namespace from folder name |

---

## Embedding Providers

Embedding model and dimension must always be set together. Dimension is **not auto-detected** — mismatched values will cause indexing errors.

| Model | Provider | Dimension | Setup |
|-------|----------|-----------|-------|
| `nomic-embed-text` (default) | ollama | 768 | `ollama pull nomic-embed-text` |
| `bge-m3` | ollama | 1024 | `ollama pull bge-m3` |
| `text-embedding-3-small` | openai | 1536 | Set `API_KEY` |
| `text-embedding-3-large` | openai | 3072 | Set `API_KEY` |

### Ollama (default, local)

```bash
ollama pull nomic-embed-text

# These are the defaults — no config needed
MEMTOMEM_EMBEDDING__PROVIDER=ollama
MEMTOMEM_EMBEDDING__MODEL=nomic-embed-text
MEMTOMEM_EMBEDDING__DIMENSION=768
```

### OpenAI (cloud)

```bash
MEMTOMEM_EMBEDDING__PROVIDER=openai
MEMTOMEM_EMBEDDING__MODEL=text-embedding-3-small
MEMTOMEM_EMBEDDING__DIMENSION=1536
MEMTOMEM_EMBEDDING__API_KEY=sk-...
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) for details.
