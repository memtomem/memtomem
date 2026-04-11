# memtomem

[![PyPI](https://img.shields.io/pypi/v/memtomem)](https://pypi.org/project/memtomem/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-green)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![CLA](https://img.shields.io/badge/CLA-required-green)](CLA.md)

**Give your AI agent a long-term memory.**

memtomem turns your markdown notes, documents, and code into a searchable knowledge base that any AI coding agent can use. Write notes as plain `.md` files — memtomem indexes them and makes them searchable by both keywords and meaning.

```mermaid
flowchart LR
    A["Your files\n.md .json .py"] -->|Index| B["memtomem"]
    B -->|Search| C["AI agent\n(Claude Code, Cursor, etc.)"]
```

> **First time here?** Follow the [Getting Started](docs/guides/getting-started.md) guide — you'll have a working setup in under 5 minutes.

---

## Why memtomem?

| Problem | How memtomem solves it |
|---------|------------------------|
| AI forgets everything between sessions | Index your notes once, search them in every session |
| Keyword search misses related content | Hybrid search: exact keywords + meaning-based similarity |
| Notes scattered across tools | One searchable index for markdown, JSON, YAML, Python, JS/TS |
| Vendor lock-in | Your `.md` files are the source of truth. The DB is a rebuildable cache |

---

## Quick Start

### 1. Install

```bash
ollama pull nomic-embed-text          # local embeddings (~270MB, free)
uv tool install memtomem             # or: pipx install memtomem
```

> No GPU? Pick OpenAI in the wizard — see [Embeddings](docs/guides/embeddings.md).

### 2. Setup

```bash
mm init                               # 7-step wizard (or: mm init -y for CI)
```

The wizard picks your embedding model, points at the folder you want indexed, and registers memtomem with your AI editor.

### 3. Use

```
"Call the mem_status tool"   →  confirms the server is connected
"Index my notes folder"      →  mem_index(path="~/notes")
"Search for deployment"      →  mem_search(query="deployment checklist")
"Remember this insight"      →  mem_add(content="...", tags="ops")
```

<details>
<summary><b>Other install options</b></summary>

**Project-scoped** (per-project isolation):
```bash
uv add memtomem && uv run mm init    # all commands need `uv run` prefix
```

**No install** (uvx on demand):
```bash
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server
```

See [MCP Client Setup](docs/guides/mcp-clients.md) for Cursor / Windsurf / Claude Desktop / Gemini CLI.

</details>

---

## Key Features

- **Hybrid search** — BM25 keyword + dense vector + RRF fusion in one query
- **Semantic chunking** — heading-aware Markdown, AST-based Python, tree-sitter JS/TS, structure-aware JSON/YAML/TOML
- **Incremental indexing** — chunk-level SHA-256 diff; only changed chunks get re-embedded
- **Namespaces** — organize memories into scoped groups with auto-derivation from folder names
- **Maintenance** — near-duplicate detection, time-based decay, TTL expiration, auto-tagging
- **Web UI** — visual dashboard for search, sources, tags, sessions, health monitoring
- **72 MCP tools** — `mem_do` meta-tool routes 64 actions in `core` mode for minimal context usage

---

## Ecosystem

| Package | Description |
|---------|-------------|
| [**memtomem**](https://pypi.org/project/memtomem/) | Core — MCP server, CLI, Web UI, hybrid search, storage |
| [**memtomem-stm**](https://github.com/memtomem/memtomem-stm) | STM proxy — proactive memory surfacing via tool interception |

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/guides/getting-started.md) | Install, setup wizard, first use |
| [Hands-On Tutorial](docs/guides/hands-on-tutorial.md) | Follow-along with example files |
| [Interactive Notebooks](examples/notebooks/) | Jupyter notebooks for the Python API — hello, indexing, sessions, tuning, LangGraph |
| [User Guide](docs/guides/user-guide.md) | Complete feature walkthrough |
| [Configuration](docs/guides/configuration.md) | All `MEMTOMEM_*` environment variables |
| [Embeddings](docs/guides/embeddings.md) | Ollama and OpenAI providers |
| [MCP Client Setup](docs/guides/mcp-clients.md) | Editor-specific configuration |
| [Agent Memory Guide](docs/guides/agent-memory-guide.md) | Sessions, working memory, procedures |
| [Web UI](docs/guides/web-ui.md) | Visual dashboard |
| [Hooks](docs/guides/hooks.md) | Claude Code hooks for auto-indexing |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and the contributor guide.

## License

[Apache License 2.0](LICENSE). Contributions are accepted under the terms of the [Contributor License Agreement](CLA.md).
