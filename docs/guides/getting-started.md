# Getting Started

This guide takes you from zero to a working memtomem setup. You'll be able to index your notes and search them from your AI editor in under 5 minutes.

---

## What is memtomem?

memtomem gives your AI coding agent (Claude Code, Cursor, etc.) **long-term memory**. You write notes as markdown files, memtomem indexes them, and your agent can search them by both keywords and meaning.

**Key terms**:
- **MCP** (Model Context Protocol) — a standard for connecting AI editors to external tools. memtomem uses MCP to talk to your editor.
- **Embedding** — a numeric representation of text meaning. memtomem uses embeddings to find notes that are *related* to your query, not just keyword-matching.
- **`memtomem-server`** — the MCP server that your editor connects to. This is what runs in the background.
- **`mm`** — the CLI (command-line tool) for terminal use. Optional but convenient.

---

## Prerequisites

| Requirement | Install | Verify |
|-------------|---------|--------|
| **Python 3.12+** | [python.org](https://python.org) | `python3 --version` |
| **Ollama** | [ollama.com](https://ollama.com) | `ollama list` |
| **An AI editor** | Claude Code, Cursor, Windsurf, etc. | Any one is enough |

> **No Ollama?** You can use OpenAI embeddings instead. The setup wizard will guide you — skip the `ollama pull` step below.

### Pull the embedding model

```bash
# English-only or light multilingual use:
ollama pull nomic-embed-text

# Korean, Japanese, Chinese, or heavy multilingual use (recommended):
ollama pull bge-m3
```

| Model | Size | Dimensions | Best for |
|-------|------|------------|----------|
| `nomic-embed-text` | 270MB | 768 | English-primary, fast, lightweight |
| `bge-m3` | 1.2GB | 1024 | Multilingual, cross-language search (KR/EN/JP/CN) |

> **Multilingual tip**: `bge-m3` significantly outperforms `nomic-embed-text` for cross-language search (e.g., Korean query finding English content). If you work with multiple languages, use `bge-m3`.

---

## Install

Choose one path:

### Option A: From PyPI (recommended for most users)

No install needed for MCP usage — `uvx` downloads and runs memtomem on demand when your editor starts.

If you also want the CLI (`mm` command):
```bash
uv tool install memtomem    # or: pipx install memtomem
```

Skip to [Connect to your AI editor](#connect-to-your-ai-editor).

### Option B: Project dependency (per-project isolation)

Add memtomem as a project dependency — version pinned in `pyproject.toml`:

```bash
uv add memtomem                 # or: uv add memtomem[all]
```

All CLI commands need the `uv run` prefix:
```bash
uv run mm init                  # setup wizard
uv run mm search "query"        # search
uv run mm web                   # web UI
```

The wizard auto-detects project installs and registers the MCP server with `uv run` instead of `uvx`.

### Option C: From source (for development or testing)

```bash
git clone https://github.com/memtomem/memtomem.git
cd memtomem
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e "packages/memtomem[all]"
```

`[all]` installs every optional dependency. You can also install only what you need:

| Extra | What it adds |
|-------|-------------|
| `ollama` | Local embedding via Ollama (`nomic-embed-text`) |
| `openai` | Cloud embedding via OpenAI |
| `korean` | Korean tokenizer (`kiwipiepy`) |
| `code` | Code chunking (`tree-sitter` for Python/JS/TS) |
| `web` | Web UI (`fastapi`, `uvicorn`) |
| `all` | All of the above |

```bash
# Example: only Ollama embeddings + web UI
uv pip install -e "packages/memtomem[ollama,web]"
```

Verify it works:
```bash
uv run mm -h               # CLI help
uv run memtomem-server     # MCP server starts (Ctrl+C to stop)
uv run pytest              # tests pass
```

---

## Setup wizard

The fastest way to configure everything:

```bash
mm init         # PyPI global install
uv run mm init  # Project or source install
```

The wizard walks you through 8 steps. Type `b` to go back, `q` to quit at any step.

#### Non-interactive mode (CI / automation)

Skip the wizard entirely with `-y`. All settings have sensible defaults:

```bash
mm init -y                                              # all defaults (Ollama + nomic-embed-text)
mm init -y --provider openai --api-key sk-...           # OpenAI
mm init -y --memory-dir ~/notes --mcp claude            # custom dir + Claude Code auto-setup
```

1. **Embedding provider** — BM25-only (default, zero-dependency), Local ONNX (no server), Ollama (local server), or OpenAI (cloud)
2. **Memory directory** — where your notes live (e.g., `~/notes`, `~/memories`)
3. **Storage** — SQLite database path (default: `~/.memtomem/memtomem.db`)
4. **Namespace** — auto-assign namespace from folder name (e.g., `~/docs` → `docs`)
5. **Search** — number of results per query (default: 10), time-decay toggle
6. **Language** — tokenizer selection: Unicode (default) or Korean (kiwipiepy)
7. **Claude Code hooks** — optional hook integration via settings.json
8. **Editor connection** — Claude Code auto-setup, .mcp.json generation, or manual

After the wizard, your MCP server is ready. Skip to [First use](#first-use) if you ran the wizard.

---

## Connect to your AI editor (manual)

If you skipped the wizard's editor step, or want to configure manually:

### Claude Code

```bash
# PyPI (global)
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Project dependency
claude mcp add memtomem -s user -- uv run --directory /path/to/project memtomem-server

# Source
claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Use `-s user` to make memtomem available in all projects. Use `-s project` for one project only.

### Cursor, Windsurf, Claude Desktop, Gemini CLI

Add to your MCP config file:

**PyPI:**
```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/notes\"]"
      }
    }
  }
}
```

**Source:**
```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/notes\"]"
      }
    }
  }
}
```

| Client | Config file |
|--------|-------------|
| Cursor | `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Gemini CLI | `~/.gemini/settings.json` |

> **Note**: Claude Code stores its MCP config in `~/.claude.json`, not a separate file.

### Verify connection

In your AI editor, ask:
```
Call the mem_status tool
```

You should see index statistics (0 chunks if nothing indexed yet).

---

## First use

### 1. Index your notes

In your editor:
```
"Index my notes folder"  →  mem_index(path="~/notes")
```

Or via CLI:
```bash
mm index ~/notes
```

This scans all supported files (`.md`, `.json`, `.yaml`, `.py`, `.js`, `.ts`, etc.), splits them into searchable chunks, and creates embeddings.

### 2. Search

```
"Search for deployment checklist"  →  mem_search(query="deployment checklist")
```

```bash
mm search "deployment checklist"
```

Results are ranked by a combination of keyword relevance and semantic similarity.

### 3. Add a memory

```
"Remember that Redis LRU→LFU reduced cache misses by 40%"
→  mem_add(content="Redis LRU→LFU migration reduced cache misses by 40%", tags="redis,performance")
```

```bash
mm add "Redis LRU→LFU reduced cache misses by 40%" --tags "redis,performance"
```

### 4. Recall recent memories

```
"What did I write this week?"  →  mem_recall(since="2026-04-01")
```

```bash
mm recall --since 2026-04-01
```

---

## CLI reference

All commands support `-h` and `--help`. Interactive wizards support `b` (back) and `q` (quit).

```bash
mm init                    # 8-step setup wizard
mm search "query"          # hybrid search
mm index ~/notes           # index files
mm add "some note"         # add a memory
mm recall --since 2026-04  # recall by date
mm config show             # view settings
mm config set key value    # change a setting
mm embedding-reset         # check/resolve embedding model mismatch
mm reset                   # delete all data and reinitialize the DB
mm context detect          # find agent config files
mm context generate        # sync project rules to all editors
mm session start           # start a tracked session
mm session end             # end session with auto-summary
mm session list            # list sessions
mm activity log            # log agent activity event
mm session wrap -- CMD     # wrap a command with session lifecycle
mm shell                   # interactive REPL
mm web                     # launch Web UI (http://localhost:8080)
```

---

## Troubleshooting

### "Ollama not found" or "not running"

```bash
ollama serve               # start the Ollama server
ollama list                # verify it's running
```

### "Embedding dimension mismatch"

Your database was created with a different model than your current config.

```bash
mm embedding-reset                          # check status
mm embedding-reset --mode apply-current     # reset DB to current model (re-index needed)
mm index ~/notes                            # re-index
```

### "No such command" when running `mm`

The CLI isn't installed. Install it:
```bash
uv tool install memtomem     # PyPI
# or
uv pip install -e "packages/memtomem[all]"  # Source
```

### Tools don't appear in my editor

1. Restart your editor after configuring MCP
2. Check that `memtomem-server` (not `memtomem`) is in your MCP config
3. Verify: `uvx --from memtomem memtomem-server` should start without errors

---

## Optional: Sync project rules, skills, sub-agents, and commands across editors

If you use multiple AI editors, keep their config files — and their agent **skills**, **sub-agents**, and **slash commands** — in sync from one source under `.memtomem/`:

```bash
mm context init                         # create .memtomem/context.md from existing files
mm context generate --agent all         # generate CLAUDE.md, .cursorrules, GEMINI.md, etc.
mm context sync                         # update all after editing context.md

# Also mirror .memtomem/skills/  → .claude/skills/, .gemini/skills/, .agents/skills/
mm context sync --include=skills

# Also fan out .memtomem/agents/  → .claude/agents/, .gemini/agents/, ~/.codex/agents/
# (reports dropped fields per runtime; add --strict to fail on any drop)
mm context sync --include=agents

# Also fan out .memtomem/commands/  → .claude/commands/*.md, .gemini/commands/*.toml
# (Markdown ↔ TOML conversion with $ARGUMENTS ↔ {{args}} placeholder rewrite)
mm context sync --include=commands

# Everything in one shot
mm context sync --include=skills,agents,commands
```

See the [Agent Context Management section of the user guide](user-guide.md#agent-context-management--mm-context) for the full fan-out matrix and field-loss details.

---

## Optional: STM Proxy — Proactive Memory Surfacing

STM automatically surfaces relevant memories when your agent uses other MCP tools. It's optional — basic search/add works without it.

STM is a separate package: **[memtomem-stm](https://github.com/memtomem/memtomem-stm)**. Install via PyPI:

```bash
pip install memtomem-stm
```

See the [memtomem-stm README](https://github.com/memtomem/memtomem-stm#readme) for proxy configuration, surfacing setup, and CLI usage.

---

## Optional: Web UI

For a visual dashboard with search, tags, sessions, and health monitoring:

```bash
mm web                     # opens http://localhost:8080
```

See [Web UI Guide](web-ui.md) for details.

---

## Optional: LLM Provider

memtomem can use an LLM for enhanced features like consolidation summaries, semantic auto-tagging, and query expansion. LLM is disabled by default — basic search, indexing, and tagging work without it.

To enable:

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=ollama    # or: openai, anthropic
```

See [LLM Providers](llm-providers.md) for full setup including local servers (LM Studio, vLLM) and cloud APIs (OpenRouter).

---

## Uninstall

To completely remove memtomem, see the
[Uninstalling memtomem](user-guide.md#uninstalling-memtomem) section of the
User Guide. The short version:

```bash
# 1. Remove MCP server from your editor config (see table below)
# 2. Uninstall the package
uv tool uninstall memtomem    # or: pipx uninstall memtomem / uv remove memtomem
# 3. Delete data
rm -rf ~/.memtomem
```

---

## Next steps

- [Hands-On Tutorial](hands-on-tutorial.md) — follow-along with example files
- [User Guide](user-guide.md) — complete feature walkthrough
- [Agent Memory Guide](agent-memory-guide.md) — sessions, working memory, procedures
- [LLM Providers](llm-providers.md) — Ollama, OpenAI, and compatible endpoints
- [MCP Client Setup](mcp-clients.md) — editor-specific configuration
