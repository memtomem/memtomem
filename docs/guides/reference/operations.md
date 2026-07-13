# Operations & troubleshooting

Running the Web UI, auditing managed content, fixing common problems, optional
STM proactive surfacing, and uninstalling.

[← memtomem Reference](../reference.md)

**On this page**

- [Web UI](#web-ui)
- [Privacy audits](#privacy-audits)
- [Troubleshooting](#troubleshooting)
- [STM: Proactive Memory Surfacing (Optional)](#stm-proactive-memory-surfacing-optional)
- [Uninstalling memtomem](#uninstalling-memtomem)

---

## Web UI

```bash
mm web                         # http://localhost:8080 (prod surface)
mm web --port 3000             # custom port
mm web -b --port 3000          # run in the background
mm web status                  # show pid/port/start time
mm web stop                    # stop the tracked Web UI process
mm web --dev                   # adds opt-in maintainer pages
mm web --mode {prod,dev}       # explicit mode (mutually exclusive with --dev)
```

`mm web` opens in **Simple** mode by default, showing the Home, Search, Sources, Gateway, Index, and Settings tabs. The **Gateway** tab is the Context Gateway surface (Overview, Projects, Skills, Commands, Subagents, MCP Servers, Hooks, Wiki); the **Settings** tab holds Config, Namespaces, and Reset Database. Flip the header's **Advanced** toggle to reveal the Tags and Timeline tabs, plus the Dedup, Age-out, and Export/Import sections inside Settings. `mm web --dev` — or setting `MEMTOMEM_WEB__MODE=dev` in your shell profile — extends the surface with maintainer pages (Sessions, Working Memory, Procedures, Health Report) and unlocks structural namespace verbs (rename, delete) that are dev-only by ADR-0007.

Tab classification changes over time — run `mm web --dev` against your installed version to see the complete surface. The API endpoints backing dev-only pages return 404 in `prod` mode; scripts that hit `/api/sessions`, `/api/scratch`, `/api/namespaces/{ns}/rename`, `DELETE /api/namespaces/{ns}`, etc. need `dev` mode. `GET /api/namespaces` (list) and `PATCH /api/namespaces/{ns}` (cosmetic edit — color, description) are prod-tier and respond in both modes.

### Remote access

`mm web` binds the loopback interface (`127.0.0.1`) by default, and **refuses to start** when `--host` is anything else:

```text
Error: --host 0.0.0.0 exposes the Web UI off-loopback. Pass --allow-remote-ui
to acknowledge, paired with --trusted-origin and --trusted-host so the
CSRF/Origin/Host allow-list covers the remote shape.
```

The refusal is deliberate: the Web UI is an **unauthenticated** single-page app — memtomem ships no first-party login ([ADR-0029](../../adr/0029-mcp-network-transport-auth-stance.md)) — so anyone who can reach the port can read and modify your memory store. Exposing it off-loopback takes three flags:

| Flag | Effect |
|------|--------|
| `--allow-remote-ui` | Acknowledge the off-loopback bind. Required whenever `--host` is non-loopback; startup refuses without it. |
| `--trusted-origin HOST` | Add a hostname to the CSRF Origin/Referer allow-list (repeatable). Loopback (`127.0.0.1`, `::1`, `localhost`) is always trusted; anything else must be named explicitly. |
| `--trusted-host HOST` | Add a hostname to the Host-header allow-list (repeatable). Defends DNS rebinding when running with `--allow-remote-ui`. |

A trusted-LAN example — serving the UI to browsers that reach this machine as `workstation.local`:

```bash
mm web --host 0.0.0.0 --allow-remote-ui \
  --trusted-origin workstation.local \
  --trusted-host workstation.local
```

Requests whose `Origin`/`Referer` or `Host` headers are not on the allow-list are rejected by the three-layer request guard — see [SECURITY.md](../../../SECURITY.md#csrf--origin--host-guard-rfc-787) for the full model and the `MEMTOMEM_WEB__CSRF_ENFORCE` rollback valve.

**Anything beyond a trusted LAN needs an authenticating reverse proxy** — TLS plus auth in front, with memtomem still bound to loopback behind it. The [authenticated reverse proxy recipe](../mcp-clients.md#authenticated-reverse-proxy-required-for-public-exposure) (nginx, TLS + Basic auth) applies to the Web UI the same way it does to MCP network transports.

---

## Privacy audits

memtomem applies its redaction guard before managed writes, but historical
content may predate the current patterns. The audit commands below are
read-only: they report findings without deleting, quarantining, rewriting, or
re-embedding anything.

```bash
# Stored database chunks in one explicit memory tier
mm mem rescan --scope user --json
mm mem rescan --scope project_shared --json
mm mem rescan --scope project_local --json

# Historical files under managed _imported/, _fetched/, and sessions/ trees
mm mem rescan-files --json

# Canonical Context Gateway artifacts in one explicit tier
mm context rescan --scope project_shared --json
```

Each command exits `0` when the scan is clean and `1` when it finds a
violation. `mm mem rescan` accepts `--source` to narrow the database scan to
one file or directory. `mm context rescan` requires a scope so CI and audit
logs never rely on an implicit tier.

Review every reported path or chunk before remediation. Move secrets out of
git-tracked content, rotate exposed credentials, and then use the ordinary
edit/delete/re-index workflow appropriate to that source. The audit commands
intentionally have no automatic `--fix` mode.

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
2. Verify config file path (see [MCP Clients](../mcp-clients.md))
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

- **File watcher overlap**: both servers watch `memory_dirs`. A file created by one server may be re-indexed by the other, causing duplicate chunks. Restart the server that has stale data, or force a full re-index (`mem_index(force=True)`) to reconcile.
- **Orphaned index entries**: interrupted concurrent writes could previously leave orphaned FTS/vec entries causing `constraint failed` errors on subsequent indexing. This is now handled automatically — `upsert_chunks` defensively cleans orphans before INSERT.
- **Recommendation**: for typical usage, run only the MCP server. Launch the Web UI on-demand when you need visual browsing.

---

## STM: Proactive Memory Surfacing (Optional)

The **[memtomem-stm](https://github.com/memtomem/memtomem-stm)** package adds a
proxy that sits between your AI agent and other MCP servers. It automatically
recalls relevant memories when your agent uses any tool. STM is distributed as
a separate package; it communicates with memtomem core entirely through the MCP
protocol — no direct code coupling. Enhanced composition is negotiated from
`mem_do(action="version").capabilities`, not inferred from an installed package
version: `context_compose` schema 2 enables scoped Pinned Context composition,
schema 3 additionally preserves adjacent context-window chunks, and
`candidate_propose` schema 1 independently enables review-first proposals.

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

### Install

```bash
pip install memtomem-stm
```

For setup, CLI usage, compression strategies, surfacing configuration, and the full tool list, see the [memtomem-stm README](https://github.com/memtomem/memtomem-stm#readme).

---

## Uninstalling memtomem

See [`uninstall.md`](../uninstall.md) for the five-step removal flow: detach the MCP server from each editor, uninstall the Python package, delete `~/.memtomem/`, clean up project-scoped `.memtomem/` and generated rule files, and optionally prune memtomem hooks from `~/.claude/settings.json`.

---
