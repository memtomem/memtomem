# memtomem

> 🚧 **Alpha** — APIs, defaults, and on-disk config surfaces may still change between `0.x` releases. Feedback and issue reports are especially welcome at [github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

Markdown-first long-term memory infrastructure for AI agents. Core usage is hook-free by default: your files remain the source of truth, and memory changes happen only when you or your agent explicitly call memtomem. Optional client hooks are separate, visible integrations.

**Core philosophy**: `.md` files are the source of truth and the vector database is a derived cache. Manage memories as plain text files — memtomem makes them instantly searchable.

**Built for:**
- AI agents (Claude Code, Cursor, Windsurf, Claude Desktop, Kimi CLI) that need to *remember* between sessions
- Developers who want a searchable knowledge base built from their existing markdown notes — no proprietary database, no vendor lock-in
- Multilingual content (English, Korean, Japanese, Chinese) via `bge-m3` embeddings

## Quick Start

```bash
# 1. Install memtomem with all features (Python 3.12+)
uv tool install 'memtomem[all]'  # or: pipx install 'memtomem[all]'
mm --version

# 2. Configure storage, search, and optional MCP registration
mm init

# 3. Verify a complete memory round trip
mm status
mm add "Deployment checklist uses blue-green rollout" --tags ops
mm search "blue-green"
```

The search should return the sentence you just added. `mm add` writes to your configured user memory directory and indexes the entry immediately, so this path works without an existing notes directory or a connected editor.

Choose **Minimal** in the setup picker for a no-model-download first proof;
rerun `mm init` later to add semantic search.

To index existing files next:

```bash
mm index /path/to/your/notes
```

If `mm init` registered an MCP client, ask it to `Call the mem_status tool`. See [Getting Started](https://github.com/memtomem/memtomem/blob/main/docs/guides/getting-started.md) for install alternatives, the [Korean Claude Code/Codex quickstart](https://github.com/memtomem/memtomem/blob/main/docs/guides/vibe-coding-getting-started-ko.md) for a plugin-first path, and [MCP Client Setup](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md) for manual registration.

`[all]` includes ONNX, Ollama and OpenAI integrations, Korean tokenization,
code chunking, the Web UI, Langfuse tracing, and LangGraph Store support.
Install bare `memtomem` for BM25-only usage. See the
[optional extras table](https://github.com/memtomem/memtomem/blob/main/docs/guides/getting-started.md#optional-extras)
for smaller bundles. If `mm` is not on PATH, run `uv tool update-shell` and
open a new shell. If an install appears stale, re-run it with `--refresh`.

> memtomem is the long-term-memory store. [memtomem-stm](https://github.com/memtomem/memtomem-stm) is a separate, optional MCP proxy for automatic surfacing, compression, and caching.

## Key Features

- **🔍 Hybrid search** — BM25 (FTS5) + dense vectors (sqlite-vec) merged via Reciprocal Rank Fusion. Exact terms via keyword, meaning via semantic, both at once.
- **📦 Semantic chunking** — heading-aware Markdown, AST-based Python, tree-sitter JS/TS, structure-aware JSON/YAML/TOML
- **♻️ Incremental indexing** — chunk-level SHA-256 diff means only changed chunks get re-embedded
- **🏷️ Namespaces** — scope memories into groups (work / personal / project) with optional auto-derivation from folder names; label them (colour, description) from Settings → Namespaces in the Web UI
- **🧹 Maintenance** — near-duplicate detection with merge, time-based score decay, TTL expiration, auto-tagging
- **🔄 Export / import** — JSON bundle backup and restore with re-embedding
- **🌐 Web UI** — polished SPA dashboard for search, sources, indexing, tags, and timeline (`mm web --dev` unlocks the full maintainer surface including Sessions, Working Memory, and Health Report)
- **🧭 Context Gateway** — keep canonical Skills, Commands, and Subagents in a project or user Store, optionally install reusable Wiki assets, then push them to supported runtimes
- **⚙️ Scriptable CLI** — `--json` output on `mm status` and write commands (`mm add` / `mm reset` / `mm purge`); `mm warmup` pre-loads local models so the first query skips cold-start
- **🛠️ 9-tool core mode** — a compact default surface, with `mem_do` routing the full feature set without loading every tool into agent context
- **📌 Pinned Context** — small file-backed user/project/agent blocks are composed before retrieved memory
- **🕸️ LangGraph Store** — optional `MemtomemBaseStore` supplies tuple-namespace JSON persistence and search

The full surface contains **99 current tools** plus the deprecated
`mem_context_migrate` compatibility alias. It includes the Pinned Context actions
(`mem_pinned_list/get/set/delete`, `mem_context_compose`) and review-first
formation actions (`mem_formation_scan`, `mem_candidate_propose/list/review/recover`). See the
[complete MCP table](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md#available-mcp-tools)
for every category.

## Documentation

Full documentation lives in the [memtomem GitHub repo](https://github.com/memtomem/memtomem):

| Guide | Topic |
|-------|-------|
| [Getting Started](https://github.com/memtomem/memtomem/blob/main/docs/guides/getting-started.md) | **Start here** — install, configure, save and find your first memory |
| [한국어 바이브코딩 빠른 시작](https://github.com/memtomem/memtomem/blob/main/docs/guides/vibe-coding-getting-started-ko.md) | Claude Code·Codex CLI에서 10~15분 안에 기억 저장·검색 |
| [MCP Client Setup](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md) | Connect Claude Code, Cursor, Codex, and other clients |
| [Core memory tools](https://github.com/memtomem/memtomem/blob/main/docs/guides/reference/core-memory-tools.md) | Index existing notes, search, and manage memories |
| [Configuration](https://github.com/memtomem/memtomem/blob/main/docs/guides/configuration.md) | Supported config files, precedence, and environment variables |
| [Embeddings](https://github.com/memtomem/memtomem/blob/main/docs/guides/embeddings.md) | ONNX, Ollama, and OpenAI providers, model dimensions, switching models |
| [Context Gateway](https://github.com/memtomem/memtomem/blob/main/docs/guides/context-gateway.md) | Author and push canonical Skills, Commands, and Subagents to each type's supported AI tools |
| [Operations & troubleshooting](https://github.com/memtomem/memtomem/blob/main/docs/guides/reference/operations.md) | Web UI, privacy audits, diagnostics, and recovery |
| [Reference](https://github.com/memtomem/memtomem/blob/main/docs/guides/reference.md) | Complete feature reference — all tools and patterns |
| [memtomem-stm](https://github.com/memtomem/memtomem-stm) | Optional STM proxy for proactive memory surfacing (separate package) |

## License

Apache License 2.0 — see [LICENSE](https://github.com/memtomem/memtomem/blob/main/LICENSE) for details.
