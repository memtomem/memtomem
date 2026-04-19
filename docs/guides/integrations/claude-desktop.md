# Claude Desktop x memtomem Integration Guide

**Audience**: Users of Claude Desktop who want to build a personal knowledge base with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Claude Desktop installed
**Estimated Time**: About 10 minutes

---

## Overview

Claude Desktop is Anthropic's conversational AI assistant app
that can connect external tools through MCP servers.
Connecting memtomem lets you turn personal notes, research materials, and documents into a **semantically searchable knowledge base**.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| Conversational AI assistant | Claude Desktop built-in |
| Personal knowledge base search | memtomem (`mem_search`) |
| Note/document indexing | memtomem (`mem_index`) |
| Memory management | memtomem MCP tools |
| Code pattern indexing (Markdown, JSON/YAML/TOML, Python/JS/TS) | memtomem (`mem_index`) |

---

## Step 1: MCP Registration

### macOS

Edit the `~/Library/Application Support/Claude/claude_desktop_config.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      
      "env": {
        "MEMTOMEM_STORAGE__SQLITE_PATH": "~/.memtomem/memtomem.db",
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/notes\"]"
      }
    }
  }
}
```

### Windows

Edit the `%APPDATA%\Claude\claude_desktop_config.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      
      "env": {
        "MEMTOMEM_STORAGE__SQLITE_PATH": "%USERPROFILE%\\.memtomem\\memtomem.db",
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"%USERPROFILE%\\\\notes\"]"
      }
    }
  }
}
```

Restart Claude Desktop after configuring.

> **If you already have `mcpServers`**: Simply add the `"memtomem"` key to the existing object.

---

## Step 2: Verify Connection

In Claude Desktop chat:

```
Call the mem_status tool and tell me the memtomem connection status
```

Example of a successful response:
```
memtomem status:
  - Storage backend: SQLite
  - Total chunks: 0 (not yet indexed)
  - Embedding model: ollama/nomic-embed-text
```

> **If tools are not visible**: Fully quit Claude Desktop (Cmd+Q / Alt+F4) and relaunch. Settings may not be reflected if background processes remain.

---

## Step 3: First Indexing

```
Index my ~/notes directory
```

Agent:
```
mem_index(path="~/notes", recursive=True)
→ Indexing complete:
  - Files scanned: 47
  - Total chunks: 1284
  - Indexed: 1284
  - Skipped (unchanged): 0
  - Deleted (stale): 0
  - Duration: 3200ms
```

---

## Tool Usage Guidelines

You can guide the role in Claude Desktop conversations as follows:

```
Follow these rules:
- When something needs to be found in my notes or documents → use the mem_search tool
- When saving a new memo → use the mem_add tool
- When checking recently added records → use the mem_recall tool
- When asking about past records ("previously", "what was it" etc.) → check conversation history first, if not found use mem_search for semantic search, and synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Personal Note/Memo Search

Search notes managed as markdown during conversations.

```
User: "Find the budget-related content from last month's meeting notes"

Agent:
mem_search("meeting budget")
→ Returns chunks from notes/2026-02-meeting.md
→ "Found it in the February 15 meeting notes: Q2 budget is ..."
```

### Scenario B: Research Material Management

Organize papers or articles as markdown and search them.

```
User: "Do I have notes about the Transformer attention mechanism?"

Agent:
mem_search("Transformer attention mechanism")
→ Returns chunks from notes/research/transformer-notes.md
→ "Found related notes: Self-attention is ..."
```

---

# Claude Desktop x memtomem Integration Guide > ## Built-in Features vs memtomem Comparison

| Feature | Claude Desktop Built-in | memtomem |
|---------|------------------------|---------|
| Conversational AI | **Built-in** (Claude model) | None (complementary area) |
| Hybrid search (BM25 + Dense) | None | BM25 + Dense + RRF fusion |
| Markdown indexing | None | Heading-based semantic chunker |
## Frequently Asked Questions

**Q: Does this reduce Claude Desktop's own features?**
No. memtomem operates as separate MCP tools and does not affect Claude Desktop's existing functionality.

**Q: Do I need to install Ollama locally?**
The default embedding provider is Ollama (local, free). OpenAI is also supported as an alternative — set `MEMTOMEM_EMBEDDING__PROVIDER=openai` with an API key. If using Ollama, it must be installed locally.

**Q: Are indexed notes sent to Anthropic's servers?**
No. memtomem runs locally, and data is stored only in the local SQLite DB. Ollama runs locally, so no data is sent externally.

**Q: Does it work on Windows?**
Yes. It works on Windows as long as the `memtomem-server` command can be executed. Just change the configuration file path to `%APPDATA%\Claude\claude_desktop_config.json`.

---

## Next Steps

- [Reference](../reference.md) — Complete feature reference
- [Configuration](../configuration.md) — All `MEMTOMEM_*` environment variables
