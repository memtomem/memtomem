# Windsurf x memtomem Integration Guide

**Audience**: Developers using Windsurf who want to enhance project knowledge search with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Windsurf installed
**Estimated Time**: About 10 minutes

---

## Overview

Windsurf has built-in Cascade Flow (IDE action tracking) and its own Memory system.
memtomem **does not replace** these, but complements areas Cascade Flow does not cover: **semantic search of accumulated project knowledge**.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| IDE action tracking (file open/edit/run) | Cascade Flow |
| Auto/manual fact storage | Windsurf Memory |
| Project document semantic search | memtomem (`mem_search`) |
| Code pattern indexing (Markdown, JSON/YAML/TOML, Python/JS/TS) | memtomem (`mem_index`) |

---

## Step 1: MCP Registration

### Basic Configuration

Create or edit the `~/.codeium/windsurf/mcp_config.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      
      "env": {
        "MEMTOMEM_STORAGE__SQLITE_PATH": "~/.memtomem/memtomem.db",
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "~/notes"
      }
    }
  }
}
```

Restart Windsurf after configuring.

> **If you already have `mcpServers`**: Simply add the `"memtomem"` key to the existing object.

---

## Step 2: Verify Connection

In Windsurf's Cascade chat:

```
Call mem_status and tell me the memtomem connection status
```

Example of a successful response:
```
memtomem status:
  - Storage backend: SQLite
  - Total chunks: 0 (not yet indexed)
  - Embedding model: ollama/nomic-embed-text
```

---

## Step 3: First Indexing

```
Index my project docs directory
```

Agent:
```
mem_index(path="./docs", recursive=True)
→ "Indexed 23 files, 567 chunks in 1.8s"
```

---

## Tool Usage Guidelines (Add to Windsurf Rules)

Adding the following to Windsurf's Rules settings helps Cascade distinguish between the two memory systems:

```markdown
## Memory Tool Usage Guidelines

### Windsurf Built-in Memory
- IDE action history tracked by Cascade Flow
- Facts and preferences saved by the user
- Current session context

### memtomem (`mem_search`, `mem_index`, `mem_recall`)
- Project ADRs, architecture documents, team onboarding materials
- Code pattern history, debugging records
- Long-term knowledge base managed as markdown files

### Principles
- "Related to the file just edited" → Cascade Flow
- "Something to find in documents or code patterns" → `mem_search`
- When in doubt, call both tools and combine the results in your response

### Dual Memory Search
When users ask about past records ("previously", "what was decided", "what was it" etc.):
1. Check Windsurf Memory first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Project Document Search -- Areas Cascade Flow Doesn't Cover

Cascade Flow tracks file editing history in the current session,
but does not perform semantic search across all project documents.

```
User: "Tell me about our team's deployment procedure"

Agent:
1. Cascade Flow → No deployment-related work in current session
2. mem_search("deployment procedure") → Returns chunks from docs/deploy-guide.md
→ "Here's the deployment procedure found in team documents: ..."
```

---

## Built-in Memory vs memtomem Comparison

| Feature | Windsurf Built-in | memtomem |
|---------|-------------------|---------|
| Hybrid search (BM25 + Dense) | None | BM25 + Dense + RRF fusion |
| Cascade Flow action tracking | **Built-in** | None (complementary area) |
| Markdown indexing | None | Heading-based semantic chunker |

---

## Frequently Asked Questions

**Q: Does Cascade Flow go away?**
No. memtomem operates independently of Cascade Flow. Cascade tracks IDE actions, while memtomem searches project documents.

**Q: Does the same content get stored in both Windsurf Memory and memtomem?**
They serve different purposes. Windsurf Memory stores facts automatically extracted from conversations, while memtomem only handles explicitly indexed files.

---

## Next Steps

- [User Guide](../user-guide.md) — Complete feature walkthrough
- [Practical Use Cases](../use-cases.md) — Agent workflow scenarios
