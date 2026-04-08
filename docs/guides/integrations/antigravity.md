# Antigravity x memtomem Integration Guide

**Audience**: Developers using Google Antigravity IDE who want to add project knowledge search with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Antigravity installed
**Estimated Time**: About 10 minutes

---

## Overview

Antigravity is Google's AI-native IDE with a built-in context system.
memtomem complements Antigravity with **hybrid semantic search** features it lacks.

> **Note**: Antigravity uses GUI-based MCP configuration. While you can also edit configuration files directly like other editors, GUI-based configuration is recommended.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| IDE default context | Antigravity built-in |
| Project document semantic search | memtomem (`mem_search`) |
| Code pattern indexing (Markdown, JSON/YAML/TOML, Python/JS/TS) | memtomem (`mem_index`) |

---

## Step 1: MCP Registration

### Add via GUI (Recommended)

1. Click `…` menu at the top of the Agent panel → **MCP Servers**
2. Click **Manage MCP Servers** at the top of the MCP Store
3. Select **View raw config** → `mcp_config.json` opens
4. Add the memtomem server configuration:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      
      "env": {
        "MEMTOMEM_STORAGE__SQLITE_PATH": "/Users/your-name/.memtomem/memtomem.db",
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "/Users/your-name/notes"
      }
    }
  }
}
```

5. Save the file and restart the Agent session

> **Absolute paths required**: Antigravity does not recognize `~` or `$HOME`. Always use absolute paths in the format `/Users/your-name/...`.

> **`${workspaceFolder}` not supported**: Unlike other IDEs, this variable is not supported. Use absolute paths.

---

## Step 2: Verify Connection

In Antigravity's Agent chat:

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

> **If connection fails**: Completely terminate the Agent session and start again. MCP configuration changes require a session restart.

---

## Step 3: First Indexing

```
Index my project docs directory
```

Agent:
```
mem_index(path="/Users/your-name/project/docs", recursive=True)
→ "Indexed 23 files, 567 chunks in 1.8s"
```

> **Path note**: Using absolute paths for indexing paths is recommended for reliability.

---

## Tool Usage Guidelines

You can provide the following guidelines to the Antigravity agent:

```markdown
## Memory Tool Usage Guidelines

### memtomem (`mem_search`, `mem_index`, `mem_recall`)
- Search project ADRs, architecture documents, team onboarding materials
- Code pattern history, debugging records
- Time-range queries (`mem_recall`)

### Principles
- Something to find in project documents → `mem_search`
- When recording new knowledge → `mem_add`
- Check recent records → `mem_recall`

### Dual Memory Search
When users ask about past records ("previously", "what was decided", "what was it" etc.):
1. Check IDE built-in context first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Project Document Indexing + Search

```
User: "Find our team's coding convention document"

Agent:
mem_search("coding conventions style guide")
→ Returns chunks from docs/coding-conventions.md
→ "According to the team conventions, Python requires Black formatter and mandatory type hints."
```

### Scenario B: Absolute Path Troubleshooting

Path issues are the most common error:

```
# Incorrect — Not recognized by Antigravity
mem_index(path="~/notes")        # ❌ ~ not supported
mem_index(path="$HOME/notes")    # ❌ Environment variable not expanded

# Correct
mem_index(path="/Users/your-name/notes")  # ✅ Absolute path
```

---

## Built-in Features vs memtomem Comparison

| Feature | Antigravity Built-in | memtomem |
|---------|---------------------|---------|
| Code autocompletion/generation | **Built-in** | None (complementary area) |
| Hybrid search (BM25 + Dense) | None | BM25 + Dense + RRF fusion |
| Markdown indexing | None | Heading-based semantic chunker |

---

## Frequently Asked Questions

**Q: I changed MCP settings in the GUI but it's not reflected.**
Completely terminate the Agent session and start again. MCP server configuration changes require a session restart.

**Q: Why can't I use `~` paths?**
Antigravity's MCP execution environment does not load shell profiles, so `~` expansion does not work. Always use absolute paths in the format `/Users/your-name/...`.

**Q: Can I share the same DB with other editors (Cursor, Claude Code, etc.)?**
Yes. By specifying the same `MEMTOMEM_STORAGE__SQLITE_PATH` with absolute paths, multiple editors can access the same memory.

---

## Next Steps

- [User Guide](../user-guide.md) — Complete feature walkthrough
- [Practical Use Cases](../use-cases.md) — Agent workflow scenarios
