# memtomem — OpenClaw Plugin

Markdown-first semantic memory for AI agents. This plugin bridges the memtomem MCP server into OpenClaw, exposing all 63 memory tools to your AI gateway.

## Architecture

```
OpenClaw Gateway
  └── memtomem plugin (Node.js, in-process)
        └── McpBridge (stdio transport)
              └── memtomem-server (Python subprocess)
                    └── SQLite + sqlite-vec
```

The plugin starts the memtomem MCP server as a subprocess on the first tool call and communicates over stdio. The server stays running for the lifetime of the gateway.

## Installation

### From source

```bash
git clone https://github.com/memtomem/memtomem.git
openclaw plugins install ./memtomem/packages/memtomem-openclaw-plugin -l
```

## Prerequisites

- **Node.js 22+**
- **Python 3.12+** with `uvx` available (for starting the MCP server)
- **Embedding provider**: [Ollama](https://ollama.ai) with `nomic-embed-text` (default)
- **memtomem**: Install via `uv pip install -e "packages/memtomem[all]"` (source) or `uvx --from memtomem memtomem-server` (PyPI)

## Configuration

In `~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    entries: {
      "memtomem": {
        enabled: true,
        config: {
          // Default: uses uvx to fetch and run from PyPI
          // "command": "uvx",
          // "serverArgs": ["--from", "memtomem", "memtomem-server"]

          // Alternative: use a local installation
          // "command": "memtomem-server"
          // "serverArgs": []
        },
        env: {
          // Configure embedding provider
          // "MEMTOMEM_EMBEDDING__PROVIDER": "openai",
          // "MEMTOMEM_EMBEDDING__API_KEY": "sk-..."
        }
      }
    }
  }
}
```

## Tools (63)

All 63 memtomem MCP tools are registered as OpenClaw agent tools:

| Category | Tools |
|----------|-------|
| **Search** | `mem_search`, `mem_recall` |
| **Browse** | `mem_list`, `mem_read` |
| **CRUD** | `mem_add`, `mem_edit`, `mem_delete`, `mem_batch_add` |
| **Indexing** | `mem_index` |
| **Meta** | `mem_do` (routes to 55 non-core actions) |
| **Namespace** | `mem_ns_list`, `mem_ns_set`, `mem_ns_get`, `mem_ns_update`, `mem_ns_rename`, `mem_ns_delete` |
| **Tags** | `mem_tag_list`, `mem_tag_rename`, `mem_tag_delete` |
| **Cross-ref** | `mem_link`, `mem_unlink`, `mem_related` |
| **Fetch** | `mem_fetch` |
| **Sessions** | `mem_session_start`, `mem_session_end`, `mem_session_list` |
| **Scratchpad** | `mem_scratch_set`, `mem_scratch_get`, `mem_scratch_promote` |
| **Procedures** | `mem_procedure_save`, `mem_procedure_list` |
| **Multi-Agent** | `mem_agent_register`, `mem_agent_search`, `mem_agent_share` |
| **Consolidation** | `mem_consolidate`, `mem_consolidate_apply` |
| **Reflection** | `mem_reflect`, `mem_reflect_save` |
| **Evaluation** | `mem_eval` |
| **Search History** | `mem_search_history`, `mem_search_suggest` |
| **Conflict** | `mem_conflict_check` |
| **Importance** | `mem_importance_scan` |
| **Import** | `mem_import_notion`, `mem_import_obsidian` |
| **Maintenance** | `mem_dedup_scan`, `mem_dedup_merge`, `mem_decay_scan`, `mem_decay_expire`, `mem_auto_tag` |
| **Data** | `mem_export`, `mem_import` |
| **Config** | `mem_stats`, `mem_status`, `mem_config`, `mem_embedding_reset` |

## How It Works

1. **Plugin registration**: All 72 tools are registered with OpenClaw using their full JSON Schema definitions
2. **Lazy connection**: The MCP server subprocess is started on the first tool call, not at gateway startup
3. **Stdio bridge**: Tool calls are forwarded to the MCP server over stdin/stdout using the MCP protocol
4. **Graceful shutdown**: A background service closes the bridge when the gateway stops

## License

Apache-2.0 — same as the [memtomem](https://github.com/memtomem/memtomem) project.
