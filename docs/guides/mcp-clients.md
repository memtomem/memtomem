# MCP Client Configuration Guide

**Audience**: Users who want to connect memtomem to a specific AI editor
**Prerequisite**: [Getting Started](getting-started.md) complete (embedding path picked — BM25 default, or ONNX / Ollama / OpenAI via the wizard; optional cross-encoder reranker)
**Estimated Time**: ~5 minutes

> **Which editor should I use?**
> Any MCP-compatible editor works. If you're new, **Claude Code** is recommended — it has the simplest setup (one command).

### Key distinction

| Command | What it is | When to use |
|---------|-----------|-------------|
| `memtomem-server` | **MCP server** — runs in the background, connects to your editor | Always use this in MCP config |
| `memtomem` (or `mm`) | **CLI tool** — terminal commands for search, index, etc. | Optional, for terminal use |

> **Common mistake**: Using `memtomem` instead of `memtomem-server` in your MCP config will fail.

---

## 1. Claude Code

### Pick a scope

Claude Code has three install scopes — pick one based on how you want to
share the server:

| Scope | Flag | Shared with | Storage |
|-------|------|-------------|---------|
| local (default) | `-s local` (or omit `-s`) | This project × this user only | `~/.claude.json` → `projects."<cwd>".mcpServers` |
| project | `-s project` (or commit `.mcp.json`) | Everyone who clones the repo | `<project-root>/.mcp.json` |
| user | `-s user` | This user across every project | `~/.claude.json` → top-level `mcpServers` |

Precedence is `local > project > user`, so a `local` entry can override
a shared team `project` server when you need to test with personal
credentials.

> **Different axis from memtomem's canonical tier.** The `-s local /
> -s project / -s user` flag above selects where Claude Code records
> the MCP server entry; memtomem's own canonical artifacts (agents,
> skills, commands, memory) carry an independent `user / project_shared
> / project_local` **tier** (ADR-0016) chosen per write via `mm context
> ... --scope=<tier>`. Keeping the two axes separate is intentional —
> server registration and canonical residency are unrelated decisions.

### Add via command (`local` / `user`)

```bash
# Local scope (default) — this project only, not committed
claude mcp add memtomem -- uvx --from memtomem memtomem-server

# User scope — available in every project
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Both write to `~/.claude.json` — no need to edit that file by hand.

For the full plugin experience (slash commands, automation hooks, memory curator agent), see the [Claude Code integration guide](integrations/claude-code.md).

### Project scope — commit a `.mcp.json`

For a team-shared setup, create a `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Teammates see this server after approving Claude Code's workspace-trust
prompt on first use.

### Verify Connection

In Claude Code (or run `/memtomem:status` with the plugin):
```
Call the mem_status tool
```

---

## 2. Cursor

Create or edit the `~/.cursor/mcp.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Restart Cursor after configuration.
### Verify Connection

In Cursor's AI chat:
```
Call mem_status to check the memtomem connection status
```

---

## 3. Windsurf

Create or edit the `~/.codeium/windsurf/mcp_config.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Restart Windsurf after configuration.

---

## 4. Claude Desktop

Edit the `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Windows path: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after configuration.

---

## 5. Gemini CLI

Create or edit the `~/.gemini/settings.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]"
      }
    }
  }
}
```

Restart Gemini CLI after configuration.

---

## 6. Codex CLI

Codex CLI reads MCP servers from `~/.codex/config.toml` under the
`[mcp_servers.<id>]` section header — TOML, not JSON. Add a section for
memtomem:

```toml
[mcp_servers.memtomem]
command = "uvx"
args = ["--from", "memtomem", "memtomem-server"]

[mcp_servers.memtomem.env]
MEMTOMEM_INDEXING__MEMORY_DIRS = '["~/memories"]'
```

Restart Codex CLI after configuration.

> Codex MCP tools default to **serialized** calls. memtomem is safe to
> run in parallel — opt in by adding `supports_parallel_tool_calls = true`
> alongside `command`/`args`. Other server-level knobs documented by
> Codex (e.g. `enabled`, `enabled_tools`, `disabled_tools`,
> `startup_timeout_sec`, `tool_timeout_sec`) all work; see the official
> [Codex config reference](https://developers.openai.com/codex/config-reference)
> for the full schema.

### Verify Connection

In Codex CLI:

```
Call mem_status to check the memtomem connection status
```

---

## 7. Antigravity

1. Click the `...` menu at the top of the Agent panel > **MCP Servers**
2. Click **Manage MCP Servers** at the top of the MCP Store
3. Select **View raw config** > `mcp_config.json` will open
4. Add the memtomem server configuration:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"/path/to/notes\"]"
      }
    }
  }
}
```

> Antigravity does not support the `${workspaceFolder}` variable — use absolute paths.
> Restart the Agent session after changing settings.

> **Two MCP config locations inside Antigravity.** The steps above register
> memtomem with the **built-in Gemini agent**, which reads
> `~/.gemini/antigravity/mcp_config.json` (key `mcpServers`). VS Code-side
> integrations inside Antigravity — the MCP panel, Copilot Chat, Cline,
> Claude extension, etc. — read a separate file at
> `~/Library/Application Support/Antigravity/User/mcp.json` (key `servers`,
> VS Code's standard MCP schema). Antigravity does **not** inherit MCP
> entries from a sibling VS Code install; each fork keeps its own
> `Application Support/<AppName>/User/` directory. Register memtomem in
> whichever file matches the agent you plan to call it from — or both, if
> you use both.

---

## 8. Verifying Your Connection

These verification methods work across all clients.

### Calling mem_status

Ask the AI:

```
Call the mem_status tool to show the current status
```

Expected response (BM25 default — the `Embedding` and `Dimension`
lines change depending on the provider picked in the wizard):

```
memtomem Status
==============
Storage:   sqlite
DB path:   ~/.memtomem/memtomem.db
Embedding: none /
Dimension: 0
Top-K:     10
RRF k:     60

Index stats
-----------
Total chunks:  0
Source files:  0
...
```

The full report also includes an `Immutable fields` block (provider /
model / tokenizer / backend echoed back as a "what can't be changed at
runtime" reminder), and a `Warnings` block with stable schema keys
(`kind` / `fix` / `doc` / `stored` / `configured`) when an embedding-
dimension mismatch is detected. Run `mm status` from a terminal to see
the exact output your install produces.

### From a terminal — `mm status`

If the editor isn't reachable yet (or you want to verify the install
without involving any client), run the same check from a terminal:

```bash
mm status
```

`mm status` is a thin CLI wrapper over the same code path `mem_status`
uses, so the output is identical. Useful as a sanity check between
`mm init` and the first editor-side call.

### Available MCP Tools (80)

| Category | Tools |
|----------|-------|
| **Search** | `mem_search` (hybrid BM25+Dense+RRF, optional `as_of` for temporal-validity queries), `mem_recall` (date-range retrieval), `mem_expand` (context-window expansion) |
| **Browse** | `mem_list` (indexed sources), `mem_read` (chunk by UUID) |
| **CRUD** | `mem_add`, `mem_edit`, `mem_delete`, `mem_batch_add` |
| **Indexing** | `mem_index` (file/directory indexing, optional `auto_tag`) |
| **Meta** | `mem_do` (routes to all registered actions, supports aliases — including `schedule_register`, `schedule_list`, `schedule_run_now`, `schedule_delete` for cron jobs) |
| **Ask** | `mem_ask` (natural-language Q&A over indexed memories) |
| **Namespace** | `mem_ns_list`, `mem_ns_set`, `mem_ns_get`, `mem_ns_assign`, `mem_ns_update`, `mem_ns_rename`, `mem_ns_delete` |
| **Tags** | `mem_tag_list`, `mem_tag_rename`, `mem_tag_delete`, `mem_auto_tag` |
| **Cross-ref** | `mem_link`, `mem_unlink`, `mem_related` |
| **Fetch** | `mem_fetch` (URL → markdown → index) |
| **Sessions** | `mem_session_start` (with optional `title`), `mem_session_end`, `mem_session_list` |
| **Working Memory** | `mem_scratch_set`, `mem_scratch_get`, `mem_scratch_promote` |
| **Procedures** | `mem_procedure_save`, `mem_procedure_list` |
| **Multi-Agent** | `mem_agent_register`, `mem_agent_search`, `mem_agent_share` |
| **Consolidation** | `mem_consolidate`, `mem_consolidate_apply` |
| **Reflection** | `mem_reflect`, `mem_reflect_save` |
| **History** | `mem_search_history`, `mem_search_suggest` |
| **Conflict** | `mem_conflict_check` |
| **Importance** | `mem_importance_scan` |
| **Entity** | `mem_entity_scan`, `mem_entity_search` |
| **Temporal** | `mem_timeline`, `mem_activity` |
| **Policy** | `mem_policy_add`, `mem_policy_list`, `mem_policy_delete`, `mem_policy_run` |
| **Health** | `mem_watchdog`, `mem_cleanup_orphans` |
| **Import** | `mem_import_notion`, `mem_import_obsidian` |
| **Maintenance** | `mem_dedup_scan`, `mem_dedup_merge`, `mem_decay_scan`, `mem_decay_expire` |
| **Data** | `mem_export`, `mem_import` |
| **Config** | `mem_stats`, `mem_status`, `mem_config`\*, `mem_embedding_reset`\*, `mem_reset`\* |
| **Evaluation** | `mem_eval` |
| **Context** | `mem_context_detect`, `mem_context_init`, `mem_context_generate`, `mem_context_diff`, `mem_context_sync`, `mem_context_migrate` (context tools accept `include="skills,agents,commands"` for canonical artifact workflows; `init`, `generate`, `sync`, and `diff` accept `scope="project_shared\|user\|project_local"` — the canonical **tier** per ADR-0016 §2; `generate`/`sync` also accept `on_drop="ignore\|warn\|error"` (and the legacy alias `strict=True` ≡ `on_drop="error"`) to control how dropped sub-agent or command fields are reported; `migrate` takes `from_scope`/`to_scope` instead of a single `scope` because it has two endpoints, plus `apply=True` to execute and `confirm_project_shared=True` when writing to the git-tracked tier) |

\* Requires `MEMTOMEM_TOOL_MODE=full`. In `core` or `standard` mode, use `mm config` (CLI) or the Web UI Settings tab instead.

> **Tool mode**: Set `MEMTOMEM_TOOL_MODE` to `core` (9 tools, default), `standard` (core + common packs + `mem_do`), or `full` (all 80 tools individually) to control how many tools are exposed. In `core` mode, use `mem_do(action="...", params={...})` to access any non-core action. Fewer tools = less context usage for AI agents.

### STM Proxy Tools (optional, separate package)

The STM proxy is distributed as a separate package: **[memtomem-stm](https://github.com/memtomem/memtomem-stm)**. Once installed and configured, it exposes additional tools including `stm_proxy_stats`, `stm_proxy_select_chunks`, `stm_proxy_read_more`, `stm_proxy_cache_clear`, `stm_surfacing_feedback`, `stm_surfacing_stats`, and dynamically proxied upstream tools (`{prefix}__{tool}`). See the [memtomem-stm README](https://github.com/memtomem/memtomem-stm#readme) for full setup and tool reference.

---

## 9. Environment Variable Overrides

You can override settings by adding environment variables to the `env` block.

> **List-typed settings must be JSON-encoded.** `MEMTOMEM_INDEXING__MEMORY_DIRS`
> is a list, so pass it as a JSON array literal string: `"[\"~/memories\"]"`
> — not a bare path. Passing a plain string will crash the MCP server on
> startup with a pydantic-settings parse error.

### Common Configuration Options

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]",
        "MEMTOMEM_STORAGE__SQLITE_PATH": "~/.memtomem/memtomem.db",
        "MEMTOMEM_EMBEDDING__MODEL": "nomic-embed-text"
      }
    }
  }
}
```

### Changing the Embedding Model

The recommended Ollama embedding model is `nomic-embed-text` (768d). To use a different model, set `MEMTOMEM_EMBEDDING__MODEL` and `MEMTOMEM_EMBEDDING__DIMENSION` in the `env` block.

**Example: BGE-M3 (1024d)**

```bash
ollama pull bge-m3
```

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/memories\"]",
        "MEMTOMEM_EMBEDDING__MODEL": "bge-m3",
        "MEMTOMEM_EMBEDDING__DIMENSION": "1024"
      }
    }
  }
}
```

| Model | Dimension | Pull Command |
|-------|-----------|-------------|
| `nomic-embed-text` | 768 | `ollama pull nomic-embed-text` |
| `bge-m3` (multilingual) | 1024 | `ollama pull bge-m3` |

> The server default is `provider = "none"` (BM25 keyword-only, no
> embedding model). The models above are Ollama-specific choices; the
> wizard also exposes ONNX (`fastembed`) and OpenAI options.

> **Important**: `MEMTOMEM_EMBEDDING__DIMENSION` must match the model's output dimension. Mismatched values will cause indexing errors.

> **Security Note**: Instead of placing API keys directly in configuration files, it is recommended to use an OS keychain or environment variable management tool.

---

## 10. Network transports (advanced)

Every editor section above launches `memtomem-server` over **stdio** — the
MCP client spawns the server as a subprocess and talks to it on stdin/stdout.
That is the right transport for almost every setup; you don't need this
section unless you specifically want a long-running server an editor on a
different machine can connect to over the network.

`memtomem-server` also supports two MCP **network transports**:

| Transport | Flag | Notes |
|-----------|------|-------|
| Streamable HTTP | `--transport http` (alias for `streamable-http`) | Recommended for new deployments |
| Server-Sent Events | `--transport sse` | Older transport, kept for editors that haven't moved off SSE |

> **Trusted-network only.** Both network transports speak the MCP protocol
> with **no built-in authentication**. Bind to loopback (`127.0.0.1`) and
> put an authenticated reverse proxy in front before exposing publicly.
> Do not expose them on the public internet without that layer.

### Quick start — Streamable HTTP on loopback

```bash
memtomem-server \
  --transport http \
  --host 127.0.0.1 \
  --port 8000 \
  --url http://127.0.0.1:8000/mcp
```

The server prints the internal and public URLs at startup and waits for
client connections. Stop it with `Ctrl+C`.

### Behind a reverse proxy

`--url` is the **public endpoint your MCP client connects to**, and the
URL path is also used as the server's internal endpoint path. Forward the
public path unchanged to the internal listener:

```bash
memtomem-server \
  --transport http \
  --host 127.0.0.1 \
  --port 8000 \
  --url https://mcp.example.com/mcp
```

For SSE, the URL path is split into a mount point plus the SSE endpoint
(`https://mcp.example.com/memtomem/events` → mount `/memtomem`, endpoint
`/events`).

### DNS rebinding protection

By default, `memtomem-server` validates the `Host` and `Origin` headers
on every request and returns 421 / 403 on a mismatch. This blocks
DNS-rebinding attacks against the local listener.

The `Host` and `Origin` allow-lists are seeded **asymmetrically**:

- **`Host` allow-list** auto-includes loopback variants (`127.0.0.1` /
  `localhost` / `[::1]`, each in bare and `:*` port-wildcard form), the
  hostname from `--url` (bare and `:*` form), the explicit `--host` value
  when it isn't a wildcard, plus any `--allowed-host` values you pass.
- **`Origin` allow-list** auto-includes **only** the scheme+host+port
  derived from `--url` (e.g. `--url https://mcp.example.com/mcp` →
  `https://mcp.example.com`), plus any `--allowed-origin` values you
  pass. Loopback Origins are **not** auto-allowed: a browser-style
  client sending `Origin: http://127.0.0.1:<port>` against a server
  configured with a non-loopback `--url` will be rejected. Pass
  `--allowed-origin http://127.0.0.1:<port>` (or `--allowed-origin
  http://localhost:*`) to test the internal listener from a browser.

- `--allowed-host VALUE` — extra `Host` header to accept. Repeatable.
  Matching is **exact** unless the value ends in `:*` (port wildcard);
  most clients send `Host: <hostname>:<port>`, so for non-default ports
  pass either `<hostname>:*` or the exact `<hostname>:<port>`.
- `--allowed-origin VALUE` — extra `Origin` header to accept. Repeatable.
  Same matching rules — typical browser-style Origins look like
  `http://<hostname>:<port>`, so use `<scheme>://<hostname>:*` or the
  full `<scheme>://<hostname>:<port>`.
- `--disable-dns-rebinding-protection` — turn the check off entirely.
  **Only safe behind an authenticated reverse proxy** that already
  validates the request origin.

> **`--host 0.0.0.0` binds on all interfaces but does not auto-allow them.**
> If you bind to `0.0.0.0` without passing a `--url` whose hostname
> matches how clients reach you, the allow-list stays loopback-only
> and LAN clients are rejected (HTTP 421 / 403). The simplest fix is
> to pass `--url http://<reachable-host>:<port>/mcp` — `memtomem-server`
> derives both port-wildcard (`<reachable-host>:*`) and exact
> (`<reachable-host>:<port>`) entries from that URL and adds the matching
> `Origin`. **Advanced:** if you must keep a loopback `--url`, pass both
> `--allowed-host <reachable-host>:*` **and**
> `--allowed-origin http://<reachable-host>:<port>` — a bare
> `--allowed-host <reachable-host>` does **not** match `Host:
> <reachable-host>:<port>` (the SDK only treats `:*`-suffixed values as
> port wildcards) and Origin-bearing clients are blocked separately.

### One server at a time

`memtomem-server` takes a per-user pid lock regardless of transport.
Run **either** the stdio server (spawned by your editor) **or** a network
server, not both — a second launch logs a warning about concurrent writes
and leaves the primary server's pid file in place. If you need both MCP
and Web UI access concurrently, see the [Concurrent MCP + Web server]
section in the reference guide.

[Concurrent MCP + Web server]: reference.md#concurrent-mcp--web-server

---

## Troubleshooting

### Tools don't appear in my editor

1. **Restart your editor** after changing MCP configuration
2. Check that you used `memtomem-server` (not `memtomem`) in your config
3. Verify the install is reachable: `mm --version` (or `uvx --from memtomem mm --version` for uvx-only setups) — side-effect-free
4. From inside the editor, ask it to call the `mem_status` tool — a successful response confirms the MCP handshake reached the server

> Running `uvx --from memtomem memtomem-server` bare in a terminal prints
> a setup hint (MCP client configuration plus the network-transport
> examples from §10) and exits — it is **not** a "does it serve?" smoke
> test because no MCP client is connected. To test stdio, configure your
> editor and call `mem_status` from there; to test a network transport,
> start the server with `--transport http|sse` and connect a client.

### "Connection refused" or timeout

1. Check that Ollama is running: `ollama list`
2. For source installs, verify the `--directory` path is correct
3. Check for port conflicts if using SSE transport

### Embedding mismatch warning

Your database was created with a different embedding model than your current config.
```bash
mm embedding-reset                          # check status
mm embedding-reset --mode apply-current     # reset to current model
mm index ~/notes                            # re-index
```

---

## Next Steps

- [Getting Started](getting-started.md) — Setup wizard and first use
- [Reference](reference.md) — Complete feature reference
