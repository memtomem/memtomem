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

### 1.1 Pick an Embedding Path (optional)

The MCP server runs out of the box in **keyword-only (BM25)** mode — no
embedding model needed for this tutorial. If you want semantic search as
well, pick one of the following before continuing:

```bash
# Local dense embeddings, no server — add the onnx extra and memtomem
# will download the model on first index.
uv tool install 'memtomem[onnx]'

# Local server — requires Ollama installed (ollama.com)
ollama pull nomic-embed-text     # English-primary (768d, 270MB)
# or: ollama pull bge-m3         # Multilingual recommended (1024d, 1.2GB)

# Cloud — set OPENAI_API_KEY in the env passed to the MCP server.
```

The wizard (`mm init`) writes the chosen provider into
`~/.memtomem/config.json`; for this tutorial you can also leave the
default and skip ahead. The wizard also offers an optional **reranker**
step right after embedding — press Enter to skip, or opt in and pick the
multilingual model if you work with Korean/Chinese/Japanese content.

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

> **Tool mode**: By default the server exposes 9 tools (8 core +
> `mem_do`), which routes to every other action via
> `mem_do(action="...", params={...})`. This tutorial uses the default
> `core` mode and shows the `mem_do` form for non-core actions like
> `edit` / `delete` / `batch_add` / `orphans`. If you prefer calling
> them as top-level tools, add `"env": {"MEMTOMEM_TOOL_MODE": "standard"}`
> (32 tools) or `"full"` (all 74) to the MCP server entry above.

### 1.3 First Tool Call

Ask your agent to check the memtomem status.

**You**: "What's the status of my memtomem?"

Claude calls `mem_status()` and returns:

```
memtomem Status
==============
Storage:   sqlite
DB path:   ~/.memtomem/memtomem.db
Embedding: ollama / nomic-embed-text
Dimension: 768
Top-K:     10
RRF k:     60

Index stats
-----------
Total chunks:  0
Source files:  0
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

Ask the agent to index the notes you just wrote.

**You**: "Index my markdown notes in ~/my-notes"

Claude calls `mem_index(path="~/my-notes")` and returns:

```
Indexed 3 files (9 chunks created, 0 updated, 0 deleted)
  python-basics.md: 3 chunks
  git-workflow.md: 3 chunks
  docker-notes.md: 3 chunks
```

memtomem recognizes the markdown heading (`##`) structure and splits content into semantically meaningful chunks.

### 2.3 Search

**You**: "Search my notes for list comprehensions"

Claude calls `mem_search(query="list comprehensions")` and returns:

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

**You**: "Show me everything from my git-workflow notes"

Claude calls `mem_recall(source_filter="git-workflow.md")` and returns the matching chunks.

---

## Step 3 — Memory Management: Add, Edit, Delete (5 min)

### 3.1 Quick Note Addition

You can add memories directly with `mem_add` without creating files manually.

**You**: "Remember this: Python 3.12 introduced the type statement (PEP 695). You can declare type aliases like `type Vector = list[float]`. Tag it with python and typing."

Claude calls `mem_add(content="Python 3.12 introduced the type statement (PEP 695). You can declare type aliases like \`type Vector = list[float]\`.", tags=["python", "typing"])` and returns:

```
Memory added to ~/.memtomem/memories/2026-04-11.md
- Chunks indexed: 1
- File: ~/.memtomem/memories/2026-04-11.md
```

> `mem_add` automatically creates a markdown file in the `~/.memtomem/memories/` directory (one per UTC date by default) and re-indexes it so the new memory is immediately searchable.

### 3.2 Adding Multiple Notes at Once

`batch_add` is a non-core action, so in the default tool mode it is
routed through `mem_do`.

**You**: "Save two python web notes — FastAPI: a framework that adds Pydantic validation on top of Starlette. uvicorn: an ASGI server; use --reload for auto-restart during development."

Claude calls:

```python
mem_do(
  action="batch_add",
  params={"entries": [
    {"key": "FastAPI", "value": "FastAPI is a framework that adds Pydantic validation on top of Starlette", "tags": ["python", "web"]},
    {"key": "uvicorn", "value": "uvicorn is an ASGI server. Use the --reload option for auto-restart during development", "tags": ["python", "web"]},
  ]},
)
```

Each entry takes `key` (title), `value` (content), and optional `tags`.
All entries in one call are appended to the same markdown file and
indexed in a single pass.

### 3.3 Editing an Existing Chunk

Use the chunk ID from search results to edit the content. Like
`batch_add`, `edit` is a non-core action routed through `mem_do`.

**You**: "Find the docker compose note."

Claude calls `mem_search(query="docker compose")` and shows the matching chunks with their IDs.

**You** (after spotting chunk `abc123`): "Update that chunk — note that since v2 it's `docker compose` with a space, not `docker-compose` with a hyphen."

Claude calls:

```python
mem_do(
  action="edit",
  params={
    "chunk_id": "abc123",
    "new_content": "docker compose up -d: Run a multi-container app in the background. Since v2, use docker compose (with a space) instead of docker-compose (with a hyphen).",
  },
)
```

Under the hood `edit` rewrites the corresponding line range in the
source markdown file and then re-indexes that file with `force=true`,
so the change is immediately searchable and rolls back cleanly if
indexing fails.

### 3.4 Deleting Unnecessary Chunks

**You**: "Delete chunk abc123."

Claude calls `mem_do(action="delete", params={"chunk_id": "abc123"})`.

`delete` can also remove every chunk from a given source file or
namespace — pass `{"source_file": "~/my-notes/old.md"}` or
`{"namespace": "scratch"}` instead of `chunk_id`. When called with a
`chunk_id` it strips the matching line range from the markdown file and
re-indexes (same rollback semantics as `edit`); the source-file and
namespace variants only touch the index, not the files on disk.

### 3.5 Checking Status

**You**: "How many memories do I have?"

Claude calls `mem_stats()` and returns:

```
Memory index statistics:
- Total chunks: 12
- Total sources: 4
- Storage backend: sqlite
```

### 3.6 Editing Files Directly and Re-Indexing

When you edit a markdown file outside memtomem (in your editor, from
a git pull, whatever), re-index the same path so memtomem catches up.

**You**: "Re-index my notes folder."

Claude calls `mem_index(path="~/my-notes")` and returns (after editing one section in `git-workflow.md`):

```
Indexing complete:
- Files scanned: 3
- Total chunks: 10
- Indexed: 2
- Skipped (unchanged): 8
- Deleted (stale): 1
- Duration: 85ms
```

How to read the stats:

- **Indexed** — chunks whose content hash is new (either brand-new
  sections *or* sections whose text changed and now have a different
  hash). Only these hit the embedder.
- **Skipped (unchanged)** — hash matched a chunk already in the
  database, no embedding call made.
- **Deleted (stale)** — a chunk that used to exist in the file but is
  no longer produced. An edited section contributes *both* an Indexed
  row (new hash) and a Deleted row (old hash), because the diff is
  hash-based.

If you swap the embedding model (e.g., `nomic-embed-text` → `bge-m3`)
and need every chunk re-embedded from scratch, ask for a forced re-index.

**You**: "Force a full re-index of my notes folder."

Claude calls `mem_index(path="~/my-notes", force=true)`, and every chunk shows up in `Indexed` regardless of hash match.

### 3.7 Cleaning Up After Deleted Files

If you delete a markdown file from disk, `mem_index` will *not* notice
the deletion — it only walks files that currently exist, so the old
chunks stay in the database as "orphans". `mem_status` detects this
automatically and tells you to clean up.

**You**: "Check the memtomem status."

Claude calls `mem_status()` and returns:

```
memtomem Status
==============
Storage:   sqlite
DB path:   ~/.memtomem/memtomem.db
Embedding: ollama / nomic-embed-text
Dimension: 768
Top-K:     10
RRF k:     60

Index stats
-----------
Total chunks:  11
Source files:  4 (1 orphaned — run mem_cleanup_orphans)
```

Call `orphans` through `mem_do` — dry-run first, then apply.

**You**: "Preview the orphan cleanup (dry run)."

Claude calls `mem_do(action="orphans", params={"dry_run": true})` and returns:

```
Orphaned files: 1 (dry-run, no deletions)
- ~/my-notes/docker-notes.md
```

Once you're happy with the list, ask the agent to run it for real.

**You**: "Looks good — go ahead and clean them up."

Claude calls `mem_do(action="orphans", params={"dry_run": false})` and returns:

```
Cleanup complete:
- Orphaned files: 1
- Chunks deleted: 3
```

> This is the MCP-client twin of what
> [`examples/notebooks/06_lifecycle.ipynb`](../../examples/notebooks/06_lifecycle.ipynb)
> walks through from the Python API side (with the raw
> `storage.delete_chunks` / `delete_by_source` calls visible).

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
| `mem_config`\* | `mm config` |

\* `mem_config` requires `MEMTOMEM_TOOL_MODE=full`. In core/standard mode, use `mm config` (CLI) or the Web UI Settings tab.

---

## MCP Tools Used in This Tutorial

Core tools (always available in `core` mode, the default):

| Tool | Purpose |
|------|---------|
| `mem_status` | Check system status (also warns when orphan files exist) |
| `mem_index` | Index markdown files; supports `force=true` for full rebuild |
| `mem_search` | Hybrid search (BM25 + Dense) |
| `mem_recall` | Date/source-based memory retrieval |
| `mem_add` | Quick note addition |
| `mem_stats` | Overall status (chunk count, source count) |

Non-core actions (in `core` mode call them via `mem_do`; in
`standard` / `full` mode they are also available as top-level tools
named `mem_<action>`):

| `mem_do` action | Purpose |
|-----------------|---------|
| `batch_add` | Add multiple notes in one call |
| `edit` | Edit a chunk's source-file line range and re-index |
| `delete` | Delete chunk(s) by `chunk_id`, `source_file`, or `namespace` |
| `orphans` | Find and remove chunks whose source file no longer exists (alias of `cleanup_orphans`) |

---

## Next Steps

- [Interactive Notebooks](../../examples/notebooks/) — Same ideas, but driven from Python (Jupyter) instead of an MCP client. Useful if you want to embed memtomem in a data-science or agent-framework workflow.
- [User Guide](user-guide.md) — Complete feature walkthrough
- [Practical Use Cases](use-cases.md) — Agent workflow scenarios
- [MCP Client Configuration](mcp-clients.md) — Editor-specific setup
