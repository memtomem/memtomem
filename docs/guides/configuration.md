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
| `MEMTOMEM_EMBEDDING__PROVIDER` | `ollama` | `ollama` (local) or `openai` (cloud) |
| `MEMTOMEM_EMBEDDING__MODEL` | `nomic-embed-text` | Embedding model name |
| `MEMTOMEM_EMBEDDING__DIMENSION` | `768` | Vector dimension (must match the model) |
| `MEMTOMEM_EMBEDDING__BASE_URL` | `http://localhost:11434` | API endpoint URL |
| `MEMTOMEM_EMBEDDING__API_KEY` | _(empty)_ | API key (required for OpenAI) |
| `MEMTOMEM_EMBEDDING__BATCH_SIZE` | `64` | Texts per embedding API call |
| `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` | `4` | Max parallel embedding requests |

See [Embedding Providers](embeddings.md) for the supported model list and the dimension values you must use with each one.

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

## Context Window

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_CONTEXT_WINDOW__ENABLED` | `false` | Enable context expansion for all searches |
| `MEMTOMEM_CONTEXT_WINDOW__WINDOW_SIZE` | `2` | Number of adjacent chunks (±N) to include |

When enabled, search results include surrounding chunks from the same source file. Also available per-call via `mem_search(context_window=N)` or `mem_do(action="expand", params={"chunk_id": "...", "window": 2})`.

## Indexing

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_INDEXING__MEMORY_DIRS` | `["~/.memtomem/memories"]` | Directories to index |
| `MEMTOMEM_INDEXING__MAX_CHUNK_TOKENS` | `512` | Maximum tokens per chunk |
| `MEMTOMEM_INDEXING__MIN_CHUNK_TOKENS` | `128` | Merge threshold for short chunks |
| `MEMTOMEM_INDEXING__CHUNK_OVERLAP_TOKENS` | `0` | Token overlap between adjacent chunks |
| `MEMTOMEM_INDEXING__STRUCTURED_CHUNK_MODE` | `original` | JSON/YAML/TOML chunking: `original` or `recursive` |

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

## Tool Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_TOOL_MODE` | `core` | Which MCP tools are exposed: `core` (9 tools), `standard` (~32 + `mem_do`), `full` (72) |

In `core` mode, use `mem_do(action="...", params={...})` to access any of the 63 non-core actions. Fewer tools means less context usage for AI agents.

## Querying and Modifying at Runtime

You can also inspect and change settings at runtime via the `mem_config` MCP tool:

```
mem_config()                                      # Output all settings as JSON
mem_config(key="search.default_top_k")            # Query a single value
mem_config(key="search.default_top_k", value="20")  # Change and persist
```
