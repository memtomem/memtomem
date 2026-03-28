# Hands-On Tutorial: Building Memory for Your AI Agent

**Audience**: First-time memtomem users
**Estimated Time**: ~15 minutes
**Outcome**: An AI memory system that indexes markdown notes and searches them

---

## Prerequisites

| Requirement | How to Verify |
|-------------|---------------|
| Ollama | `ollama list` |
| MCP client (Claude Code, Cursor, Windsurf, etc.) | One is enough |

---

## Step 1 — Setup and First Run (5 min)

### 1.1 Prepare the Embedding Model

```bash
ollama pull nomic-embed-text     # English-primary (768d, 270MB)
# or: ollama pull bge-m3         # Multilingual recommended (1024d, 1.2GB)
```

### 1.2 Connect the MCP Server

No installation needed — `uvx` handles everything automatically.

Add the following to your editor's MCP configuration file (`.mcp.json`):

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"]
    }
  }
}
```

Or for Claude Code:
```bash
# PyPI
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

> **Note**: MCP clients run `memtomem-server` via `uvx`. `memtomem` (the CLI) is for terminal commands only.

### 1.3 First Tool Call

Call `mem_status` from your MCP client to check the system status.

```
> mem_status
```

Example response:
```
memtomem v0.1.0 (or later)
Storage: sqlite (~/.memtomem/memtomem.db)
Embedding: ollama/nomic-embed-text (768d)
Chunks: 0 | Sources: 0
```

No memories yet. Let's populate them in the next step.

---

## Step 2 — First Memory Storage and Search (5 min)

### 2.1 Write Study Notes

First, create a few markdown files to index.

```bash
mkdir -p ~/my-notes
```

**File 1: `~/my-notes/python-basics.md`**

```markdown
# Python Basics

## Variables and Types
Python is a dynamically typed language. You assign variables without type declarations, like `x = 42`.
Basic types: `int`, `float`, `str`, `bool`, `list`, `dict`, `tuple`, `set`

## List Comprehensions
`[x**2 for x in range(10)]` generates a list of squares from 0 to 9.
Conditional filtering: `[x for x in range(20) if x % 3 == 0]`
```

**File 2: `~/my-notes/git-workflow.md`**

```markdown
# Git Workflow

## Branch Strategy
- `main`: Production branch. Always kept in a deployable state
- `feature/*`: Feature development branches. Forked from main and merged via PR

## Frequently Used Commands
- `git stash`: Temporarily save changes. Restore with `git stash pop`
- `git rebase -i`: Clean up commit history. Use squash, fixup
- `git cherry-pick <hash>`: Apply a specific commit to the current branch
```

**File 3: `~/my-notes/docker-notes.md`**

```markdown
# Docker Core Concepts

## Images vs Containers
- Image: A read-only template. Built with a Dockerfile
- Container: A running instance of an image. An isolated process

## Frequently Used Commands
- `docker build -t myapp .`: Build an image from a Dockerfile
- `docker run -p 8080:80 myapp`: Run a container with port mapping
- `docker compose up -d`: Run a multi-container app in the background
```

### 2.2 Indexing

Index the notes you wrote into memtomem.

```
> mem_index path="~/my-notes"
```

Example response:
```
Indexed 3 files (9 chunks created, 0 updated, 0 deleted)
  python-basics.md: 3 chunks
  git-workflow.md: 3 chunks
  docker-notes.md: 3 chunks
```

memtomem recognizes the markdown heading (`##`) structure and splits content into semantically meaningful chunks.

### 2.3 Search

```
> mem_search query="list comprehensions"
```

Example response:
```
Found 3 results:

1. [0.85] python-basics.md — "List Comprehensions"
   [x**2 for x in range(10)] generates a list of squares from 0 to 9...

2. [0.42] python-basics.md — "Variables and Types"
   Basic types: int, float, str, bool, list, dict...

3. [0.31] docker-notes.md — "Frequently Used Commands"
   docker run -p 8080:80 myapp...
```

Interpreting results:
- **Score** `[0.85]`: Relevance between 0 and 1 — fuses BM25 (keyword) and Dense (semantic) search via RRF
- **Source**: Shows which file and section the result came from
- **Ranking**: Most relevant results appear first

### 2.4 Recall by Date

`mem_recall` retrieves memories by date range and source filters (no search query needed).

```
> mem_recall source_filter="git-workflow.md"
```

---

## Step 3 — Memory Management: Add, Edit, Delete (5 min)

### 3.1 Quick Note Addition

You can add memories directly with `mem_add` without creating files manually.

```
> mem_add content="Python 3.12 introduced the type statement (PEP 695). You can declare type aliases like `type Vector = list[float]`." tags="python,typing"
```

Example response:
```
Added 1 chunk (saved to ~/.memtomem/memories/20260319_143022.md)
Tags: python, typing
```

> `mem_add` automatically creates a markdown file in the `~/.memtomem/memories/` directory and indexes it.

### 3.2 Adding Multiple Notes at Once

```
> mem_batch_add entries=[{"key": "FastAPI", "value": "FastAPI is a framework that adds Pydantic validation on top of Starlette", "tags": ["python", "web"]}, {"key": "uvicorn", "value": "uvicorn is an ASGI server. Use the --reload option for auto-restart during development", "tags": ["python", "web"]}]
```

### 3.3 Editing an Existing Chunk

Use the chunk ID from search results to edit the content.

```
> mem_search query="docker compose"
```

After finding the chunk ID (e.g., `abc123`) in the results:

```
> mem_edit chunk_id="abc123" new_content="docker compose up -d: Run a multi-container app in the background. Since v2, use docker compose (with a space) instead of docker-compose (with a hyphen)."
```

### 3.4 Deleting Unnecessary Chunks

```
> mem_delete chunk_id="abc123"
```

### 3.5 Checking Status

```
> mem_stats
```

Example response:
```
Chunks: 12 | Sources: 4 | Storage: sqlite
```

---

## CLI Equivalents (optional)

If you install the CLI (`uv tool install memtomem`, or `uv run mm ...` from a git clone), you can also use these from the terminal. `mm` is a shorthand alias for `memtomem`.

| MCP Tool | CLI Command |
|----------|-------------|
| `mem_search` | `mm search "query"` |
| `mem_index` | `mm index ~/my-notes` |
| `mem_add` | `mm add "note content"` |
| `mem_recall` | `mm recall --since 2026-03-01` |
| `mem_status` | (no CLI equivalent) |
| `mem_config` | `mm config` |

---

## MCP Tools Used in This Tutorial

| Tool | Purpose |
|------|---------|
| `mem_status` | Check system status |
| `mem_index` | Index markdown files |
| `mem_search` | Hybrid search (BM25 + Dense) |
| `mem_recall` | Date/source-based memory retrieval |
| `mem_add` | Quick note addition |
| `mem_batch_add` | Add multiple notes at once |
| `mem_edit` | Edit chunk content |
| `mem_delete` | Delete chunks |
| `mem_stats` | Overall status (chunk count, source count) |

---

## Next Steps

- [User Guide](user-guide.md) — Complete feature walkthrough
- [Practical Use Cases](use-cases.md) — Agent workflow scenarios
- [MCP Client Configuration](mcp-clients.md) — Editor-specific setup
