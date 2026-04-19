# Web UI Guide

**Audience**: Users who want to browse, manage, and monitor their memory system visually

## Launch

```bash
# PyPI
uv tool install memtomem[web]
# Source (if running from git clone): uv run memtomem-web

memtomem-web                   # http://localhost:8080
memtomem-web --port 9090       # custom port
```

Or via environment:

```bash
MEMTOMEM_WEB__PORT=9090 memtomem-web
```

---

## Tabs Overview

| Tab | Purpose |
|-----|---------|
| **Home** | Dashboard with stats, charts, recent sources, quick actions |
| **Search** | Semantic search with filters, bulk operations, detail panel |
| **Sources** | Browse indexed files, view chunks per file |
| **Index** | Index new directories or re-index existing ones |
| **Tags** | Tag cloud/list, auto-tag untagged chunks |
| **Timeline** | Chronological chunk browser with date range filter |
| **More** | Settings hub with system, maintenance, and harness sub-tabs |

---

## Home Dashboard

The home tab shows a real-time overview:

- **Stat cards**: Total chunks, source files, namespaces, storage size, sessions, working memory entries
- **Charts**: Namespace distribution, file types, activity heatmap (1 year), chunk size distribution
- **Recent sources**: Last indexed files with sizes
- **Quick actions**: Search, index, reindex, export, dedup, auto-tag

---

## Settings Hub (More tab)

The settings hub is organized into groups:

### System
- **Config**: View and edit all configuration sections (embedding, storage, indexing, search, decay).
  Picks up external edits (`mm config set`, manual editor saves) on the next
  interaction — no restart needed. If `config.json` becomes invalid on disk,
  the Config tab shows a banner and saves are disabled until the file is
  fixed. See [configuration → external edits](configuration.md#external-edits-while-the-web-ui-is-running).
- **Namespaces**: List, edit metadata, rename, delete namespaces

### Maintenance
- **Dedup**: Scan for duplicate chunks by similarity threshold, merge candidates
- **Decay**: Scan for stale chunks by age, preview and execute expiry
- **Reset**: Delete all data (chunks, sessions, history, etc.) and reinitialize the DB. Embedding config is preserved. Requires double confirmation.

### Data Transfer
- **Export / Import**: Download chunks as JSON bundle, upload and re-import

### Harness

Agent Memory Harness monitoring:

#### Sessions

Browse episodic memory sessions recorded by AI agents.

- **Table columns**: Session ID, agent, namespace, started, ended, summary
- **Active badge**: Green indicator for sessions not yet ended
- **Events panel**: Click "Events" to expand and see all session events (queries, adds, edits, tool calls) with timestamps

#### Working Memory

Manage the short-term scratchpad used during agent sessions.

- **Add entries**: Set key, value, and optional TTL (minutes)
- **List view**: Shows all entries with session binding, expiry, and promoted status
- **Actions**:
  - **Delete**: Remove an entry
  - **Promote**: Convert to long-term memory (saves to markdown file and marks as promoted)

#### Procedures

View saved procedural memories — reusable workflows and patterns.

- Displays procedure-tagged chunks with full content
- Created via `mem_procedure_save` MCP tool

#### Hooks Sync

Compare and resolve conflicts between memtomem's canonical hook
definitions (`.memtomem/settings.json`) and Claude's
`~/.claude/settings.json`.

- **Status indicators**: In Sync, Out of Sync, or Conflicts
- **Synced hooks**: Hooks that match in both files
- **Pending hooks**: Hooks defined in memtomem but not yet in Claude's settings
- **Conflicts**: Hooks with the same name but different content — each
  conflict shows a diff with the option to accept the proposed version
- **Apply all**: Run a full settings merge to push all pending hooks at once
- **Per-conflict resolve**: Replace a single hook in the target file
  with memtomem's version. An mtime guard prevents race conditions if
  another process writes to the file concurrently

API endpoints:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/settings-sync` | Compare canonical vs target hooks |
| `POST` | `/api/settings-sync` | Run full settings merge |
| `POST` | `/api/settings-sync/resolve` | Resolve a single hook conflict |

#### Health

Memory system health report with visual gauges:

- **Access Coverage**: Percentage of chunks that have been accessed at least once
- **Tag Coverage**: Percentage of chunks with at least one tag
- **Dead Memories**: Percentage of chunks never accessed (candidates for cleanup)
- **Session count**: Total and active sessions
- **Working Memory**: Total entries and promoted count
- **Cross-References**: Number of chunk-to-chunk links
- **Top Accessed**: Most frequently accessed chunks
- **Namespace Distribution**: Chunks per namespace

---

## API Endpoints

The Web UI exposes a REST API at `/api/`. Interactive docs: `http://localhost:8080/api/docs`

### Harness endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/sessions` | List sessions (`agent_id`, `since`, `limit` params) |
| `GET` | `/api/sessions/{id}/events` | Get events for a session |
| `GET` | `/api/scratch` | List working memory entries |
| `POST` | `/api/scratch` | Set entry (`key`, `value`, `ttl_minutes`) |
| `DELETE` | `/api/scratch/{key}` | Delete entry |
| `POST` | `/api/scratch/{key}/promote` | Promote to long-term memory |
| `GET` | `/api/procedures` | List procedure-tagged chunks |
| `GET` | `/api/eval` | Memory health report JSON |

---

## Language / i18n

The web UI supports English and Korean. A toggle button in the header
switches between the two languages:

- **EN** / **한**: click to switch. The choice is stored in
  `localStorage` and persists across sessions.
- Auto-detection: if your browser language starts with `ko`, Korean
  is used by default on first visit.
- All static labels use `data-i18n` attributes; the `t()` function
  handles interpolation and falls back to English for any missing key.

---

## Security

- Binds to `127.0.0.1` only (not publicly accessible)
- CORS restricted to localhost origins
- Content Security Policy blocks inline scripts
- All markdown preview is sanitized with DOMPurify
- File access validates against indexed sources only
- Symlinked files are rejected

---

## Next Steps

- [User Guide](user-guide.md) — MCP tool reference
- [Agent Memory Guide](agent-memory-guide.md) — Sessions, working memory, procedures
- [Security Policy](../../SECURITY.md) — Security measures and reporting
