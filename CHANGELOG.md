# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

### Core
- **Context-window search (small-to-big retrieval)**: `search(context_window=N)` attaches ±N adjacent chunks via batch `list_chunks_by_sources()`. Per-call override or global via `ContextWindowConfig`
- **`mem_expand` action**: targeted expansion of individual search results without re-running search — `mem_do(action="expand", params={"chunk_id": "...", "window": 2})`
- Search result formatting: `[chunk M/T]` position + before/after sections in `_format_single_result()`
- Web API: `context_window` query parameter on search endpoint
- E2E integration tests: 9 tests verifying full pipeline with real Ollama (index → search → context-window → format)

### STM Proxy
- **Query-aware adaptive compression**: `_context_query` drives section budget allocation via `RelevanceScorer` protocol instead of fixed top-down order
  - `BM25Scorer` (default): zero-latency, heading 3× weighting, suffix stemming
  - `EmbeddingScorer` (opt-in): sync httpx to Ollama/OpenAI, automatic BM25 fallback on error
  - Switching strategy (not blending): embedding when configured, else BM25
  - `RelevanceScorerConfig` in `ProxyConfig` for scorer type and embedding settings
  - Benchmark validated: 83% QA recovery vs 0% baseline at tight budgets (20%, 10 sections)
  - Vocabulary mismatch confirmed: BM25 fails cross-language, embedding solves it
- **HybridCompressor query-aware**: tail section compression now receives `context_query`
- **Horizontal scaling**: `PendingStore` protocol — `InMemoryPendingStore` (default) + `SQLitePendingStore` (WAL mode, thread-safe) for multi-instance `SelectiveCompressor`
- **Gateway production improvements** (4 phases):
  - Error classification: `ErrorCategory` (TRANSPORT/TIMEOUT/PROTOCOL/UPSTREAM/PROGRAMMING), smart retry
  - Tool metadata optimization: `hidden`, `description_override`, `max_description_chars`, `strip_schema_descriptions`
  - Context-window-aware compression: `consumer_model` + `context_budget_ratio` for model-based budget
  - Observability: `RPSTracker`, `trace_id`, latency percentiles (p50/p95/p99)
- **Gateway audit fixes**: dual retention, token counting, error path tests, cross-session surfacing dedup (SQLite), stress/concurrency tests, catastrophic backtracking fix
- **Compression pipeline**: AUTO strategy, section-distributed truncation, code-aware truncation, JSON key-distributed, tail anomaly detection, dynamic `min_retention` by content length
- **Benchmark framework**: 35 tasks, `RuleBasedJudge` (keyword + fuzzy QA + source analysis), `LLMJudge`, bootstrap CI, Wilcoxon test, strategy matrix, compression curves
- Surfacing: `context_window_size` wired to LTM search, `max_injection_chars` raised to 3000

### Docs
- Standalone test guide rewritten: 10 use cases, beginner-friendly, detailed config values
- Per-client MCP setup guides: Claude Code, Gemini CLI, Google Antigravity, Cursor
- CLAUDE.md updated with all new features

### Bug Fixes
- Section minimum extraction lost headings when match position included leading newline
- `SchemaPruningCompressor` referenced undefined variable `t` — fixed to `TruncateCompressor()`
- `GENERIC_RE` catastrophic backtracking in metrics

### Testing
- 1543 automated tests (core 846 + STM 697)
- New: context-window (18), pending store (17), query-aware (22), E2E pipeline (9), bench framework (180+)

---

## [0.1.0] — 2026-04-04

Initial release.

### Core
- MCP server (`memtomem-server`) with 65 tools + `mem_do` meta-tool
- CLI (`memtomem` / `mm`) with subcommands: `init`, `search`, `add`, `recall`, `index`, `config`, `context`, `embedding-reset`, `stm`, `shell`, `web`
- Interactive setup wizard (`mm init`) — 7-step with back/cancel navigation (b/q)
- STM proxy setup wizard (`mm stm init`) — auto-detects MCP clients and configures upstream servers
- STM proxy reset (`mm stm reset`) — disables STM and restores original MCP configs
- `-h` shortcut for help on all CLI commands
- Web UI (`memtomem-web`) — full SPA dashboard with search, sources, tags, sessions, health report
- Hybrid search pipeline: BM25 (FTS5) + Dense (sqlite-vec) + RRF fusion
- Multi-stage pipeline: query expansion → parallel retrieval → RRF → decay → reranking → MMR → access boost
- Configurable search cache TTL (`MEMTOMEM_SEARCH__CACHE_TTL`)

### Storage
- SQLite with FTS5, sqlite-vec, WAL mode, read pool (3 connections)
- Mixin architecture: SessionMixin, ScratchMixin, RelationMixin, AnalyticsMixin, HistoryMixin, EntityMixin, PolicyMixin
- 13 tables: chunks, chunks_fts, chunks_vec, sessions, session_events, working_memory, chunk_relations, chunk_entities, memory_policies, access_log, query_history, namespace_metadata, _memtomem_meta

### Indexing
- Semantic chunking: Markdown (heading-aware with frontmatter tag extraction, wikilink resolution, heading-boundary merge prevention), Python (AST), JS/TS (tree-sitter), JSON/YAML/TOML
- Incremental indexing with SHA-256 content hashing (only changed chunks re-embedded)
- Auto-tag option on indexing (`auto_tag` parameter)
- File watcher with debounced batch reindexing (concurrent via asyncio.gather)
- Embedding providers: Ollama (local) and OpenAI (cloud) with concurrent batching
- `bge-m3` recommended for multilingual use (KR/EN/JP/CN cross-language search)

### Tool System
- `mem_do` meta-tool routes to 61 actions across 14 categories (with aliases for discoverability)
- Tool modes: `core` (9 tools, default), `standard` (~30), `full` (65)
- `@register` decorator in `server/tool_registry.py` for action registration
- `mem_do(action="help")` returns full action catalog with parameter docs

### Agent Memory Features
- Episodic memory: sessions with event tracking
- Working memory: scratchpad with TTL, session binding, promotion to long-term
- Procedural memory: save and list reusable workflows
- Multi-agent: agent-scoped namespaces with shared knowledge
- Cross-references: bidirectional chunk relations
- Entity extraction: people, dates, decisions, tech
- Memory policies: auto-archive, auto-expire, auto-tag
- Consolidation and reflection (Stanford Generative Agents pattern)

### Plugins
- Plugin architecture for Claude Code and OpenClaw (experimental, not yet published)

### Integrations
- LangGraph adapter: `MemtomemStore` for direct Python integration
- STM proxy gateway: proactive memory surfacing with compression pipeline

### STM Proxy (memtomem-stm)
- 4-stage pipeline: CLEAN → COMPRESS → SURFACE → INDEX
- 6 compression strategies + `auto_select_strategy()` content-type detection
- Section-aware truncation for markdown (cuts at heading boundaries, lists remaining sections)
- `FieldExtractCompressor` shows first key-value pairs of nested dicts
- Proactive memory surfacing with context extraction, path tokenization, relevance gating
- Session dedup (same memory not shown twice), injection size cap (`max_injection_chars`)
- Feedback-driven search boost: "helpful" ratings increment `access_count` (once per surfacing event)
- AutoTuner with cold-start global fallback for tools with insufficient feedback samples
- Response caching (pre-surfacing content cached, surfacing re-applied on hit)
- Unified CircuitBreaker (closed/open/half-open) for surfacing and LLM compression
- Retry with exponential backoff, error type filtering (transport errors only)
- Privacy-aware content scanning; `<script>`/`<style>` blocks fully stripped
- 5 MCP tools: stats, select_chunks, cache_clear, surfacing_feedback, surfacing_stats
- CLI: `memtomem-stm-proxy` (status/list/add/remove)
- Optional Langfuse tracing

### Security
- XSS: DOMPurify sanitization
- SSRF: private IP/internal host blocking in URL fetcher
- Path traversal: source validation, symlink rejection
- SQL injection: all queries parameterized

### Testing
- 1101 automated tests (pytest + pytest-asyncio), Ollama-dependent tests auto-skipped
- Core: 819 tests — storage, search, chunking, sessions, scratch, entities, policies, analytics, meta-tool, SSRF, webhooks, config, usability fixes, user workflows, server tools (core/org/advanced), search stages (RRF/MMR), chunkers (Python/JS/structured), web routes, server helpers, tools logic (entity extraction/policy engine/temporal), storage extended, CLI, indexing engine, embedding providers
- STM: 282 tests — circuit breaker, compression (6 strategies), relevance gate, context extractor, feedback/auto-tuner, proxy cache, cleaning, surfacing cache, surfacing engine, formatter, proxy manager, config persistence, integration, effectiveness, information loss, STM remaining (fastmcp compat/tracing/metrics/mcp client)

### Bug Fixes
- `mm stm init` wizard crash on cache TTL input (`type=int` not converted to Click type)
- Ollama-dependent tests now auto-skipped via `@pytest.mark.ollama` + `pytest_collection_modifyitems` hook
- Link flood detection regex missed markdown links with trailing descriptions (`- [text](url) — desc`)
- `TruncateCompressor` now preserves Summary/Conclusion sections at document end
