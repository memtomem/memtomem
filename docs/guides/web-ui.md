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
- **Config**: View and edit all configuration sections (embedding, storage, indexing, search, decay)
- **Namespaces**: List, edit metadata, rename, delete namespaces

### Maintenance
- **Dedup**: Scan for duplicate chunks by similarity threshold, merge candidates
- **Decay**: Scan for stale chunks by age, preview and execute expiry

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
