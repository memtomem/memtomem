# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is memtomem?

Markdown-first, long-term memory infrastructure for AI agents. Provides hybrid BM25 + semantic search across indexed markdown/JSON/YAML/code files via MCP (Model Context Protocol).

## Build & Development Commands

```bash
# Install (uv workspace — Python 3.12+)
uv pip install -e "packages/memtomem[dev]"

# Run all tests (pytest + pytest-asyncio, async tests auto-detected)
uv run pytest                      # 1101 tests (core 819 + STM 282)

# Run core tests only
uv run pytest packages/memtomem/tests/ -v

# Run STM tests only
uv run pytest packages/memtomem-stm/tests/ -v

# Run a single test file
uv run pytest packages/memtomem/tests/test_search.py -v

# Run a single test by name
uv run pytest packages/memtomem/tests/test_search.py::test_bm25_search -xvs

# Skip tests requiring a running Ollama instance
uv run pytest -m "not ollama"

# Lint and format (ruff, line-length=100, target py312)
uv run ruff check packages/memtomem/src --fix
uv run ruff format packages/memtomem/src
uv run ruff check packages/memtomem-stm/src --fix
uv run ruff format packages/memtomem-stm/src

# Type check
uv run mypy packages/memtomem/src

# Run MCP server
uv run memtomem-server

# Run CLI
uv run memtomem search "query"    # or: mm search "query"

# Run web UI
uv run memtomem-web               # http://localhost:8080
```

## Architecture

**Monorepo** managed by uv workspace with two Python packages and two plugins:

- `packages/memtomem/` — Core: MCP server, CLI, web UI, search, storage, indexing
- `packages/memtomem-stm/` — STM proxy gateway for proactive memory surfacing
- `packages/memtomem-claude-plugin/` — Claude Code plugin (experimental, not yet published)
- `packages/memtomem-openclaw-plugin/` — OpenClaw plugin (experimental, not yet published)

### Dependency injection: AppContext

All services live in `AppContext` (dataclass in `server/context.py`). Every MCP tool receives `ctx: CtxType` and calls `_get_app(ctx)` to access config, storage, embedder, index engine, search pipeline, and file watcher. The lifespan (`server/lifespan.py`) initializes all services at startup.

### MCP tools

65 tools registered via `@register` decorator (in `server/tool_registry.py`) in `server/tools/*.py`, imported in `server/__init__.py`. Each tool is wrapped with `@tool_handler` for error handling. Tool visibility is controlled by `MEMTOMEM_TOOL_MODE` env var (`core`=9 tools including `mem_do`, `standard`=~30 + `mem_do`, `full`=65 + `mem_do`). Default mode is `core`. The `mem_do` meta-tool routes to 61 non-core actions via `mem_do(action="...", params={...})`. Action aliases (e.g. `health_report` → `eval`) are supported for discoverability.

### Storage: SQLite + FTS5 + sqlite-vec

`SqliteBackend` in `storage/sqlite_backend.py` combines multiple mixins (Session, Scratch, Relation, Analytic, History, Entity, Policy) for different domains. Uses a read pool (3 read-only connections) + write lock. Vector search via `sqlite-vec` extension with F32 serialization.

### Search pipeline

`search/pipeline.py` runs a multi-stage pipeline:
1. Query expansion (tags/headings)
2. Parallel BM25 (FTS5) + dense (sqlite-vec cosine) retrieval
3. RRF (Reciprocal Rank Fusion) merging
4. Optional time-decay scoring
5. Optional cross-encoder reranking
6. MMR diversification

Results cached with 30s TTL.

### Chunking

`chunking/` module with specialized chunkers: markdown (heading-aware sections), Python (AST-based), JS/TS (tree-sitter), structured data (JSON/YAML/TOML). Registry pattern in `chunking/registry.py`. Incremental re-indexing via SHA-256 content hashing — only changed chunks get re-embedded.

### Embedding providers

`embedding/` supports Ollama (local, default `nomic-embed-text` 768-dim) and OpenAI (cloud). Batch processing with configurable batch size and concurrency.

### Configuration

All config via `MEMTOMEM_` prefixed env vars with `__` nesting (e.g., `MEMTOMEM_EMBEDDING__PROVIDER=openai`). Pydantic-settings classes in `config.py`.

### STM proxy gateway

`packages/memtomem-stm/` is a separate uv workspace package that proxies upstream MCP servers with a 4-stage pipeline:

1. **CLEAN** — `proxy/cleaning.py`: HTML/script/style stripping, paragraph dedup, link flood collapse (supports links with trailing descriptions). `DefaultContentCleaner` accepts `CleaningConfig` in constructor.
2. **COMPRESS** — `proxy/compression.py`: 6 strategies (none/truncate/selective/hybrid/extract_fields/LLM) + `auto_select_strategy()` for content-type detection. `TruncateCompressor` is section-aware for markdown (cuts at heading boundaries, preserves Summary/Conclusion sections at document end). `FieldExtractCompressor` shows first key-value pairs of nested dicts. `SelectiveCompressor` stores pending sections in deque-backed storage.
3. **SURFACE** — `surfacing/engine.py`: proactive memory injection from LTM. Gated by `RelevanceGate` (rate limit, cooldown, write-tool heuristic), protected by `CircuitBreaker`, session dedup (same memory not shown twice), and `max_injection_chars` size cap. File paths are tokenized for query extraction.
4. **INDEX** — optional auto-indexing of large responses to LTM.

Key patterns:
- `STMContext` dataclass for dependency injection (parallel to core `AppContext`)
- `ToolConfig` frozen dataclass returned by `ProxyManager._resolve_tool_config()` (per-tool compression/indexing settings)
- Unified `CircuitBreaker` in `utils/circuit_breaker.py` — used by both surfacing engine and LLM compressor
- `ProxyCache` stores pre-surfacing content; surfacing re-applied on cache hit to keep memories fresh
- `AutoTuner` adjusts per-tool `min_score` based on feedback ratios (>60% not_relevant → raise, <20% → lower), with global ratio fallback for cold-start tools
- Feedback-driven search boost: "helpful" ratings increment `access_count` (once per surfacing event), feeding into core's access-frequency boost
- `LLMCompressor` reuses `httpx.AsyncClient` for connection pooling

## Testing

- Framework: pytest + pytest-asyncio (asyncio_mode = "auto")
- Core test root: `packages/memtomem/tests/` (819 tests)
- STM test root: `packages/memtomem-stm/tests/` (282 tests)
- Both paths configured in `pyproject.toml` `testpaths`
- Core fixtures in `conftest.py` create isolated SQLite DB per test
- STM fixtures in `conftest.py` provide `surfacing_config`, `feedback_store`, `proxy_cache`, `token_tracker`
- Marker `@pytest.mark.ollama` for tests requiring a running Ollama instance (auto-skipped if unavailable)

## Adding new MCP tools

1. Create module in `server/tools/`
2. Implement async function with `@register` decorator (from `server/tool_registry.py`) and `@tool_handler`
3. Import in `server/__init__.py`
4. Add to appropriate tool mode set (`_CORE_TOOLS`, `_STANDARD_TOOLS`, or full by default)

The `@register` decorator in `server/tool_registry.py` replaces direct `@mcp.tool()` usage. The meta-tool implementation lives in `server/tools/meta.py`.
