# memtomem

> 🚧 **Alpha** — APIs, defaults, and on-disk config surfaces may still change between `0.1.x` releases. Feedback and issue reports are especially welcome at [github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

Markdown-first long-term memory infrastructure for AI agents. Hybrid keyword + semantic search across your notes, docs, and code via the Model Context Protocol.

**Core philosophy**: `.md` files are the source of truth and the vector database is a derived cache. Manage memories as plain text files — memtomem makes them instantly searchable.

**Built for:**
- AI agents (Claude Code, Cursor, Windsurf, Claude Desktop) that need to *remember* between sessions
- Developers who want a searchable knowledge base built from their existing markdown notes — no proprietary database, no vendor lock-in
- Multilingual content (English, Korean, Japanese, Chinese) via `bge-m3` embeddings

## Quick Start

```bash
# 1. Install memtomem (requires Python 3.12+)
uv tool install memtomem        # or: pipx install memtomem

# 2. Run the setup (preset picker → memory_dir + MCP)
mm init    # on PATH after `uv tool install` — no `uv run` needed
```

The picker offers three presets and an Advanced fallback:

| Preset | Embedding | Reranker | Tokenizer |
|---|---|---|---|
| Minimal | BM25 only (no download) | — | unicode61 |
| English (Recommended) | ONNX `bge-small-en-v1.5` (~33 MB, 384d) | English (`ms-marco-MiniLM-L-6-v2`) | unicode61 |
| Korean-optimized | ONNX `bge-m3` (~1.2 GB, 1024d) | Multilingual (`jina-reranker-v2`) | `kiwipiepy` |
| Advanced | — | — | — (full 10-step wizard, all options) |

Pick a preset interactively, or use `mm init -y` (minimal), `mm init --preset korean -y`, or `mm init --advanced` for scripted runs. See [Embeddings](https://github.com/memtomem/memtomem/blob/main/docs/guides/embeddings.md) for the full model matrix.

Then in your AI editor, ask:

```
"Call the mem_status tool"   →  confirms the server is connected
"Index my notes folder"      →  mem_index(path="~/notes")
"Search for deployment"      →  mem_search(query="deployment checklist")
"Remember this insight"      →  mem_add(content="...", tags=["ops"])
```

That's it. Your agent now has a long-term memory built from plain markdown files.

For full setup, OpenAI configuration, and troubleshooting, see the [Getting Started guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/getting-started.md).

<details>
<summary><b>Prefer no install? (uvx direct, MCP only)</b></summary>

If you'd rather skip the CLI install, `uvx` will download and run memtomem on demand. `~/.memtomem/memories` is always indexed; for AI tool memory folders (Claude Code per-project memory, Claude plans, Codex memories), run `mm init` once and pick the surfaces you want indexed — nothing is added silently. Set `MEMTOMEM_INDEXING__MEMORY_DIRS` to add custom paths.

```bash
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server
```

Or add the following to your MCP client config file — the path depends on
the editor: `~/.cursor/mcp.json` (Cursor),
`~/.codeium/windsurf/mcp_config.json` (Windsurf),
`~/Library/Application Support/Claude/claude_desktop_config.json`
(Claude Desktop), or `~/.gemini/settings.json` (Gemini CLI):

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"/path/to/your/notes\"]"
      }
    }
  }
}
```

</details>

## Key Features

- **🔍 Hybrid search** — BM25 (FTS5) + dense vectors (sqlite-vec) merged via Reciprocal Rank Fusion. Exact terms via keyword, meaning via semantic, both at once.
- **📦 Semantic chunking** — heading-aware Markdown, AST-based Python, tree-sitter JS/TS, structure-aware JSON/YAML/TOML
- **♻️ Incremental indexing** — chunk-level SHA-256 diff means only changed chunks get re-embedded
- **🏷️ Namespaces** — scope memories into groups (work / personal / project) with optional auto-derivation from folder names
- **🧹 Maintenance** — near-duplicate detection with merge, time-based score decay, TTL expiration, auto-tagging
- **🔄 Export / import** — JSON bundle backup and restore with re-embedding
- **🌐 Web UI** — full-featured SPA dashboard for search, sources, indexing, tags, sessions, health monitoring
- **🛠️ 74 MCP tools** — full feature surface as MCP tools, with `mem_do` meta-tool routing all registered actions in `core` mode (default) for minimal context usage

## Documentation

Full documentation lives in the [memtomem GitHub repo](https://github.com/memtomem/memtomem):

| Guide | Topic |
|-------|-------|
| [Getting Started](https://github.com/memtomem/memtomem/blob/main/docs/guides/getting-started.md) | **Start here** — install, setup wizard, first use |
| [Reference](https://github.com/memtomem/memtomem/blob/main/docs/guides/reference.md) | Complete feature reference — all tools and patterns |
| [Configuration](https://github.com/memtomem/memtomem/blob/main/docs/guides/configuration.md) | All `MEMTOMEM_*` environment variables |
| [Embeddings](https://github.com/memtomem/memtomem/blob/main/docs/guides/embeddings.md) | ONNX, Ollama, and OpenAI providers, model dimensions, switching models |
| [MCP Client Setup](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md) | Editor-specific configuration |
| [memtomem-stm](https://github.com/memtomem/memtomem-stm) | Optional STM proxy for proactive memory surfacing (separate package) |

## License

Apache License 2.0 — see [LICENSE](https://github.com/memtomem/memtomem/blob/main/LICENSE) for details.
