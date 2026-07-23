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

Normal registrations contain only the server command. The server reads the
configuration written by `mm init` from `~/.memtomem/config.json`. Add an
`env` block only when you deliberately want that client to override the saved
configuration; environment variables have the highest precedence.

---

**Jump to your editor**

- [1. Claude Code](#1-claude-code)
- [2. Cursor](#2-cursor)
- [3. Windsurf](#3-windsurf)
- [4. Claude Desktop](#4-claude-desktop)
- [5. Gemini CLI](#5-gemini-cli)
- [6. Kimi CLI](#6-kimi-cli)
- [7. Codex CLI](#7-codex-cli)
- [8. OpenCode](#8-opencode)
- [9. Antigravity](#9-antigravity)
- [10. Verifying Your Connection](#10-verifying-your-connection)
- [11. Environment Variable Overrides](#11-environment-variable-overrides)
- [12. Network transports (advanced)](#12-network-transports-advanced)
- [Troubleshooting](#troubleshooting)
- [Next Steps](#next-steps)

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
claude mcp add memtomem -- memtomem-server

# User scope — available in every project
claude mcp add memtomem -s user -- memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Both write to `~/.claude.json` — no need to edit that file by hand.

These commands reuse the persistent environment that provides `mm`. If you
intentionally do not install memtomem, replace `memtomem-server` with `uvx
--isolated --from "memtomem[all]==0.3.12" memtomem-server`.

For the safe plugin experience (bundled MCP server plus six focused skills),
install `/plugin install memtomem@memtomem`. Prompt retrieval and write-time
indexing are a separate opt-in `/plugin install memtomem-automation@memtomem`.
Note that both manual commands above differ from the plugin's pinned launch
command (`uvx --from memtomem==0.3.12 memtomem-server`), so installing the
plugin on top of a manual entry runs **two** servers against the same store.
The [Claude Code integration guide](integrations/claude-code.md) shows how to
check for the duplicate and which registration to keep.

### Project scope — commit a `.mcp.json`

For a team-shared setup, create a `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "memtomem-server",
      "args": []
    }
  }
}
```

Teammates see this server after approving Claude Code's workspace-trust
prompt on first use.

<a id="verify-connection"></a>
### Verify Claude Code

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
      "command": "memtomem-server",
      "args": []
    }
  }
}
```

Restart Cursor after configuration.
<a id="verify-connection-1"></a>
### Verify Cursor

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
      "command": "memtomem-server",
      "args": []
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
      "command": "memtomem-server",
      "args": []
    }
  }
}
```

Windows path: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after configuration.

---

## 5. Gemini CLI

> **Deprecated upstream.** Google is transitioning Gemini CLI to the
> Antigravity CLI (see [§9](#9-antigravity)). Gemini CLI stopped serving
> free/Pro/Ultra individual tiers on **2026-06-18** (enterprise Gemini Code
> Assist Standard/Enterprise keep it). New setups should prefer the
> **Antigravity CLI (`agy`)** instructions in §9.

Create or edit the `~/.gemini/settings.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "memtomem-server",
      "args": []
    }
  }
}
```

Restart Gemini CLI after configuration.

---

## 6. Kimi CLI

Kimi CLI reads MCP servers from `~/.kimi/mcp.json` by default. If
`KIMI_SHARE_DIR` is set, write `mcp.json` under that directory instead.

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "memtomem-server",
      "args": []
    }
  }
}
```

You can also let `mm init` write the file:

```bash
mm init --mcp kimi
```

Restart Kimi CLI after configuration.

---

## 7. Codex CLI

### Option A: Install the memtomem plugin

The repository marketplace bundles the exact-pinned MCP server and six Codex
skills:

```bash
codex plugin marketplace add /path/to/memtomem
codex plugin add memtomem@memtomem
```

Start a new thread after installation. See the [Codex integration guide](integrations/codex.md).

### Option B: Register only the MCP server

Codex CLI reads manually registered MCP servers from `~/.codex/config.toml` under the
`[mcp_servers.<id>]` section header — TOML, not JSON. Add a section for
memtomem:

```toml
[mcp_servers.memtomem]
command = "memtomem-server"
args = []
```

Restart Codex CLI after configuration.

> Codex MCP tools default to **serialized** calls. memtomem is safe to
> run in parallel — opt in by adding `supports_parallel_tool_calls = true`
> alongside `command`/`args`. Other server-level knobs documented by
> Codex (e.g. `enabled`, `enabled_tools`, `disabled_tools`,
> `startup_timeout_sec`, `tool_timeout_sec`) all work; see the official
> [Codex config reference](https://developers.openai.com/codex/config-reference)
> for the full schema.

<a id="verify-connection-2"></a>
### Verify Codex CLI

In Codex CLI:

```
Call mem_status to check the memtomem connection status
```

---

## 8. OpenCode

The published npm plugin is `opencode-memtomem@0.1.2`. Add it through
OpenCode's plugin form — `{"plugin": ["opencode-memtomem@0.1.2"]}` in
`opencode.json`. The plugin supplies an exact-pinned MCP server, six commands,
three read-only skills, and conservative permissions.

If you only need the MCP tools — without the plugin's bundled commands and
skills — configure the released MCP server directly instead:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memtomem": {
      "type": "local",
      "command": ["uvx", "--isolated", "--from", "memtomem[all]==0.3.12", "memtomem-server"],
      "enabled": true,
      "timeout": 60000,
      "environment": {"MEMTOMEM_TOOL_MODE": "core"}
    }
  }
}
```

See the [OpenCode integration guide](integrations/opencode.md) for precedence,
permission, platform, and development-install details.

---

## 9. Antigravity

"Antigravity" ships as two distinct surfaces with **separate** MCP config
files — register memtomem in whichever you actually use:

- **Antigravity IDE** (the desktop app / VS Code fork) — configured through
  the Agent panel UI below.
- **Antigravity CLI** (`agy`, Google's terminal-native successor to Gemini
  CLI) — configured by editing `~/.gemini/antigravity-cli/mcp_config.json`
  directly; see [Antigravity CLI (`agy`)](#antigravity-cli-agy) below.

### Antigravity IDE

1. Click the `...` menu at the top of the Agent panel > **MCP Servers**
2. Click **Manage MCP Servers** at the top of the MCP Store
3. Select **View raw config** > `mcp_config.json` will open
4. Add the memtomem server configuration:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "memtomem-server",
      "args": []
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

### Antigravity CLI (`agy`)

The Antigravity CLI is a **separate** surface from the IDE above: it reads
MCP servers from `~/.gemini/antigravity-cli/mcp_config.json` (key
`mcpServers`), **not** the IDE's `~/.gemini/antigravity/mcp_config.json`.
Create or edit that file:

```json
{
  "mcpServers": {
    "memtomem": {
      "type": "stdio",
      "command": "memtomem-server",
      "args": []
    }
  }
}
```

Restart the `agy` session after editing. The CLI also reads your existing
`~/.gemini/GEMINI.md` context and can pull in Gemini/Claude plugins via
`agy plugin import`, so memory indexed with `mm ingest gemini-memory` keeps
working unchanged.

---

## 10. Verifying Your Connection

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

<a id="available-mcp-tools-96"></a>

### Available MCP Tools

| Category | Tools |
|----------|-------|
| **Search** | `mem_search` (hybrid BM25+Dense+RRF, optional `as_of` for temporal-validity queries), `mem_recall` (date-range retrieval), `mem_expand` (context-window expansion) |
| **Browse** | `mem_list` (indexed sources), `mem_read` (chunk by UUID) |
| **CRUD** | `mem_add`, `mem_edit`, `mem_delete`, `mem_batch_add`, `mem_add_redaction_stats` |
| **Indexing** | `mem_index` (file/directory indexing, optional `auto_tag`) |
| **Meta** | `mem_do` (routes to all registered actions, supports aliases — including `schedule_register`, `schedule_list`, `schedule_run_now`, `schedule_delete` for cron jobs) |
| **Ask** | `mem_ask` (natural-language Q&A over indexed memories) |
| **Namespace** | `mem_ns_list`, `mem_ns_set`, `mem_ns_get`, `mem_ns_assign`, `mem_ns_update`, `mem_ns_rename`, `mem_ns_delete` |
| **Tags** | `mem_tag_list`, `mem_tag_rename`, `mem_tag_merge`, `mem_tag_delete`, `mem_auto_tag` |
| **Cross-ref** | `mem_link`, `mem_unlink`, `mem_related` |
| **Fetch** | `mem_fetch` (URL → markdown → index) |
| **Sessions** | `mem_session_start` (with optional `title`), `mem_session_end`, `mem_session_list` |
| **Working Memory** | `mem_scratch_set`, `mem_scratch_get`, `mem_scratch_promote` |
| **Procedures** | `mem_procedure_save`, `mem_procedure_list` |
| **Multi-Agent** | `mem_agent_register`, `mem_agent_search`, `mem_agent_share` |
| **Consolidation** | `mem_consolidate`, `mem_consolidate_apply` |
| **Reflection** | `mem_reflect`, `mem_reflect_save` |
| **History** | `mem_search_history`, `mem_search_feedback`, `mem_search_suggest` |
| **Conflict** | `mem_conflict_check` |
| **Importance** | `mem_importance_scan` |
| **Entity** | `mem_entity_scan`, `mem_entity_search` |
| **Temporal** | `mem_timeline`, `mem_activity` |
| **Policy** | `mem_policy_add`, `mem_policy_list`, `mem_policy_delete`, `mem_policy_run` |
| **Schedule** | `mem_schedule_register`, `mem_schedule_list`, `mem_schedule_run_now`, `mem_schedule_delete` (cron-driven compaction, decay, dead-link cleanup, dedup; also reachable as `mem_do` actions) |
| **Health** | `mem_watchdog`, `mem_cleanup_orphans` |
| **Import** | `mem_import_notion`, `mem_import_obsidian` |
| **Maintenance** | `mem_dedup_scan`, `mem_dedup_merge`, `mem_decay_scan`, `mem_decay_expire` |
| **Data** | `mem_export`, `mem_import` |
| **Config** | `mem_stats`, `mem_status`, `mem_config`\*, `mem_embedding_reset`\*, `mem_reset`\* |
| **Evaluation** | `mem_eval` |
| **Quality Lab** | `mem_quality_replay` (replay stored eval cases into a deterministic retrieval-quality report; promote/compare stay on `mm quality`) |
| **Context** | `mem_context_detect`, `mem_context_init`, `mem_context_generate`, `mem_context_diff`, `mem_context_sync`, `mem_context_memory_migrate`, `mem_context_artifact_migrate`, `mem_context_artifact_transfer`, `mem_context_version`, `mem_context_promote`, `mem_context_pull` — cross-runtime artifact push and pull (`mm context`). Parameters: [Context tool reference](reference.md#context-tool-parameters); workflow: [Context Gateway](context-gateway.md) |
| **Pinned Context** | `mem_pinned_list`, `mem_pinned_get`, `mem_pinned_set`, `mem_pinned_delete`, `mem_context_compose` |
| **Formation** | `mem_formation_scan`, `mem_candidate_propose`, `mem_candidate_list`, `mem_candidate_review`, `mem_candidate_recover` |


\* Exposed as an individual tool only under `MEMTOMEM_TOOL_MODE=full`. The actions stay reachable in `core` and `standard` mode through the dispatcher — `mem_do(action="config", params={...})`, `mem_do(action="embedding_reset", params={...})`, `mem_do(action="reset", params={...})` — and the CLI equivalents are `mm config`, `mm embedding-reset`, and `mm reset`.

> **Tool mode**: Set `MEMTOMEM_TOOL_MODE` to `core` (9 names, default), `standard` (38 names), or `full` (99 current tools plus the deprecated `mem_context_migrate` alias, 100 registered names) to control how many tools are exposed. In `core` mode, use `mem_do(action="...", params={...})` to access any non-core action. Fewer tools = less context usage for AI agents.

`mem_candidate_propose(content, source, source_ref, idempotency_key)` lets an
external agent submit a review candidate without writing durable memory.
`content` must be non-empty and at most 2,000 characters; `source` and
`idempotency_key` are required. The content and source reference pass the
privacy scanner, accepted proposals remain pending for up to 30 days, and a
reused idempotency key returns the original proposal unless its content differs.
`mem_context_migrate` is a deprecated alias for
`mem_context_memory_migrate` and is scheduled for removal in v0.5.0.

### STM Proxy Tools (optional, separate package)

The STM proxy is distributed as a separate package: **[memtomem-stm](https://github.com/memtomem/memtomem-stm)**. Once installed and configured, it exposes additional tools including `stm_proxy_stats`, `stm_proxy_select_chunks`, `stm_proxy_read_more`, `stm_proxy_cache_clear`, `stm_surfacing_feedback`, `stm_surfacing_stats`, and dynamically proxied upstream tools (`{prefix}__{tool}`). See the [memtomem-stm README](https://github.com/memtomem/memtomem-stm#readme) for full setup and tool reference.

---

## 11. Environment Variable Overrides

You can override settings for one client by adding environment variables to
the `env` block. This is an advanced, explicit override: these values win over
`config.d/` and `config.json`. For ordinary setup, omit the block and manage
settings with `mm init`, `mm config`, or the Web UI.

> **List-typed settings must be JSON-encoded.** `MEMTOMEM_INDEXING__MEMORY_DIRS`
> is a list, so pass it as a JSON array literal string: `"[\"~/memories\"]"`
> — not a bare path. Passing a plain string will crash the MCP server on
> startup with a pydantic-settings parse error.

### Common Configuration Options

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "memtomem-server",
      "args": [],
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
      "command": "memtomem-server",
      "args": [],
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

## 12. Network transports (advanced)

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

> **Trusted-network only — no first-party authentication.** Both network
> transports speak the MCP protocol with **no built-in authentication**, and
> memtomem ships none of its own by design (the stance and the rejected
> static-token option are recorded in
> [ADR-0029](../adr/0029-mcp-network-transport-auth-stance.md); see also the
> [SECURITY.md](https://github.com/memtomem/memtomem/blob/main/SECURITY.md)
> MCP-transport section). Bind to loopback (`127.0.0.1`) and put an
> authenticated reverse proxy in front before exposing publicly — see
> [Authenticated reverse proxy](#authenticated-reverse-proxy-required-for-public-exposure)
> below. Do not expose them on the public internet without that layer.

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

### Authenticated reverse proxy (required for public exposure)

`memtomem-server` ships **no first-party MCP authentication** — once a
network transport is reachable by an untrusted client, it grants full LTM
read plus file-touching tool access with no credential check (stance:
[ADR-0029](../adr/0029-mcp-network-transport-auth-stance.md)). Anything
beyond a trusted LAN therefore needs an authenticating reverse proxy that
**terminates TLS and rejects unauthenticated requests** before they reach
the loopback listener.

The recipe below uses nginx HTTP Basic auth in front of a loopback-bound
server. Keep the server on `127.0.0.1` and pair it with a public `--url` so
its DNS-rebinding allow-lists match the forwarded `Host` / `Origin`:

```bash
# 1. Create a credential (repeat without -c to add more users)
htpasswd -c /etc/nginx/memtomem.htpasswd alice

# 2. Run the server bound to loopback, named by its PUBLIC url
memtomem-server \
  --transport http \
  --host 127.0.0.1 \
  --port 8000 \
  --url https://mcp.example.com/mcp
```

```nginx
# /etc/nginx/sites-available/memtomem-mcp
server {
    listen 443 ssl;
    server_name mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;

    # The auth gate memtomem does not provide itself.
    auth_basic           "memtomem MCP";
    auth_basic_user_file /etc/nginx/memtomem.htpasswd;

    location /mcp {
        proxy_pass         http://127.0.0.1:8000/mcp;
        proxy_http_version 1.1;

        # Forward the public host so it matches the server's --url-derived
        # Host/Origin allow-lists (the loopback listener still validates them).
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # nginx has already enforced auth_basic above; strip the credential so
        # it is never forwarded to the unauthenticated backend (or its logs).
        proxy_set_header   Authorization     "";

        # Streamable HTTP and SSE hold long-lived connections — disable
        # response buffering and raise the idle timeout so the proxy does not
        # cut the stream with a premature 504.
        proxy_buffering    off;
        proxy_read_timeout 1h;
    }
}
```

Your MCP client connects to `https://mcp.example.com/mcp` and must send the
matching credential. Prefer an `Authorization: Basic <base64>` header if your
client supports custom MCP headers. Some clients only accept credentials in
the URL (`https://alice:<password>@mcp.example.com/mcp`) — avoid that where you
can: URL userinfo leaks easily into shell history, config files, and client /
proxy logs. If it is your only option, use a dedicated, low-privilege,
rotatable credential rather than a shared one.

> **A static Bearer token is not an upgrade.** Gating the same proxy on a
> shared `Authorization: Bearer <token>` (via nginx `map` / `if`, or an
> `auth_request` subrequest to your own authz service) is also fine, but a
> shared static token has no per-client identity, revocation, or expiry — a
> leak means full read + write access until you rotate it and restart. That
> is exactly why memtomem does **not** bake one in (ADR-0029); the proxy is
> the right place for whatever auth your deployment actually needs.

> **Do not pair this with `--disable-dns-rebinding-protection` unless the
> proxy validates `Origin` itself.** Disabling the built-in Host/Origin
> check removes memtomem's last defense if a request ever reaches the
> listener without passing through the proxy.

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

[Concurrent MCP + Web server]: reference/operations.md#concurrent-mcp--web-server

---

## Troubleshooting

### Tools don't appear in my editor

1. **Restart your editor** after changing MCP configuration
2. Check that you used `memtomem-server` (not `memtomem`) in your config
3. Verify the install is reachable: `mm --version` (or `uvx --from memtomem mm --version` for uvx-only setups) — side-effect-free
4. From inside the editor, ask it to call the `mem_status` tool — a successful response confirms the MCP handshake reached the server

> Running `uvx --isolated --from "memtomem[all]==0.3.12" memtomem-server`
> bare in a terminal prints
> a setup hint (MCP client configuration plus the network-transport
> examples from §12) and exits — it is **not** a "does it serve?" smoke
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
