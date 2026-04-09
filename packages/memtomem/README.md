# memtomem

Markdown-first long-term memory infrastructure for AI agents. Hybrid keyword + semantic search across your notes, docs, and code via the Model Context Protocol.

**Core philosophy**: `.md` files are the source of truth and the vector database is a derived cache. Manage memories as plain text files — memtomem makes them instantly searchable.

**Built for:**
- AI agents (Claude Code, Cursor, Windsurf, Claude Desktop) that need to *remember* between sessions
- Developers who want a searchable knowledge base built from their existing markdown notes — no proprietary database, no vendor lock-in
- Multilingual content (English, Korean, Japanese, Chinese) via `bge-m3` embeddings

## Installation

```bash
# As an MCP server (most common — no install needed, uvx handles it)
ollama pull nomic-embed-text         # one-time embedding model

# Add to Claude Code
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Or add to .mcp.json for Cursor / Windsurf / Claude Desktop
```

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

For terminal use, install the CLI separately:

```bash
uv tool install memtomem    # or: pipx install memtomem
mm init                     # 7-step interactive wizard
```

## Quick Start

In your AI editor, ask:

```
"Index my notes folder"   →  mem_index(path="~/notes")
"Search for deployment"   →  mem_search(query="deployment checklist")
"Remember this insight"   →  mem_add(content="...", tags="ops")
```

That's it. Your agent now has a long-term memory built from plain markdown files.

## Key Features

- **🔍 Hybrid search** — BM25 (FTS5) + dense vectors (sqlite-vec) merged via Reciprocal Rank Fusion. Exact terms via keyword, meaning via semantic, both at once.
- **📦 Semantic chunking** — heading-aware Markdown, AST-based Python, tree-sitter JS/TS, structure-aware JSON/YAML/TOML
- **♻️ Incremental indexing** — chunk-level SHA-256 diff means only changed chunks get re-embedded
- **🏷️ Namespaces** — scope memories into groups (work / personal / project) with optional auto-derivation from folder names
- **🧹 Maintenance** — near-duplicate detection with merge, time-based score decay, TTL expiration, auto-tagging
- **🔄 Export / import** — JSON bundle backup and restore with re-embedding
- **🌐 Web UI** — full-featured SPA dashboard for search, sources, indexing, tags, sessions, health monitoring
- **🛠️ 72 MCP tools** — full feature surface as MCP tools, with `mem_do` meta-tool routing 64 actions in `core` mode (default) for minimal context usage

## Documentation

Full documentation lives in the [memtomem GitHub repo](https://github.com/memtomem/memtomem):

| Guide | Topic |
|-------|-------|
| [Getting Started](https://github.com/memtomem/memtomem/blob/main/docs/guides/getting-started.md) | **Start here** — install, setup wizard, first use |
| [Hands-On Tutorial](https://github.com/memtomem/memtomem/blob/main/docs/guides/hands-on-tutorial.md) | Follow-along with example files |
| [User Guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/user-guide.md) | Complete feature walkthrough — all tools and patterns |
| [Configuration](https://github.com/memtomem/memtomem/blob/main/docs/guides/configuration.md) | All `MEMTOMEM_*` environment variables |
| [Embeddings](https://github.com/memtomem/memtomem/blob/main/docs/guides/embeddings.md) | Ollama and OpenAI providers, model dimensions, switching models |
| [MCP Client Setup](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md) | Editor-specific configuration |
| [Agent Memory Guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/agent-memory-guide.md) | Sessions, working memory, procedures, multi-agent |
| [Web UI Guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/web-ui.md) | Visual dashboard reference |
| [Hooks](https://github.com/memtomem/memtomem/blob/main/docs/guides/hooks.md) | Claude Code hooks for automatic indexing and search |
| [memtomem-stm](https://github.com/memtomem/memtomem-stm) | Optional STM proxy for proactive memory surfacing (separate package) |

## License

Apache License 2.0 — see [LICENSE](https://github.com/memtomem/memtomem/blob/main/LICENSE) for details.
