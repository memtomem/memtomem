# Codex x memtomem Integration Guide

The native Codex plugin bundles an exact-pinned memtomem MCP server and six
skills. BM25 is enabled by default; an embedding provider is optional.

Prefer a short Korean first-success path? Follow the [Claude Code·Codex CLI
vibe-coding quickstart](../vibe-coding-getting-started-ko.md), then return here
for the complete skill reference.

## Install

From the GitHub marketplace:

```bash
codex plugin marketplace add memtomem/memtomem
codex plugin add memtomem@memtomem
```

For local marketplace development, replace `memtomem/memtomem` with the path
to the checkout:

```bash
codex plugin marketplace add /path/to/memtomem
codex plugin add memtomem@memtomem
```

Start a new Codex thread after installation so the marketplace snapshot,
skills, and MCP server are reloaded.

## Included skills

| Skill | Behavior | Invocation policy |
|---|---|---|
| `memtomem-search` | Topic search with `mem_search` | May be selected implicitly |
| `memtomem-recall` | Date-range recall with `mem_recall` | May be selected implicitly |
| `memtomem-status` | Configuration and index health | May be selected implicitly |
| `memtomem-remember` | Persist a user-requested memory | Explicit only |
| `memtomem-index` | Index a selected path | Explicit only |
| `memtomem-setup` | Status → explicit path → index → search | Explicit only |

The plugin exposes memtomem's nine core MCP tools. The shipped skills do not
use the broad `mem_do` gateway.

## Verify

Ask Codex:

```text
Use $memtomem-status to inspect my memory index.
```

For MCP-only registration or advanced timeout and parallel-call settings, see
the [MCP clients guide](../mcp-clients.md#7-codex-cli).
