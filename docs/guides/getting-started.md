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
| **An AI editor** | Claude Code, Cursor, Windsurf, etc. | Any one is enough |

### Pick an embedding path (optional)

memtomem ships with four embedding options. The setup wizard in the next
section asks which one you want and writes the config for you — you
don't have to decide now.

| Option | Setup | When to pick it |
|--------|-------|-----------------|
| **Keyword-only (BM25)** | None | Default. Fast, no external deps. Great for short, exact-term notes. |
| **ONNX (local, no server)** | `uv tool install 'memtomem[onnx]'` | Semantic search without running a server. ~22 MB–1.2 GB model on first use. |
| **Ollama (local server)** | Install [Ollama](https://ollama.com), then `ollama pull nomic-embed-text` (English) or `ollama pull bge-m3` (multilingual, 1.2 GB). | Semantic search with full local control; best Korean/JP/CN quality with `bge-m3`. |
| **OpenAI (cloud)** | `OPENAI_API_KEY` env var. | No local model to manage; pay-per-call. |

> **Multilingual tip**: if you work with Korean, Japanese, or Chinese,
> pick Ollama with `bge-m3` or OpenAI `text-embedding-3-small` — both
> significantly outperform English-only models for cross-language search.

---

## Install

Choose one path:

### Option A: From PyPI (recommended for most users)

No install needed for MCP usage — `uvx` downloads and runs memtomem on demand when your editor starts.

If you also want the CLI (`mm` command):
```bash
uv tool install 'memtomem[all]'    # or: pipx install 'memtomem[all]'
mm --version                        # verify install
```

**`[all]` vs minimal**: `[all]` bundles ONNX dense embeddings, Korean tokenizer, Ollama / OpenAI SDKs, code chunker, and the Web UI (~250 MB total). For a BM25-only install without downloads (~40 MB), skip the extras:

```bash
uv tool install memtomem            # BM25 only — dense search, Web UI, Korean tokenizer unavailable
```

You can opt in to individual features later with `uv tool install --reinstall 'memtomem[onnx,web]'` or any combination from the extras table in [Option C](#option-c-from-source-for-development-or-testing).

> **If `mm --version` shows an older release**: `uv` caches PyPI metadata per package, and a fresh install can resolve to the cached entry for a short window after a new release. Re-run with `uv tool install 'memtomem[all]' --refresh`, or clear the cache first: `uv cache clean memtomem`. Check the [latest release](https://github.com/memtomem/memtomem/releases) for the expected version.

> **If you see `mm: command not found`**: `uv tool install` writes the `mm` shim to `~/.local/bin` (macOS/Linux default), but that directory isn't on `$PATH` in a fresh shell profile. Run `uv tool update-shell` once, then open a new shell and re-run `mm --version`. (`uv` prints a one-line hint on first-ever tool install, but it's easy to miss.)

Skip to [Connect to your AI editor](#connect-to-your-ai-editor).

### Option B: Project dependency (per-project isolation)

Add memtomem as a project dependency — version pinned in `pyproject.toml`:

```bash
uv add 'memtomem[all]'          # or: uv add memtomem (BM25-only, skip extras)
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
| `onnx` | Local embedding via ONNX (`fastembed`) — no server needed |
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

`mm init` starts with a preset picker — pick one of three bundled setups (**Minimal**, **English (Recommended)**, **Korean-optimized**) or choose **Advanced** for the full 10-step wizard. Preset paths only ask about the memory directory and MCP registration.

### Choose your setup

| Preset | What it bundles | When to pick |
|---|---|---|
| **Minimal** | BM25 keyword search, no downloads, unicode61 tokenizer, no reranker | Want the lightest possible install, or starting from scratch to explore |
| **English (Recommended)** | ONNX `bge-small-en-v1.5` (384d, ~33 MB) + English reranker (`Xenova/ms-marco-MiniLM-L-6-v2`) + auto-discover provider memory folders | Most English-language setups — good default if you're unsure |
| **Korean-optimized** | ONNX `bge-m3` (1024d, ~1.2 GB) + multilingual reranker (`jinaai/jina-reranker-v2-base-multilingual`) + `kiwipiepy` tokenizer + auto-discover | Korean content (or Korean/Chinese/Japanese mixed) |
| **Advanced** | — (10-step wizard, full control) | Need to set every knob — custom model, separate DB path, decay, etc. |

Type `b` to go back or `q` to quit at any prompt.

#### Non-interactive mode (CI / automation)

Skip prompting with `-y`. `mm init -y` alone applies the **Minimal** preset (same defaults as before this feature landed); pass `--preset` for the others:

```bash
mm init -y                                              # Minimal preset (BM25-only)
mm init --preset english -y                             # English recommended
mm init --preset korean -y                              # Korean-optimized
mm init --advanced                                      # Force the full 10-step wizard

# Explicit flags override preset values:
mm init -y --provider onnx --model all-MiniLM-L6-v2     # custom ONNX model
mm init -y --provider ollama --model nomic-embed-text   # Ollama (requires `ollama serve`)
mm init -y --provider openai --api-key sk-...           # OpenAI
mm init -y --memory-dir ~/notes --mcp claude            # custom dir + Claude Code auto-setup

# Pull in AI tool memory folders (repeat per category):
mm init -y --include-provider claude-memory --include-provider codex
```

`--preset` and `--advanced` are mutually exclusive. Running without `-y` / `--preset` / `--advanced` from a non-TTY (e.g., piped stdin) exits with an error — pass one of those flags explicitly.

#### Advanced (10-step wizard) step list

Selecting **Advanced** (from the picker or `--advanced`) runs all ten steps:

1. **Embedding provider** — BM25-only (default, zero-dependency), Local ONNX (no server), Ollama (local server), or OpenAI (cloud)
2. **Reranker (optional)** — off by default; opt-in to a local fastembed cross-encoder. Korean/Chinese/Japanese/mixed content should pick the multilingual model
3. **Memory directory** — where your notes live (e.g., `~/notes`, `~/memories`)
4. **Provider memory folders** — opt in (per category) to indexing Claude Code per-project memory (`~/.claude/projects/*/memory/`), Claude plans (`~/.claude/plans/`), and/or Codex memories (`~/.codex/memories/`). Skipped silently if none are present. Nothing is added without your confirmation
5. **Storage** — SQLite database path (default: `~/.memtomem/memtomem.db`)
6. **Namespace** — auto-assign namespace from folder name (e.g., `~/docs` → `docs`)
7. **Search** — number of results per query (default: 10), time-decay toggle
8. **Language** — tokenizer selection: Unicode (default) or Korean (kiwipiepy)
9. **Claude Code hooks** — optional hook integration via settings.json
10. **Editor connection** — Claude Code auto-setup, .mcp.json generation, or manual

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

This one-shot command seeds the index with files already on disk. After
this, the `mm server` file watcher keeps your `memory_dirs` in sync with
new edits automatically — you only need to run `mm index` again when you
add a brand-new directory or want a forced rebuild (`--force`).

In your editor:
```
"Index my notes folder"  →  mem_index(path="~/notes")
```

Or via CLI:
```bash
mm index ~/notes
```

This scans all supported files (`.md`, `.json`, `.yaml`, `.py`, `.js`, `.ts`, etc.), splits them into searchable chunks, and creates embeddings. The re-run is idempotent (content-hash dedup), so it's safe to repeat.

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
mm init                    # preset picker (or `--advanced` for the full 10-step wizard)
mm search "query"          # hybrid search
mm index ~/notes           # index files
mm add "some note"         # add a memory
mm recall --since 2026-04  # recall by date
mm config show             # view settings
mm config set key value    # change a setting
mm config unset key        # drop a pinned override (e.g., mmr.enabled)
mm embedding-reset         # check/resolve embedding model mismatch
mm reset                   # delete all data and reinitialize the DB
mm context detect          # find agent config files
mm context init            # create .memtomem/context.md from existing files
mm context generate        # generate CLAUDE.md, .cursorrules, GEMINI.md, etc.
mm context diff            # show pending changes before syncing
mm context sync            # update all editors after editing context.md
mm session start           # start a tracked session
mm session end             # end session with auto-summary
mm session list            # list sessions
mm session events <id>     # show events for a session
mm activity log            # log agent activity event
mm session wrap -- CMD     # wrap a command with session lifecycle
mm watchdog status         # show latest health check results
mm watchdog run            # run health checks immediately
mm watchdog history        # view historical health check results
mm ingest claude-memory    # index Claude Code auto-memory
mm ingest gemini-memory    # index Gemini CLI memory
mm ingest codex-memory     # index Codex CLI memory
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
uv tool install 'memtomem[all]'     # PyPI
# or
uv pip install -e "packages/memtomem[all]"  # Source
```

### Tools don't appear in my editor

1. Restart your editor after configuring MCP
2. Check that `memtomem-server` (not `memtomem`) is in your MCP config
3. Verify install: `uvx --from memtomem mm --version` should print the memtomem
   version. (`mm` and `memtomem-server` ship from the same package. Running
   `memtomem-server` bare isn't a useful check — it expects JSON-RPC on stdin,
   so a TTY emits noisy `Invalid JSON` errors.)

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

Run `mm context --help` for the full fan-out matrix across editors (Claude Code, Cursor, Gemini CLI, OpenAI Codex, GitHub Copilot) and per-runtime field-drop details.

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

For a visual dashboard:

```bash
mm web                     # polished dashboard on http://127.0.0.1:8080
mm web --dev               # adds opt-in maintainer pages
```

The default surface covers the Home, Search, Sources, Index, Tags, Timeline, and More tabs (the More tab hosts Settings, Dedup, Age-out, Export/Import, and Reset Database). Pass `--dev` (or set `MEMTOMEM_WEB__MODE=dev` in your shell profile) to expose maintainer pages like Namespaces, Sessions, Working Memory, and Health Report — see [Configuration → Web UI Mode](configuration.md#web-ui-mode) for details.

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
[Uninstalling memtomem](uninstall.md) guide. The short version:

```bash
# 1. Remove MCP server from your editor config (see table below)
# 2. Uninstall the package
uv tool uninstall memtomem    # or: pipx uninstall memtomem / uv remove memtomem
# 3. Delete data
rm -rf ~/.memtomem
```

---

## Next steps

- [Reference](reference.md) — complete feature reference for all tools and patterns
- [Configuration](configuration.md) — all `MEMTOMEM_*` environment variables
- [Embeddings](embeddings.md) — ONNX, Ollama, OpenAI providers
- [LLM Providers](llm-providers.md) — Ollama, OpenAI, and compatible endpoints
- [MCP Client Setup](mcp-clients.md) — editor-specific configuration
