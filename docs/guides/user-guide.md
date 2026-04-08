# memtomem User Guide

**Audience**: Users who have completed the [Getting Started](getting-started.md) guide
**Prerequisites**: memtomem installed, MCP server connected to your AI editor

> **New to memtomem?** Start with [Getting Started](getting-started.md) first, then the [Hands-On Tutorial](hands-on-tutorial.md). This guide is a complete reference for all features.

---

## Glossary

| Term | Meaning |
|------|---------|
| **MCP** | Model Context Protocol — how your AI editor talks to memtomem |
| **Embedding** | A numeric vector that represents the meaning of text |
| **BM25** | Keyword-based search algorithm (like Google, but local) |
| **Dense search** | Meaning-based search using embeddings |
| **Hybrid search** | BM25 + dense combined — the default in memtomem |
| **RRF** | Reciprocal Rank Fusion — the algorithm that merges keyword and meaning results |
| **Chunk** | A section of a file (one heading, one function, one key) — the unit of indexing |
| **Namespace** | A label to organize memories (e.g., "work", "personal", "project-x") |
| **`mem_do`** | Meta-tool that routes to 61 advanced actions (with aliases). Use `mem_do(action="help")` to list all |

---

## How memtomem Works

```mermaid
flowchart LR
    A["Your files\n.md .json .py"] -->|mem_index| B["Knowledge Base"]
    B -->|mem_search| C["Relevant context"]
    C --> D["AI agent"]
    
    style A fill:#f9f,stroke:#333
    style D fill:#bbf,stroke:#333
```

**Index once, search forever.** memtomem reads your files, breaks them into meaningful chunks, and builds a searchable index. When your AI agent needs context, it searches by both keywords and meaning to find the most relevant pieces.

Your `.md` files are the source of truth. The index is a rebuildable cache — delete it anytime and re-index.

### What happens under the hood

```mermaid
sequenceDiagram
    participant User as Your files
    participant Index as mem_index
    participant DB as Knowledge Base
    participant Search as mem_search
    participant Agent as AI agent

    User->>Index: New or changed files
    Index->>Index: Split into chunks (by heading, function, key)
    Index->>Index: Skip unchanged chunks
    Index->>DB: Store chunks + embeddings
    Agent->>Search: "deployment checklist"
    Search->>DB: Keyword match + meaning match
    DB-->>Search: Top results (ranked)
    Search-->>Agent: Relevant context
```

---

## MCP Tools at a Glance

memtomem provides **65 MCP tools** organized into categories (plus optional STM proxy tools):

| Category | Tools | What they do |
|----------|-------|-------------|
| **Search** | `mem_search`, `mem_recall` | Find memories by meaning or by date |
| **CRUD** | `mem_add`, `mem_batch_add`, `mem_edit`, `mem_delete` | Create, update, remove memories |
| **Indexing** | `mem_index` | Build the knowledge base from files |
| **Namespace** | `mem_ns_list/set/get/assign/update/rename/delete` | Organize memories into groups |
| **Maintenance** | `mem_dedup_scan/merge`, `mem_decay_scan/expire`, `mem_auto_tag`, `mem_cleanup_orphans` | Keep the index clean |
| **Data** | `mem_export`, `mem_import` | Backup and restore |
| **Config** | `mem_stats`, `mem_status`, `mem_config`, `mem_embedding_reset` | Monitor and configure |

### `mem_do` action naming convention

In **core** tool mode (default), most features are accessed through `mem_do(action="...")`. Action names follow these conventions:

- **Namespace actions** use `ns_` prefix: `ns_list`, `ns_set`, `ns_assign`, `ns_rename`, `ns_delete`
- **Session actions** use `session_` prefix: `session_start`, `session_end`, `session_list`
- **Scratch (working memory)** uses `scratch_` prefix: `scratch_set`, `scratch_get`, `scratch_promote`
- **Maintenance** uses descriptive names: `dedup_scan`, `decay_expire`, `cleanup_orphans`
- **Analytics** uses short names: `eval`, `activity`, `timeline`, `reflect`

Use `mem_do(action="help")` to see all 61 actions, or `mem_do(action="help", params={"category": "sessions"})` for per-category details with parameter descriptions. Common aliases are supported (e.g. `health_report` → `eval`, `namespace_set` → `ns_set`).

---

## 1. Indexing — `mem_index`

### Index a directory

```
mem_index(path="~/notes")
→ "Indexed 47 files (312 chunks)"
```

Supported files and their chunking strategies:

| File Type | Strategy |
|-----------|----------|
| `.md` | Heading-aware split (`#`, `##`, `###`) |
| `.json` / `.yaml` / `.toml` | Top-level key split |
| `.py` | Functions and classes (tree-sitter) |
| `.js` / `.ts` / `.tsx` | Functions and classes (tree-sitter) |

### Incremental re-indexing

memtomem tracks what changed. Re-indexing only processes new or modified chunks:

```
mem_index(path="~/notes")
→ "3 new, 2 updated, 1 deleted — 44 unchanged skipped"
```

### Force re-index

After switching embedding models, upgrading memtomem, or for a clean rebuild:

```
mem_index(path="~/notes", force=True)
```

### Namespace-scoped indexing

```
mem_index(path="~/work/docs", namespace="work")
mem_index(path="~/personal/notes", namespace="personal")
```

### Auto-watch

Files in `MEMTOMEM_INDEXING__MEMORY_DIRS` are watched for changes and re-indexed automatically. For files outside watched directories, call `mem_index` manually.

---

## 2. Search — `mem_search`, `mem_recall`

### `mem_search` — Hybrid search

```
mem_search(query="deployment checklist")
```

Combines keyword matching (exact words) with meaning-based search (similar concepts), then merges the results for the best of both worlds.

**Parameters**:

| Parameter | Description | Example |
|-----------|-------------|---------|
| `query` | Natural language search query | `"authentication flow"` |
| `top_k` | Number of results (default 10, max 100) | `20` |
| `source_filter` | File path substring (recommended) or glob | `"docs/adr"`, `".yaml"` |
| `tag_filter` | Comma-separated tags, OR logic | `"redis,cache"` |
| `namespace` | Scope to namespace | `"work"` |

```
mem_search(query="caching strategy", tag_filter="redis,cache", namespace="work")
mem_search(query="auth", source_filter="docs/adr", top_k=5)
```

> **source_filter tip**: Use substrings like `"docs/adr"` or `".py"` for filtering. Glob patterns (`*`, `?`) are matched against the **full absolute path** via `fnmatch`, so `"*.py"` won't work as expected — use `".py"` instead.

### Tuning search weights

Use `bm25_weight` and `dense_weight` to shift between keyword and semantic matching:

```
mem_search(query="쿠버네티스", bm25_weight=2.0, dense_weight=0.5)   # keyword-heavy
mem_search(query="container alerts", bm25_weight=0.5, dense_weight=2.0) # meaning-heavy
```

### Cross-language search

memtomem supports searching across languages (e.g., querying in English to find Korean content), but quality depends on the embedding model:

#### Embedding model choice

| Model | KR→EN cross-search | EN→KR cross-search | KR semantic accuracy |
|-------|:---:|:---:|:---:|
| `nomic-embed-text` (768d) | Weak (often misses) | Good (#2) | Moderate |
| `bge-m3` (1024d) | **Good (#2)** | **Good (#2)** | **High (#1)** |

**Recommendation**: Use `bge-m3` if you work with Korean or other non-English content. Switch with:
```
mm embedding-reset --mode apply-current   # after updating config
mm index ~/notes --force                  # re-embed all files
```

Or in `~/.memtomem/config.json`:
```json
{"embedding": {"model": "bge-m3", "dimension": 1024}}
```

#### BM25 and language

- **Keyword (BM25) search** is language-bound — Korean keywords only match Korean text, English keywords only match English text. This is expected.
- For **Korean-heavy workloads**, switch the tokenizer to `kiwipiepy` for better BM25 results:
  ```
  mm config set search.tokenizer kiwipiepy
  ```
  This requires `pip install kiwipiepy` and provides morphological analysis for Korean text. The default `unicode61` tokenizer splits Korean text at character boundaries rather than morpheme boundaries.

### `mem_recall` — Date-range retrieval

Find memories by *when* they were created, without a search query:

```
mem_recall(since="2026-03", limit=10)
mem_recall(since="2026-01", until="2026-03")
mem_recall(since="2026-03-15", source_filter="meeting*")
mem_recall(namespace="project:*", limit=5)
```

**Parameters**:

| Parameter | Description | Format |
|-----------|-------------|--------|
| `since` | Inclusive start date | `YYYY`, `YYYY-MM`, `YYYY-MM-DD`, ISO datetime |
| `until` | Exclusive end date | same formats |
| `source_filter` | File path substring or glob | `"notes"`, `"*.md"` |
| `namespace` | Single, comma-separated, or glob | `"work"`, `"project:*"` |
| `limit` | Max results (default 20) | `10` |

---

## 3. Memory CRUD — `mem_add`, `mem_batch_add`, `mem_edit`, `mem_delete`

### `mem_add` — Add a note

```
mem_add(content="Redis LRU→LFU migration reduced cache misses by 40%", tags="redis,performance")
→ Saved to ~/.memtomem/memories/2026-03-30.md (1 chunk indexed)
```

| Parameter | Description |
|-----------|-------------|
| `content` | The note text |
| `title` | Optional heading (becomes `## title` in the file) |
| `tags` | Comma-separated tags |
| `file` | Target file path (auto-generates date-stamped file if omitted) |
| `namespace` | Namespace assignment |
| `template` | Structured template (`adr`, `meeting`, `debug`, `decision`, `procedure`) |

```
mem_add(content="New rate limit: 1000 req/min", file="api-notes.md", tags="api")
mem_add(content="Sprint decision: use GraphQL", title="Sprint 12", namespace="work")
```

#### Structured Templates

Use `template` to create formatted entries:

```
mem_add(template="adr", content='{"title":"Use GraphQL","context":"REST API hitting limits","decision":"Migrate to GraphQL","consequences":"Need to retrain team"}')
```

| Template | Fields | Use case |
|----------|--------|----------|
| `adr` | title, status, context, decision, consequences | Architecture decision records |
| `meeting` | title, date, attendees, agenda, decisions, action_items | Meeting notes |
| `debug` | title, symptom, root_cause, fix, prevention | Debugging logs |
| `decision` | title, options, chosen, rationale | Decision records |
| `procedure` | title, trigger, steps, tags | Reusable workflows |

You can also pass plain text as `content` — it will be placed in the template body directly. Fields not provided in the JSON are automatically omitted from the output.

#### How `mem_add` stores entries

- Without `file`: entries are appended to a date-stamped file (`~/.memtomem/memories/YYYY-MM-DD.md`).
- Each entry gets its own `## ` heading and is indexed as a separate chunk.
- Tags are applied only to the new entry, not to existing entries in the same file.
- The file is re-indexed after each add, but unchanged chunks are skipped (incremental indexing).

### `mem_batch_add` — Add multiple notes

```
mem_batch_add(entries=[
  {"key": "python-tip", "value": "Use walrus operator := for assignment expressions"},
  {"key": "docker-tip", "value": "Use multi-stage builds to reduce image size"}
])
```

Entries become `## key` headings in a single markdown file.

### `mem_edit` — Edit a chunk

Use the chunk ID from `mem_search` results:

```
mem_edit(chunk_id="abc123-...", new_content="Updated content")
```

Modifies the source `.md` file and re-indexes it.

> **Note**: After editing, the chunk gets a new UUID (the old one is replaced during re-indexing). If you need to reference it again, search for the updated content.

### `mem_delete` — Delete

```
mem_delete(chunk_id="abc123-...")                # single chunk
mem_delete(source_file="/path/to/notes.md")      # all chunks from a file
mem_delete(namespace="old-project")              # all chunks in a namespace
```

---

## 4. Namespace — `mem_ns_*`

Namespaces organize memories into scoped groups.

### Core workflow

```
mem_ns_set(namespace="work")          # set session default
mem_ns_get()                          # check current namespace
mem_ns_list()                         # list all namespaces with counts
→ default: 200 chunks
  work: 150 chunks
  personal: 94 chunks
```

After `mem_ns_set`, all operations (search, add, index) default to that namespace.

### Namespace metadata

```
mem_ns_list()
→ default: 200 chunks
  work: 150 chunks (description="Company project docs", color="#3B82F6")
  personal: 94 chunks

mem_ns_update(namespace="work", description="Company project docs", color="#3B82F6")
```

### Rename and delete

```
mem_ns_rename(old="project-v1", new="project-v2")   # SQL update, no re-indexing
mem_ns_delete(namespace="archived")                  # deletes all chunks
```

### Bulk assign — `ns_assign`

Move existing chunks to a namespace without re-indexing:

```
mem_do(action="ns_assign", params={"namespace": "infra", "source_filter": "k8s"})
mem_do(action="ns_assign", params={"namespace": "archive", "old_namespace": "default"})
```

| Parameter | Description |
|-----------|-------------|
| `namespace` | Target namespace to assign to |
| `source_filter` | Only chunks from paths containing this substring |
| `old_namespace` | Only chunks currently in this namespace |

### Auto-namespace

Derive namespace from subfolder names automatically:

```
mem_config(key="namespace.enable_auto_ns", value="true")
```

```
memory_dirs = ["~/notes"]

~/notes/work/api.md       → namespace "work"
~/notes/personal/diary.md → namespace "personal"
~/notes/readme.md          → namespace "default"  (root of memory_dir)
```

Files at the root of a `memory_dir` get the default namespace. Only files inside subfolders get auto-derived namespaces. This prevents the memory_dir folder name itself from becoming an unintended namespace.

---

## 5. Maintenance — `mem_dedup_*`, `mem_decay_*`, `mem_auto_tag`

### Deduplication

```
mem_dedup_scan(threshold=0.92, limit=50)
→ 3 candidate pairs:
  1. [0.97] chunk-A ↔ chunk-B (exact match)
  2. [0.94] chunk-C ↔ chunk-D

mem_dedup_merge(keep_id="chunk-A-uuid", delete_ids=["chunk-B-uuid"])
→ Deleted 1, kept chunk-A (tags merged)
```

> **Performance**: `max_scan` controls how many chunks are compared pairwise. For large indexes (500+), start with `max_scan=100` to avoid timeouts (30s limit). Increase gradually if needed.

### Auto-tagging

```
mem_auto_tag(max_tags=5, dry_run=True)              # preview
mem_auto_tag(max_tags=5)                            # apply
mem_auto_tag(source_filter="notes", overwrite=True) # re-tag specific files
```

### Decay and expiration

Score decay reduces relevance of older memories:

```
mem_config(key="decay.enabled", value="true")
mem_config(key="decay.half_life_days", value="30")
```

TTL-based cleanup:

```
mem_decay_scan(max_age_days=90)                     # preview
mem_decay_expire(max_age_days=90)                   # delete (dry_run=True by default)
```

### Orphan cleanup

Remove chunks whose source files have been deleted:

```
mem_do(action="cleanup_orphans")                    # preview (dry_run=True)
mem_do(action="cleanup_orphans", params={"dry_run": false})  # delete
```

---

## 6. Data — `mem_export`, `mem_import`

### Backup

```
mem_export(output_file="~/backup.json")
mem_export(output_file="~/work-backup.json", namespace="work")
mem_export(output_file="~/recent.json", since="2026-03-01")
```

Exports chunks with content, metadata, tags, and embeddings as a JSON bundle.

### Restore

```
mem_import(input_file="~/backup.json")
mem_import(input_file="~/backup.json", namespace="imported")
```

Import re-embeds all chunks, so it works across different embedding models or machines.

### Importing from Obsidian

```
mem_do(action="import_obsidian", params={"vault_path": "~/obsidian-vault", "namespace": "notes"})
```

How Obsidian files are processed:
- **YAML frontmatter**: `tags` are automatically extracted and applied as memtomem chunk tags. Other frontmatter fields are included in the searchable content.
- **Wikilinks**: `[[target|alias]]` is resolved to `alias`, `[[target]]` to `target` — the `[[` brackets are removed during chunking so search results show clean text.
- **Heading-based chunking**: Each `##` heading becomes a separate chunk, just like native memtomem files.
- **Output**: Imported files are copied to `~/.memtomem/memories/_imported/obsidian/`.

### Importing from Notion

```
mem_do(action="import_notion", params={"path": "~/notion-export.zip", "namespace": "notion"})
```

---

## 7. Config — `mem_stats`, `mem_status`, `mem_config`, `mem_embedding_reset`

### `mem_stats` / `mem_status`

```
mem_stats()
→ Total chunks: 444, Sources: 104, Storage: sqlite

mem_status()
→ Storage: sqlite, DB path: ~/.memtomem/memtomem.db
  Embedding: ollama/bge-m3 (1024d), Top-K: 10, RRF k: 60
  Total chunks: 444, Source files: 104
```

### `mem_config` — View and modify settings

```
mem_config()                                         # show all settings
mem_config(key="search.default_top_k")               # read one value
mem_config(key="search.default_top_k", value="20")   # change (persists to ~/.memtomem/config.json)
```

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `search.default_top_k` | `10` | Search result count |
| `search.enable_bm25` | `true` | Keyword search (exact word match) |
| `search.enable_dense` | `true` | Meaning search (similar concepts) |
| `search.rrf_k` | `60` | Result merging balance (higher = smoother) |
| `indexing.max_chunk_tokens` | `512` | Max tokens per chunk |
| `indexing.min_chunk_tokens` | `128` | Short chunk merge threshold |
| `decay.enabled` | `false` | Time-based score decay |
| `mmr.enabled` | `false` | Result diversification |
| `namespace.enable_auto_ns` | `false` | Auto-derive namespace from folders |

### `mem_embedding_reset` — Resolve model mismatch

**MCP:**
```
mem_embedding_reset()                       # check status (default)
mem_embedding_reset(mode="apply_current")   # reset DB to current model (requires re-index)
mem_embedding_reset(mode="revert_to_stored") # switch runtime to match DB
```

**CLI:**
```bash
mm embedding-reset                          # check status (default)
mm embedding-reset --mode apply-current     # reset DB to current model (requires re-index)
mm embedding-reset --mode revert-to-stored  # switch runtime to match DB
```

---

## Web UI

```bash
memtomem-web                  # http://localhost:8080
memtomem-web --port 3000      # custom port
```

| Tab | Features |
|-----|----------|
| **Home** | Stats (chunks, sources, namespaces, sessions, working memory), charts, recent sources |
| **Search** | Full search with filters, detail panel, source preview, chunk editing |
| **Sources** | Browse indexed files, view chunks, delete sources |
| **Index** | Trigger indexing, upload files, add memories |
| **Tags** | Tag cloud, auto-tagging, tag-based browsing |
| **Timeline** | Chronological view of all chunks |
| **STM** | Proxy monitoring — compression metrics, server status, call history (only when STM installed) |
| **More** | Config, namespaces, dedup, decay, export/import, **harness** (sessions, working memory, procedures, health) |

See [Web UI Guide](web-ui.md) for full details.

---

## Google Drive Integration (Multi-Device / Team)

memtomem works across devices and teams by sharing files via Google Drive (or Dropbox, OneDrive) while keeping the search index local per device.

### Core Principles

| Item | Location | Why |
|------|----------|-----|
| Markdown files | Google Drive (shared) | Single source of truth, synced across devices |
| SQLite DB | Local per device (`~/.memtomem/`) | WAL mode is incompatible with cloud sync — corruption risk |
| Folder structure | Namespace-based subfolders | `auto_ns` maps subfolder names to namespaces |

Each device builds its own search index by indexing the same shared files. The DB stores absolute paths internally, so each device's index is independent.

### Architecture

```mermaid
flowchart TB
    GD["Google Drive\nmemtomem-memories/"]
    GD --> T["team/\nadr-003.md\nconventions.md"]
    GD --> P["personal/\njournal.md"]
    GD --> PX["project-x/\ndesign-doc.md"]
    
    GD -.->|sync| DA["Device A\nlocal DB"]
    GD -.->|sync| DB["Device B\nlocal DB"]
    
    style GD fill:#ffd,stroke:#333
    style DA fill:#dfd,stroke:#333
    style DB fill:#dfd,stroke:#333
```

Files live on Google Drive (shared). Each device builds its own local search index independently.

> **Folder = Namespace**: With `ENABLE_AUTO_NS=true`, subfolder names become namespaces automatically. Files at the root of `memory_dirs` get the `default` namespace. Create subfolders to organize by team, project, or topic.

### Step 1: Create the shared folder

Set up a namespace-based folder structure on Google Drive:

```
Google Drive/My Drive/
  memtomem-memories/
    team/               ← shared team knowledge → namespace "team"
    personal/           ← individual notes → namespace "personal"
    project-alpha/      ← project docs → namespace "project-alpha"
```

For teams, share the `memtomem-memories` folder with members.

### Step 2: Configure each device

Point `MEMORY_DIRS` to the Google Drive sync path. Keep the DB local.

**macOS** (Drive syncs to `~/Library/CloudStorage/GoogleDrive-{email}/My Drive/` or `~/Google Drive/My Drive/`):

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_STORAGE__SQLITE_PATH": "~/.memtomem/memtomem.db",
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "~/Google Drive/My Drive/memtomem-memories",
        "MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS": "true"
      }
    }
  }
}
```

**Windows**:

```json
{
  "env": {
    "MEMTOMEM_STORAGE__SQLITE_PATH": "%USERPROFILE%\\.memtomem\\memtomem.db",
    "MEMTOMEM_INDEXING__MEMORY_DIRS": "G:\\My Drive\\memtomem-memories",
    "MEMTOMEM_NAMESPACE__ENABLE_AUTO_NS": "true"
  }
}
```

### Step 3: Initial indexing

Each device indexes the shared folder to build its local search index:

```
mem_index(path="~/Google Drive/My Drive/memtomem-memories")
→ "Indexed 47 files (312 chunks)"
```

### Step 4: Daily workflow

**Adding notes** — Use relative paths in `mem_add`. The path is resolved against `memory_dirs[0]` (the shared Drive folder), so it works identically on every device:

```
mem_add(content="Sprint: migrate to GraphQL", file="team/decisions.md", tags="architecture")
→ writes to ~/Google Drive/My Drive/memtomem-memories/team/decisions.md
→ Google Drive syncs to other devices
```

**Searching** — Instant, uses the local index:

```
mem_search(query="GraphQL decision", namespace="team")
```

**Syncing** — After other devices' files sync, re-index to pick up changes:

```
mem_index(path="~/Google Drive/My Drive/memtomem-memories")
```

> **Note**: The file watcher may miss changes from cloud sync. Use explicit `mem_index` calls for reliability.

### Step 5: Team workflow example

```
Alice (Machine A):
  mem_add(content="Auth: JWT + refresh tokens", file="team/adr-003.md")
  → file syncs to Google Drive

Bob (Machine B, after sync completes):
  mem_index(path="~/Google Drive/My Drive/memtomem-memories")
  mem_search(query="authentication decision")
  → finds Alice's ADR entry in namespace "team"
```

### Limitations

**Absolute paths in DB**: Each device stores full resolved paths (e.g., `/Users/alice/Google Drive/...`). This means:
- `mem_delete(source_file=...)` only works with the local device's paths
- `source_filter` substring matching works across devices if you use folder names (e.g., `source_filter="team/"`)
- Namespace-based filtering (`namespace="team"`) is more reliable than path-based filtering across devices

**Export/Import**: `mem_export` includes absolute paths in the JSON bundle. Importing on another device preserves those paths as metadata, but they may not match the local filesystem. For cross-device sharing, use Google Drive file sync + `mem_index` instead of export/import. Use export/import for same-device backup and restore only.

### Checklist

| Item | Guidance |
|------|----------|
| Files on Google Drive | **Required** — single source of truth |
| DB local per device | **Required** — WAL mode breaks with cloud sync |
| Subfolder = namespace structure | **Recommended** — consistent organization |
| `ENABLE_AUTO_NS=true` | **Recommended** — auto-maps folders to namespaces |
| `mem_index` after sync | **Recommended** — reliable re-indexing |
| Relative paths in `mem_add` | **Recommended** — works identically on all devices |
| Periodic `mem_dedup_scan` | **Recommended** — catch overlapping edits |
| Store `.db` on cloud drive | **Never** — causes corruption |
| `mem_export` for cross-device sharing | **Not recommended** — use file sync instead |

---

## Agent Context Management — `mm context`

If you use multiple AI editors (Claude Code, Cursor, Gemini CLI, etc.), each expects its own config file with the same project information. `mm context` keeps them in sync from one source.

### The problem

```
CLAUDE.md        ← "Python 3.12+, use ruff, run pytest..."
.cursorrules     ← "Python 3.12+, use ruff, run pytest..."  (copy-paste)
GEMINI.md        ← "Python 3.12+, use ruff, run pytest..."  (copy-paste again)
```

### The solution

```mermaid
flowchart TB
    CTX[".memtomem/context.md\n(single source of truth)"]
    CTX -->|mm context generate| C["CLAUDE.md"]
    CTX -->|mm context generate| CR[".cursorrules"]
    CTX -->|mm context generate| G["GEMINI.md"]
    CTX -->|mm context generate| A["AGENTS.md"]
    CTX -->|mm context generate| CP[".github/copilot-instructions.md"]
```

### Getting started

**If you already have a CLAUDE.md or .cursorrules:**

```bash
mm context init                      # extracts sections from existing files
```

This creates `.memtomem/context.md` with sections parsed from your richest agent file. Review and edit it.

**If starting fresh:**

```bash
mm context init                      # creates a template
```

Edit `.memtomem/context.md` with your project info:

```markdown
## Project
- Name: my-app
- Language: Python 3.12+

## Commands
- Test: pytest
- Lint: ruff check .

## Architecture
FastAPI backend with SQLite storage.

## Rules
- Line length 100
- Type hints required

## Style
- English for code
```

### Generate agent files

```bash
mm context generate --agent all      # generate all
mm context generate --agent claude   # CLAUDE.md only
mm context generate --agent cursor   # .cursorrules only
```

### Keep in sync

When you update project rules:

```mermaid
sequenceDiagram
    participant You
    participant CTX as context.md
    participant Files as Agent files

    You->>CTX: Edit rules
    You->>Files: mm context diff
    Note over Files: Shows which files are out of sync
    You->>Files: mm context sync
    Note over Files: All agent files updated
```

```bash
# edit .memtomem/context.md
mm context diff                      # see what's out of sync
mm context sync                      # update all detected agent files
```

### Detect existing files

```bash
mm context detect
→ Found 3 agent file(s):
    claude      CLAUDE.md  (4488 bytes)
    cursor      .cursorrules  (1200 bytes)
    gemini      GEMINI.md  (3200 bytes)
```

### Via MCP tools

```
mem_do(action="context_detect")
mem_do(action="context_generate", params={"agent": "cursor"})
mem_do(action="context_diff")
mem_do(action="context_sync")
```

### Agent-specific sections

Add `## Claude`, `## Cursor`, etc. in context.md for agent-specific overrides:

```markdown
## Rules
- Line length 100 (applies to all agents)

## Claude
- Use tool_handler decorator for all new MCP tools

## Cursor
- Prefer inline type hints over separate .pyi files
```

### Supported agents

| Agent | Output file |
|-------|-------------|
| Claude Code | `CLAUDE.md` |
| Cursor | `.cursorrules` |
| Gemini CLI | `GEMINI.md` |
| OpenAI Codex | `AGENTS.md` |
| GitHub Copilot | `.github/copilot-instructions.md` |

---

## CLI Reference

`mm` is a shorthand alias for `memtomem`. All commands support `-h` and `--help`.

```bash
# Setup
mm init                                # 7-step interactive wizard (b: back, q: quit)

# Core (daily use)
mm search "deployment"                 # hybrid search (keywords + meaning)
mm index ~/notes                       # index directory
mm add "note" --tags "tag1"            # add a memory
mm recall --since 2026-03-01           # recall by date

# Configuration
mm config show                         # view all settings
mm config set search.default_top_k 20  # change a setting
mm embedding-reset                     # check/resolve embedding model mismatch

# Agent context sync
mm context detect                      # find agent config files
mm context init                        # create unified context.md
mm context generate --agent all        # generate all agent files
mm context diff                        # check sync status
mm context sync                        # sync context.md → agent files

# STM proxy (optional)
mm stm init                            # interactive STM proxy setup wizard
mm stm reset                           # disable STM, restore original MCP configs

# Utilities
mm shell                               # interactive REPL
mm web                                 # launch Web UI (http://localhost:8080)
```

Install the CLI: `uv tool install memtomem` (PyPI) or `uv run mm ...` (source).
All commands support `-h` and `--help`.

---

## Troubleshooting

### "No results found"

1. `mem_stats()` — Check that chunks > 0
2. `mem_index(path="~/notes")` — Re-index
3. Remove filters and try a broader query

### Embedding errors

1. Ollama: `ollama list` to verify model is pulled
2. OpenAI: check `mem_config(key="embedding.api_key")`
3. Check mismatch: `mm embedding-reset` (CLI) or `mem_embedding_reset()` (MCP)
4. Reset to current model: `mm embedding-reset --mode apply-current` then `mm index ~/notes`

### MCP tools not visible

1. Fully quit and relaunch your MCP client
2. Verify config file path (see [MCP Clients](mcp-clients.md))
3. `mem_status` — Confirm connection

### MCP server directory changed

If you moved or renamed your memtomem source directory:

1. Update the `--directory` path in your MCP config:
   - **Claude Code**: `claude mcp add memtomem -s user -- uv run --directory /new/path memtomem-server`
   - **Cursor/Windsurf**: edit `mcp.json` and update the `args` array
2. Restart or reconnect the MCP server from your editor (e.g., `/mcp` → Reconnect in Claude Code)
3. If reconnect fails, fully quit and relaunch the editor

### Slow search

1. Reduce `top_k` (default 10)
2. `mem_config(key="search.bm25_candidates", value="30")` — Reduce candidate pool
3. Disable one retriever if sufficient: `mem_config(key="search.enable_bm25", value="false")`

### "database is locked"

SQLite allows only one writer at a time. If the MCP server and Web UI server both try to write simultaneously, one will get a lock error. Solutions:
1. Run only one write-capable server at a time (read operations are fine concurrently)
2. Retry the operation — the lock is typically brief
3. For production, use a single server process

### Concurrent MCP + Web server

Running both `memtomem-server` (MCP) and `memtomem-web` simultaneously is supported but has caveats:

- **File watcher overlap**: both servers watch `memory_dirs`. A file created by one server may be re-indexed by the other, causing duplicate chunks. Restart the server that has stale data, or run `mem_index(force=True)` to reconcile.
- **Orphaned index entries**: interrupted concurrent writes could previously leave orphaned FTS/vec entries causing `constraint failed` errors on subsequent indexing. This is now handled automatically — `upsert_chunks` defensively cleans orphans before INSERT.
- **Recommendation**: for typical usage, run only the MCP server. Launch the Web UI on-demand when you need visual browsing.

---

## STM: Proactive Memory Surfacing (Optional)

The **memtomem-stm** package adds a proxy that sits between your AI agent and other MCP servers. It automatically recalls relevant memories when your agent uses any tool.

### How it works

```mermaid
sequenceDiagram
    participant Agent as AI agent
    participant STM as STM Proxy
    participant FS as File Server
    participant LTM as memtomem (LTM)

    Agent->>STM: fs__read_file("/src/auth.py")
    STM->>FS: Forward request
    FS-->>STM: File content
    STM->>STM: Compress response (save tokens)
    STM->>LTM: "What do I know about auth.py?"
    LTM-->>STM: Related memories
    STM-->>Agent: File content + relevant memories
```

The agent gets both the tool response and your previous notes about the topic — without asking.

### Quick setup

```bash
pip install "memtomem-stm[ltm]"

# Add an upstream MCP server
memtomem-stm-proxy add filesystem \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem /home/user" \
  --prefix fs

# Or run integrated with memtomem server
export MEMTOMEM_STM_PROXY__ENABLED=true
```

### STM tools

| Tool | Description |
|------|-------------|
| `stm_proxy_stats` | Token savings and cache statistics |
| `stm_proxy_select_chunks` | Retrieve sections from selective compression TOC |
| `stm_proxy_read_more` | Read next chunk from progressive delivery response |
| `stm_proxy_cache_clear` | Clear response cache |
| `stm_surfacing_feedback` | Rate surfaced memories (helpful / not_relevant / already_known) |
| `stm_surfacing_stats` | Surfacing effectiveness metrics |

### Safety features

- **Circuit breaker**: 3 LTM failures → 60s cooldown → auto-recovery
- **Timeout**: 3s surfacing timeout, falls back to original response
- **Rate limiting**: Max 15 surfacings per minute
- **Write-tool skip**: Automatically skips surfacing for write/create/delete operations
- **Auto-tuning**: Adjusts relevance threshold per tool based on feedback

See [STM README](../../packages/memtomem-stm/README.md) for full documentation.

---

## Next Steps

- [Hands-On Tutorial](hands-on-tutorial.md) — Step-by-step with example files
- [Agent Memory Guide](agent-memory-guide.md) — Sessions, working memory, procedures, multi-agent, reflection
- [Web UI Guide](web-ui.md) — Dashboard, harness tabs, health report
- [LangGraph Integration](integrations/langgraph.md) — Python adapter for LangGraph/LangChain
- [Practical Use Cases](use-cases.md) — Agent workflow scenarios
- [Claude Code Hooks](hooks.md) — Automate memory with hooks
- [STM Proxy Gateway](../../packages/memtomem-stm/README.md) — Proactive surfacing, compression, caching
- [Full Tool Reference](../../packages/memtomem/README.md) — All 65 tools with parameters
