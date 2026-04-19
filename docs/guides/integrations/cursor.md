# Cursor x memtomem Integration Guide

**Audience**: Developers using Cursor who want to enhance project knowledge search with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Cursor installed
**Estimated Time**: About 10 minutes

---

## Overview

Cursor has its own built-in Memory system and Rules files.
memtomem **does not replace** these, but complements Cursor with **hybrid semantic search** features it lacks.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| Cross-session fact storage (simple key-value) | Cursor Memory |
| Rules file context | Cursor Rules (`.cursorrules`) |
| Project document semantic search | memtomem (`mem_search`) |
| Code pattern indexing (Markdown, JSON/YAML/TOML, Python/JS/TS) | memtomem (`mem_index`) |

---

## Step 1: MCP Registration

### Global Configuration (Applies to All Projects)

Create or edit the `~/.cursor/mcp.json` file:

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

Restart Cursor after configuring.

> **If you already have `mcpServers`**: Simply add the `"memtomem"` key to the existing object.

---

## Step 2: Verify Connection

In Cursor's AI chat:

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

## Tool Usage Guidelines (Add to `.cursorrules`)

Adding the following to the `.cursorrules` file in the project root helps the Cursor agent
distinguish between the two memory systems:

```markdown
## Memory Tool Usage Guidelines

### Cursor Built-in Memory
- This project's coding conventions, style guides
- Simple facts the user asked to "remember"
- Current session context

### memtomem (`mem_search`, `mem_index`, `mem_recall`)
- Project ADRs, architecture documents, team onboarding materials
- Code pattern history, debugging records, decision records
- Long-term knowledge base managed as markdown files

### Principles
- "A fact to remember in this project" → Cursor Memory
- "Something to find in documents or code patterns" → `mem_search`
- When in doubt, call both tools and combine the results in your response

### Dual Memory Search
When users ask about past records ("previously", "what was decided", "what was it" etc.):
1. Check Cursor Memory first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Project Onboarding -- ADR/Architecture Document Search

When a new team member joins the project:

```
User: "Explain our team's authentication architecture"

Agent:
1. mem_search("authentication architecture") → Returns chunks from docs/adr/003-auth-architecture.md
→ "According to ADR-003, JWT + Refresh Token approach was adopted.
   Reason: Need for stateless communication between microservices..."
```

### Scenario B: Rules + memtomem Integration

By setting memtomem search as a precondition in `.cursorrules`,
the agent automatically references relevant documents before writing code:

```
User: "Create a new API endpoint"

Agent:
1. Following .cursorrules guidelines, auto-calls mem_search("API design guidelines")
2. → Returns API convention documents (RESTful rules, error format, etc.)
3. Generates code according to the guidelines
```

### Scenario C: Code Refactoring -- Search Related Patterns Before Changes

```
User: "Refactor the DB connection pooling code"

Agent:
1. mem_search("DB connection pooling pattern") → Returns previous decision records
2. mem_search("connection pool configuration") → Returns configuration documents
→ "There was a previous decision to switch from HikariCP to asyncpg.
   There is a max_connections=20 constraint, so the refactoring will maintain this."
```

---

## Built-in Memory vs memtomem Comparison

| Feature | Cursor Built-in | memtomem |
|---------|----------------|---------|
| Hybrid search (BM25 + Dense) | None | BM25 + Dense + RRF fusion |
| Markdown indexing | None | Heading-based semantic chunker |

---

## Frequently Asked Questions

**Q: Does Cursor's built-in Memory go away?**
No. memtomem uses a separate tool name `mem_search`, so both systems coexist independently.

**Q: Should I configure in `.cursor/mcp.json` or `~/.cursor/mcp.json`?**
Use per-project `.cursor/mcp.json` if you need different settings per project (e.g., memory directories). Use global `~/.cursor/mcp.json` to apply the same settings across all projects.

**Q: Don't Cursor's code indexing and memtomem indexing overlap?**
They serve different purposes. Cursor's indexing is for code autocompletion, while memtomem handles semantic search of project documents, ADRs, and decision records.

---

## Next Steps

- [Reference](../reference.md) — Complete feature reference
- [Configuration](../configuration.md) — All `MEMTOMEM_*` environment variables
