# Getting Started

This guide takes a new installation from zero to one verified memory round
trip. You do not need an existing notes directory, embedding server, API key,
or connected AI editor for the first success.

**On this page**

- [Quickstart](#quickstart)
- [What is memtomem?](#what-is-memtomem)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Setup wizard](#setup-wizard)
- [First use](#first-use)
- [Connect to your AI editor](#connect-to-your-ai-editor-manual)
- [Sync memories across your own machines](#optional-sync-memories-across-your-own-machines)
- [Troubleshooting](#troubleshooting)
- [Next steps](#next-steps)

## Quickstart

```bash
uv tool install 'memtomem[all]'
mm init
mm status
mm add "Deployment checklist uses blue-green rollout" --tags ops
mm search "blue-green"
```

The last command should return the sentence you just added. `mm add` creates a
Markdown file under your configured user memory directory and indexes it
immediately, so this flow does not depend on a pre-existing `~/notes` folder.

If setup registered an MCP client, ask that client to `Call the mem_status
tool` after the terminal round trip succeeds.

## What is memtomem?

memtomem gives AI coding agents long-term memory backed by files you control.
It indexes Markdown, JSON, YAML, Python, and JavaScript/TypeScript content and
can search by keywords, meaning, or both.

Key terms:

- **MCP** (Model Context Protocol) — how an AI client calls memtomem tools.
- **Embedding** — a numeric representation used for meaning-based search.
- **`memtomem-server`** — the MCP server your AI client starts.
- **`mm`** — the terminal CLI for setup, memory work, and operations.

Core memory operations are hook-free by default: they run only when you or an
agent calls them. Optional client hooks are explicit integrations and can be
installed or removed separately.

## Prerequisites

<a id="pick-an-embedding-path-optional"></a>

| Requirement | Install | Verify |
|---|---|---|
| Python 3.12+ | [python.org](https://python.org) | `python3 --version` |
| `uv` or `pipx` | [uv installation](https://docs.astral.sh/uv/getting-started/installation/) | `uv --version` |
| An AI editor | Optional for the terminal first success | — |

The Quickstart installs the `all` extra so every documented path is available.
For the smallest BM25-only installation, omit `[all]`.

## Install

Choose one installation path. The remainder of this guide uses a global tool
install, where commands are invoked as `mm ...`.

### Option A: From PyPI (recommended for most users)

```bash
uv tool install 'memtomem[all]'       # or: pipx install 'memtomem[all]'
mm --version
```

`[all]` includes every bundle below. For BM25-only search without optional downloads:

```bash
uv tool install memtomem
```

Add features later with a reinstall such as:

```bash
uv tool install --reinstall 'memtomem[onnx,web]'
```

<a id="optional-extras"></a>

#### Optional extras

| Extra | Adds |
|---|---|
| `onnx` | Local FastEmbed embeddings and reranking |
| `ollama` | Ollama Python SDK integration |
| `openai` | OpenAI Python SDK integration |
| `korean` | Kiwi Korean tokenizer |
| `code` | Tree-sitter Python, JavaScript, and TypeScript chunking |
| `web` | FastAPI/uvicorn Web UI runtime |
| `langfuse` | Langfuse tracing |
| `langgraph` | `MemtomemStore` and `MemtomemBaseStore` adapters |
| `all` | Every extra above |

### Option B: Project dependency (per-project isolation)

```bash
uv add 'memtomem[all]'
uv run mm init
```

Use the `uv run` prefix for every command in this installation mode.

### Option C: From source (for development or testing)

```bash
git clone https://github.com/memtomem/memtomem.git
cd memtomem
uv sync
uv run mm --help
```

Source checkouts also use the `uv run mm ...` form.

## Setup wizard

Run:

```bash
mm init
```

The picker offers four paths:

<a id="choose-your-setup"></a>

| Preset | Search stack | Choose it when |
|---|---|---|
| **Minimal** | BM25 keyword search | You want no model download or external service |
| **English (Recommended)** | Local ONNX English embedding + reranker | Most notes are English |
| **Korean-optimized** | Local multilingual ONNX + Korean tokenizer + reranker | Notes include Korean, Chinese, or Japanese |
| **Advanced** | Full ten-step wizard | You need to choose every provider and path |

For the most deterministic first proof, choose **Minimal**. It needs no model
download or external service; you can rerun `mm init` after the round trip to
switch to English, Korean-optimized, or Advanced settings.

Preset paths ask for the memory directory and optional MCP registration. The
result is stored in `~/.memtomem/config.json`; generated MCP entries contain
the server command, not duplicate environment overrides.

For automation:

```bash
mm init --non-interactive --preset minimal --mcp skip
mm init --non-interactive --preset english --mcp skip
mm init --non-interactive --preset korean --mcp skip
mm init --advanced
```

`-y` is a deprecated alias for `--non-interactive`; scripts should use the
long option before v0.5.0 changes `-y` into an accepted no-op.

## First use

### 1. Check the empty store

```bash
mm status
```

A new store reports zero chunks. This confirms that the config and database
can be opened before you write anything.

### 2. Add one memory

<a id="3-add-a-memory"></a>

```bash
mm add "Deployment checklist uses blue-green rollout" --tags ops
```

The command writes the entry to the first configured user memory directory
and indexes it immediately.

### 3. Find it again

<a id="2-search"></a>

```bash
mm search "blue-green"
```

The result should contain the same sentence. For scripts, use `mm status
--json` and the documented JSON forms of write commands.

### 4. Index existing notes

<a id="1-index-your-notes"></a>

Once the controlled round trip works, point memtomem at a directory that
already exists:

```bash
mm index /path/to/your/notes
mm search "a phrase from those notes"
```

See [Core memory tools](reference/core-memory-tools.md) for incremental
indexing, filters, namespaces, redaction, and force-reindex behavior.

### 5. Organise memories with namespaces

Namespaces are optional labels such as `work`, `personal`, or a project name.
Use the default while learning; see [Configuration → Namespace](configuration.md#namespace)
when you need path-based rules or bulk organization.

## Connect to your AI editor (manual)

<a id="claude-code"></a>
<a id="cursor-windsurf-claude-desktop-antigravity-cli-gemini-cli"></a>
<a id="verify-connection"></a>

If the setup wizard registered your client, restart or reconnect it and ask:

```text
Call the mem_status tool
```

If you skipped registration, use the client-specific instructions in
[MCP Client Setup](mcp-clients.md). A normal manual registration starts the
server and lets it read `~/.memtomem/config.json`; it should not repeat
`MEMTOMEM_INDEXING__MEMORY_DIRS` in the MCP entry unless you intentionally
want an environment override.

Claude Code's minimal manual registration is:

```bash
claude mcp add memtomem -s user -- memtomem-server
```

This reuses the persistent `memtomem[all]` environment installed above. For a
one-off setup without a persistent install, use `uvx --isolated --from
"memtomem[all]==0.3.11" memtomem-server`. Claude Code users can instead
install the [memtomem plugin](integrations/claude-code.md#mcp-server-setup),
which provides an exact-pinned MCP server and six focused workflows. Automatic
indexing hooks are supplied only by the separate `memtomem-automation` plugin.
Manual and plugin-managed server copies are not run simultaneously.

## Optional: Sync memories across your own machines

The first-use flow indexes one local directory. To keep a private Markdown
memory repository synchronized across your machines, follow
[Multi-device sync](multi-device-sync.md). This is separate from database
export/import and from Context Gateway artifact push/pull.

## CLI reference

<a id="4-recall-recent-memories"></a>

Daily commands are intentionally small:

```bash
mm status
mm add "note" --tags tag1,tag2
mm index /path/to/notes
mm search "query"
mm recall --since 2026-01-01
```

Use `mm --help` for the installed command tree and
[Data, config & CLI](reference/data-config-cli.md#cli-reference) for the full
reference.

## Troubleshooting

### Indexed, but search returns nothing

Run `mm status`, confirm `total_chunks` is nonzero, remove filters, and search
for an exact phrase from a known file. Then see
[Operations → No results found](reference/operations.md#no-results-found).

### "Nothing gets indexed" — path outside your index roots

Use an existing path with `mm index /absolute/path`. If a managed write is
outside the configured roots, add it through `mm init`, the Web UI, or the
documented config surfaces before retrying.

### "Ollama not found" or "not running"

Run `ollama list`, pull the configured model, or switch to the Minimal/ONNX
path. See [Embeddings](embeddings.md#ollama-local-server).

### "Embedding dimension mismatch"

Stop other memtomem processes, run `mm embedding-reset`, choose the intended
model, and re-index. See [Switching models](embeddings.md#switching-models-on-an-existing-index).

### "No such command" when running `mm`

```bash
uv tool update-shell
uv tool install 'memtomem[all]' --refresh
```

Open a new shell after updating PATH. Project/source installs require
`uv run mm` instead.

### Tools don't appear in my editor

Confirm `mm status` works in a terminal, then restart the client and follow
[MCP Client Setup → Verifying Your Connection](mcp-clients.md#10-verifying-your-connection).

### Install and upgrade issues

If `mm --version` is older than the latest release, re-run the installation
command with `--refresh`. `mm upgrade` automates this only for `uv tool`
installs; pipx, project, and source installs use their own package-manager
workflow. Detailed recovery commands live in
[Data, config & CLI](reference/data-config-cli.md#cli-reference).

## Optional: Share rules, skills, sub-agents, and commands across editors

The [Context Gateway](context-gateway.md) keeps canonical artifacts in a
project or user Store and syncs them to supported runtimes. Its Wiki is a
separate, optional upstream library—not the Store itself.

## Optional: STM Proxy — Proactive Memory Surfacing

[memtomem-stm](https://github.com/memtomem/memtomem-stm) is a separate MCP
proxy for automatic surfacing, compression, and caching. memtomem core remains
the long-term-memory store and works without STM.

## Optional: Web UI

```bash
mm web
```

Open `http://127.0.0.1:8080`. Use `mm web --dev` only when you need maintainer
pages. See [Operations → Web UI](reference/operations.md#web-ui) before any
off-loopback exposure.

## Optional: LLM Provider

Basic indexing and search do not require an LLM. Configure one only for
features such as query expansion, extraction, auto-tagging, and richer
summaries. See [LLM Providers](llm-providers.md).

## Uninstall

Use `mm uninstall` for guided removal. The dedicated
[Uninstalling memtomem](uninstall.md) guide explains how to preserve the
database or remove client registrations and project artifacts.

## Next steps

- [한국어 바이브코딩 빠른 시작](vibe-coding-getting-started-ko.md) — Claude Code·Codex CLI 플러그인으로 첫 기억을 저장·검색하세요.
- [MCP Client Setup](mcp-clients.md) — connect an editor.
- [Core memory tools](reference/core-memory-tools.md) — index and search real data.
- [Embeddings](embeddings.md) — improve semantic search quality.
- [Context Gateway](context-gateway.md) — push agent artifacts.
- [Operations & troubleshooting](reference/operations.md) — diagnose and audit.
