# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [0.1.0] — 2026-04-08

Initial open-source release.

### Core (memtomem)
- MCP server with 66 tools + `mem_do` meta-tool (61 actions, aliases)
- CLI (`memtomem` / `mm`): init, search, add, recall, index, config, context, stm, shell, web
- Web UI dashboard: search, sources, tags, sessions, health report
- Hybrid search pipeline: BM25 (FTS5) + dense vectors (sqlite-vec) + RRF fusion
- Multi-stage pipeline: query expansion → parallel retrieval → RRF → time-decay → reranking → MMR → access boost → context-window expansion
- Context-window search (small-to-big retrieval): `search(context_window=N)` + `mem_expand` action
- Tool modes: `core` (9 tools), `standard` (~30), `full` (66)

### Storage
- SQLite with FTS5, sqlite-vec, WAL mode, read pool (3 connections)
- Mixin architecture: Session, Scratch, Relation, Analytic, History, Entity, Policy
- Incremental indexing with SHA-256 content hashing

### Chunking
- Markdown: heading-aware sections with frontmatter/wikilink support
- Python: AST-based splitting at function/class boundaries
- JavaScript/TypeScript: tree-sitter parsing
- JSON/YAML/TOML: structure-aware splitting

### Embedding
- Ollama (local, default `nomic-embed-text` 768-dim)
- OpenAI (cloud)
- `bge-m3` recommended for multilingual (KR/EN/JP/CN)

### Agent Memory
- Episodic (sessions), working (scratchpad with TTL), procedural (workflows)
- Multi-agent namespaces, cross-references, entity extraction
- Memory policies (auto-archive/expire/tag), consolidation/reflection

### STM Proxy (memtomem-stm)
- 4-stage pipeline: CLEAN → COMPRESS → SURFACE → INDEX
- 9 compression strategies + auto-selection by content type
- Query-aware adaptive compression via `RelevanceScorer` (BM25 / Embedding)
- Proactive memory surfacing with relevance gating, session/cross-session dedup
- Automatic fact extraction from tool responses
- Feedback-driven search boost, AutoTuner with cold-start fallback
- CircuitBreaker, retry with backoff, response caching
- Context-window-aware compression (`consumer_model` + `context_budget_ratio`)
- Tool metadata optimization, error classification, observability (RPS, trace_id, percentiles)
- Horizontal scaling: `PendingStore` protocol (InMemory / SQLite)

### Integrations
- LangGraph adapter (`MemtomemStore`)
- Claude Code plugin (experimental)
- OpenClaw plugin (experimental)

### Security
- XSS: DOMPurify sanitization
- SSRF: private IP/internal host blocking
- Path traversal: source validation, symlink rejection
- SQL injection: all queries parameterized

### Testing
- 1519 automated tests (core 846 + STM 673)
- CI: GitHub Actions (lint, typecheck, test)
