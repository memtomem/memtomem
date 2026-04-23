# MCP Client Configuration Guide

**Audience**: Users who want to connect memtomem to a specific AI editor
**Prerequisite**: [Getting Started](getting-started.md) complete (embedding path picked — BM25 default, or ONNX / Ollama / OpenAI via the wizard; optional cross-encoder reranker)
**Estimated Time**: ~5 minutes

> **Which editor should I use?**
> Any MCP-compatible editor works. If you're new, **Claude Code** is recommended — it has the simplest setup (one command).

### Key distinction

| Command | What it is | When to use |
|---------|-----------|-------------|
| `memtomem-server` | **MCP server** — runs in the background, connects to your editor | Always use this in MCP config |
| `memtomem` (or `mm`) | **CLI tool** — terminal commands for search, index, etc. | Optional, for terminal use |

> **Common mistake**: Using `memtomem` instead of `memtomem-server` in your MCP config will fail.

---

## 1. Claude Code

### Pick a scope

Claude Code has three install scopes — pick one based on how you want to
share the server:

| Scope | Flag | Shared with | Storage |
|-------|------|-------------|---------|
| local (default) | `-s local` (or omit `-s`) | This project × this user only | `~/.claude.json` → `projects."<cwd>".mcpServers` |
| project | `-s project` (or commit `.mcp.json`) | Everyone who clones the repo | `<project-root>/.mcp.json` |
| user | `-s user` | This user across every project | `~/.claude.json` → top-level `mcpServers` |

Precedence is `local > project > user`, so a `local` entry can override
a shared team `project` server when you need to test with personal
credentials.

### Add via command (`local` / `user`)

```bash
# Local scope (default) — this project only, not committed
claude mcp add memtomem -- uvx --from memtomem memtomem-server

# User scope — available in every project
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Both write to `~/.claude.json` — no need to edit that file by hand.

For the full plugin experience (slash commands, automation hooks, memory curator agent), see the [Claude Code integration guide](integrations/claude-code.md).

### Project scope — commit a `.mcp.json`

For a team-shared setup, create a `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Teammates see this server after approving Claude Code's workspace-trust
prompt on first use.

### Verify Connection

In Claude Code (or run `/memtomem:status` with the plugin):
```
Call the mem_status tool
```

---

## 2. Cursor

Create or edit the `~/.cursor/mcp.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Restart Cursor after configuration.
### Verify Connection

In Cursor's AI chat:
```
Call mem_status to check the memtomem connection status
```

---

## 3. Windsurf

Create or edit the `~/.codeium/windsurf/mcp_config.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Restart Windsurf after configuration.

---

## 4. Claude Desktop

Edit the `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Windows path: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after configuration.

---

## 5. Gemini CLI

Create or edit the `~/.gemini/settings.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Restart Gemini CLI after configuration.

---

## 6. Antigravity

1. Click the `...` menu at the top of the Agent panel > **MCP Servers**
2. Click **Manage MCP Servers** at the top of the MCP Store
3. Select **View raw config** > `mcp_config.json` will open
4. Add the memtomem server configuration:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"/path/to/notes\"]"
      }
    }
  }
}
```

> Antigravity does not support the `${workspaceFolder}` variable — use absolute paths.
> Restart the Agent session after changing settings.

---

## 7. Verifying Your Connection

These verification methods work across all clients.

### Calling mem_status

Ask the AI:

```
Call the mem_status tool to show the current status
```

Expected response (BM25 default — the `Embedding` and `Dimension`
lines change depending on the provider picked in the wizard):

```
memtomem Status
==============
Storage:   sqlite
DB path:   ~/.memtomem/memtomem.db
Embedding: none /
Dimension: 0
Top-K:     10
RRF k:     60

Index stats
-----------
Total chunks:  0
Source files:  0
...
```

The full report also includes an `Immutable fields` block (provider /
model / tokenizer / backend echoed back as a "what can't be changed at
runtime" reminder), and a `Warnings` block with stable schema keys
(`kind` / `fix` / `doc` / `stored` / `configured`) when an embedding-
dimension mismatch is detected. Run `mm status` from a terminal to see
the exact output your install produces.

### From a terminal — `mm status`

If the editor isn't reachable yet (or you want to verify the install
without involving any client), run the same check from a terminal:

```bash
mm status
```

`mm status` is a thin CLI wrapper over the same code path `mem_status`
uses, so the output is identical. Useful as a sanity check between
`mm init` and the first editor-side call.

### Available MCP Tools (74)

| Category | Tools |
|----------|-------|
| **Search** | `mem_search` (hybrid BM25+Dense+RRF), `mem_recall` (date-range retrieval), `mem_expand` (context-window expansion) |
| **Browse** | `mem_list` (indexed sources), `mem_read` (chunk by UUID) |
| **CRUD** | `mem_add`, `mem_edit`, `mem_delete`, `mem_batch_add` |
| **Indexing** | `mem_index` (file/directory indexing, optional `auto_tag`) |
| **Meta** | `mem_do` (routes to all registered actions, supports aliases) |
| **Ask** | `mem_ask` (natural-language Q&A over indexed memories) |
| **Namespace** | `mem_ns_list`, `mem_ns_set`, `mem_ns_get`, `mem_ns_assign`, `mem_ns_update`, `mem_ns_rename`, `mem_ns_delete` |
| **Tags** | `mem_tag_list`, `mem_tag_rename`, `mem_tag_delete`, `mem_auto_tag` |
| **Cross-ref** | `mem_link`, `mem_unlink`, `mem_related` |
| **Fetch** | `mem_fetch` (URL → markdown → index) |
| **Sessions** | `mem_session_start` (with optional `title`), `mem_session_end`, `mem_session_list` |
| **Working Memory** | `mem_scratch_set`, `mem_scratch_get`, `mem_scratch_promote` |
| **Procedures** | `mem_procedure_save`, `mem_procedure_list` |
| **Multi-Agent** | `mem_agent_register`, `mem_agent_search`, `mem_agent_share` |
| **Consolidation** | `mem_consolidate`, `mem_consolidate_apply` |
| **Reflection** | `mem_reflect`, `mem_reflect_save` |
| **History** | `mem_search_history`, `mem_search_suggest` |
| **Conflict** | `mem_conflict_check` |
| **Importance** | `mem_importance_scan` |
| **Entity** | `mem_entity_scan`, `mem_entity_search` |
| **Temporal** | `mem_timeline`, `mem_activity` |
| **Policy** | `mem_policy_add`, `mem_policy_list`, `mem_policy_delete`, `mem_policy_run` |
| **Health** | `mem_watchdog`, `mem_cleanup_orphans` |
| **Import** | `mem_import_notion`, `mem_import_obsidian` |
| **Maintenance** | `mem_dedup_scan`, `mem_dedup_merge`, `mem_decay_scan`, `mem_decay_expire` |
| **Data** | `mem_export`, `mem_import` |
| **Config** | `mem_stats`, `mem_status`, `mem_config`, `mem_embedding_reset`, `mem_reset` |
| **Evaluation** | `mem_eval` |
| **Context** | `mem_context_detect`, `mem_context_generate`, `mem_context_diff`, `mem_context_sync` (each accepts `include="skills,agents,commands"` to fan out `.memtomem/{skills,agents,commands}/` to Claude/Gemini/Codex runtimes; `generate`/`sync` also accept `strict=True` to fail on sub-agent or command field drops) |

> **Tool mode**: Set `MEMTOMEM_TOOL_MODE` to `core` (9 tools, default), `standard` (core + common packs + `mem_do`), or `full` (all 74 tools individually) to control how many tools are exposed. In `core` mode, use `mem_do(action="...", params={...})` to access any non-core action. Fewer tools = less context usage for AI agents.

### STM Proxy Tools (optional, separate package)

The STM proxy is distributed as a separate package: **[memtomem-stm](https://github.com/memtomem/memtomem-stm)**. Once installed and configured, it exposes additional tools including `stm_proxy_stats`, `stm_proxy_select_chunks`, `stm_proxy_read_more`, `stm_proxy_cache_clear`, `stm_surfacing_feedback`, `stm_surfacing_stats`, and dynamically proxied upstream tools (`{prefix}__{tool}`). See the [memtomem-stm README](https://github.com/memtomem/memtomem-stm#readme) for full setup and tool reference.

---

## 8. Environment Variable Overrides

You can override settings by adding environment variables to the `env` block.

> **List-typed settings must be JSON-encoded.** `MEMTOMEM_INDEXING__MEMORY_DIRS`
> is a list, so pass it as a JSON array literal string: `"[\"~/memories\"]"`
> — not a bare path. Passing a plain string will crash the MCP server on
> startup with a pydantic-settings parse error.

### Common Configuration Options

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]",
        "MEMTOMEM_STORAGE__SQLITE_PATH": "~/.memtomem/memtomem.db",
        "MEMTOMEM_EMBEDDING__MODEL": "nomic-embed-text"
      }
    }
  }
}
```

### Changing the Embedding Model

The recommended Ollama embedding model is `nomic-embed-text` (768d). To use a different model, set `MEMTOMEM_EMBEDDING__MODEL` and `MEMTOMEM_EMBEDDING__DIMENSION` in the `env` block.

**Example: BGE-M3 (1024d)**

```bash
ollama pull bge-m3
```

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]",
        "MEMTOMEM_EMBEDDING__MODEL": "bge-m3",
        "MEMTOMEM_EMBEDDING__DIMENSION": "1024"
      }
    }
  }
}
```

| Model | Dimension | Pull Command |
|-------|-----------|-------------|
| `nomic-embed-text` | 768 | `ollama pull nomic-embed-text` |
| `bge-m3` (multilingual) | 1024 | `ollama pull bge-m3` |

> The server default is `provider = "none"` (BM25 keyword-only, no
> embedding model). The models above are Ollama-specific choices; the
> wizard also exposes ONNX (`fastembed`) and OpenAI options.

> **Important**: `MEMTOMEM_EMBEDDING__DIMENSION` must match the model's output dimension. Mismatched values will cause indexing errors.

> **Security Note**: Instead of placing API keys directly in configuration files, it is recommended to use an OS keychain or environment variable management tool.

---

## Troubleshooting

### Tools don't appear in my editor

1. **Restart your editor** after changing MCP configuration
2. Check that you used `memtomem-server` (not `memtomem`) in your config
3. Test the server manually: `uvx --from memtomem memtomem-server` — should start without errors

### "Connection refused" or timeout

1. Check that Ollama is running: `ollama list`
2. For source installs, verify the `--directory` path is correct
3. Check for port conflicts if using SSE transport

### Embedding mismatch warning

Your database was created with a different embedding model than your current config.
```bash
mm embedding-reset                          # check status
mm embedding-reset --mode apply-current     # reset to current model
mm index ~/notes                            # re-index
```

---

## Next Steps

- [Getting Started](getting-started.md) — Setup wizard and first use
- [Reference](reference.md) — Complete feature reference
