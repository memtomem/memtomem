# memtomem — Claude Code Plugin

Markdown-first semantic memory for AI agents. This plugin adds hybrid BM25 + dense search across your markdown files directly in Claude Code.

## Features

- **63 MCP tools** — search, add, edit, delete, index, recall, browse, tags, cross-ref, fetch, namespace, dedup, decay, export/import (configurable via tool mode: core/standard/full)
- **5 slash commands** — `/memtomem:search`, `/memtomem:remember`, `/memtomem:index`, `/memtomem:status`, `/memtomem:setup`
- **Automation hooks** — auto-search on prompt submit, auto-reindex on file edits
- **Memory curator agent** — deduplicate, tag, and clean up stale entries

## Installation

### MCP Server Setup

```bash
# PyPI (recommended)
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

### Local Plugin Development

```bash
claude --plugin-dir ./packages/memtomem-claude-plugin
```

## Quick Start

After installation, run `/memtomem:setup` to:

1. Verify embedding provider (Ollama by default)
2. Index your memory directory
3. Test search
4. Optionally activate automation hooks

## Prerequisites

- **Embedding provider**: [Ollama](https://ollama.ai) with `nomic-embed-text` (default), or OpenAI/Voyage API key
- **Python 3.12+**: Required for the MCP server
- **memtomem package**: The MCP server runs via `uvx --from memtomem memtomem-server` (PyPI) or `uv run --directory /path/to/memtomem memtomem-server` (source)

## Configuration

The plugin uses sensible defaults. Customize via environment variables or the `mem_config` tool:

| Setting | Default | Env Var |
|---------|---------|---------|
| Tool mode | `core` | `MEMTOMEM_TOOL_MODE` (`core`/`standard`/`full`) |
| Memory directory | `~/.memtomem/memories` | `MEMTOMEM_INDEXING__MEMORY_DIRS` |
| Embedding provider | `ollama` | `MEMTOMEM_EMBEDDING__PROVIDER` |
| Embedding model | `nomic-embed-text` | `MEMTOMEM_EMBEDDING__MODEL` |
| Database path | `~/.memtomem/memtomem.db` | `MEMTOMEM_STORAGE__SQLITE_PATH` |

**Tool mode**: Controls how many tools are exposed to the AI agent. `core` (9 tools, default) for minimal context usage -- includes `mem_do` meta-tool to access all other actions via `mem_do(action="...", params={...})`. `standard` (~30 + `mem_do`) for normal use, `full` (63 + `mem_do`) for everything. Fewer tools = less context tokens, better accuracy.

To set env vars for the MCP server, edit `.mcp.json` in the plugin directory:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_EMBEDDING__PROVIDER": "openai",
        "MEMTOMEM_EMBEDDING__API_KEY": "sk-..."
      }
    }
  }
}
```

## Hooks (Optional)

The plugin includes automation hooks that require the CLI in PATH:

```bash
# PyPI
uv tool install memtomem
# or: pipx install memtomem

# Source (if running from git clone)
# uv run mm ...
```

| Hook | Trigger | Action |
|------|---------|--------|
| `UserPromptSubmit` | Every prompt | Auto-search related memories |
| `PostToolUse` | After file edits | Auto-reindex changed files |

Without CLI installed, hooks fail silently — MCP tools work normally.

## Slash Commands

| Command | Description |
|---------|-------------|
| `/memtomem:search [query]` | Search memories with semantic search |
| `/memtomem:remember [content]` | Save a new memory entry |
| `/memtomem:index [path]` | Index or re-index files |
| `/memtomem:status` | Show status and statistics |
| `/memtomem:setup` | Guided initial setup |

## License

Apache-2.0 — same as the [memtomem](https://github.com/memtomem/memtomem) project.
