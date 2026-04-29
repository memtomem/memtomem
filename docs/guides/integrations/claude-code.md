# Claude Code x memtomem Integration Guide

**Audience**: Developers using Claude Code who want to build memory automation with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Claude Code installed
**Estimated Time**: About 15 minutes

---

## Overview

Claude Code has its own memory system including CLAUDE.md, MEMORY.md, topic files, and hooks.
memtomem **does not replace** these, but complements Claude Code with **semantic search** it lacks.
The most powerful automation pipeline is achieved when combined with Claude Code's hooks system.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| Project instructions (full loading) | CLAUDE.md |
| Auto-memory index (200-line limit) | MEMORY.md + auto-memory |
| Per-topic file on-demand reading | Claude Code built-in |
| Project document **semantic search** | memtomem (`mem_search`) |
| Hooks-based automation pipeline | memtomem CLI + hooks |

---

## MCP Server Setup

### Pick an installation scope

Claude Code offers three MCP configuration scopes. Pick the one that
matches how you want to share this server:

| Scope | Storage | Shared with | When to use |
|-------|---------|-------------|-------------|
| `local` (default) | `~/.claude.json` → `projects."<cwd>".mcpServers` | Only this project × this user | Personal setup — private paths/tokens, or testing before committing |
| `project` | `<project-root>/.mcp.json` (committed to git) | Everyone who clones the repo | Team-wide shared server |
| `user` | `~/.claude.json` → top-level `mcpServers` | This user across every project | General-purpose server not tied to one project |

**Precedence** when the same server name exists in multiple scopes:
`local` > `project` > `user` > plugins > Claude.ai connectors. Adding a
`local` entry lets you override a shared `project` server with personal
credentials without editing the committed file.

**Trust prompt**: `project` servers from `.mcp.json` require
workspace-trust approval on first use — cloning an unknown repo never
silently spawns an MCP server.

### Add via command (`local` / `user`)

```bash
# User scope — install once, available in every project
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Local scope — this project only, not committed (omitting -s is the same)
claude mcp add memtomem -s local -- uvx --from memtomem memtomem-server

# Source install (running from a git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Both `-s local` and `-s user` write to `~/.claude.json` — no need to edit
that file by hand.

### Project scope via `.mcp.json`

For a team-shared setup, commit a `.mcp.json` at the project root:

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

Teammates see this server after approving the workspace-trust prompt on
first use. To run against personal credentials without touching the
shared file, add a `-s local` entry with the same name — local wins.

---

## Verify Connection

In Claude Code (or run `/memtomem:status` if using the plugin):

```
Call the mem_status tool
```

Example of a successful response (Ollama config; first 12 lines of the
full report — the `Embedding` and `Dimension` rows change with the
provider picked in the wizard):

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
...
```

Or skip the editor and run the same check directly:

```bash
mm status
```

`mm status` is a CLI mirror of `mem_status` (same output) — handy when
the editor hasn't reconnected yet, or for scripted health checks.

> **MCP Reconnection**: After changing `.mcp.json`, restart Claude Code or use the `/mcp` command to reconnect.

---

## First Indexing

```
Index my ~/notes directory
```

Agent:
```
mem_index(path="~/notes", recursive=True)
→ Indexing complete:
  - Files scanned: 47
  - Total chunks: 1284
  - Indexed: 1284
  - Skipped (unchanged): 0
  - Deleted (stale): 0
  - Duration: 3200ms
```

---

## Hooks Automation Setup

> **Plugin users**: Hooks are included in the plugin. You only need to install the CLI for them to activate:
> ```bash
> uv tool install 'memtomem[all]'   # or: pipx install 'memtomem[all]'
> ```
> Skip to [Tool Usage Guidelines](#tool-usage-guidelines-add-to-claudemd) if you're using the plugin.

You can automate memtomem using Claude Code's hooks system.
Add the following to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "mm session start --idempotent --auto-end-stale 24h --agent-id claude-code 2>>/tmp/mm-hook.log || true",
        "timeout": 5000
      }]
    }],
    "UserPromptSubmit": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "P=$(printf '%s' \"${prompt}\" | head -c 500); [ ${#P} -gt 20 ] && mm search \"$P\" --top-k 3 --format context 2>>/tmp/mm-hook.log || true",
        "timeout": 5000
      }]
    }],
    "PostToolUse": [{
      "matcher": "Write",
      "hooks": [{
        "type": "command",
        "command": "FP=\"${tool_input.file_path}\"; case \"$FP\" in *.md|*.py|*.ts|*.tsx|*.js|*.jsx|*.go|*.rs|*.rb|*.java|*.kt|*.swift|*.c|*.cpp|*.h|*.hpp|*.sh|*.toml|*.yaml|*.yml|*.json) ;; *) exit 0 ;; esac; case \"$FP\" in node_modules/*|*/node_modules/*|dist/*|*/dist/*|build/*|*/build/*|target/*|*/target/*|.next/*|*/.next/*|.nuxt/*|*/.nuxt/*|__pycache__/*|*/__pycache__/*|.git/*|*/.git/*|.venv/*|*/.venv/*|venv/*|*/venv/*|coverage/*|*/coverage/*|.cache/*|*/.cache/*) exit 0 ;; esac; mm index --debounce-window 5 \"$FP\" 2>>/tmp/mm-hook.log || true",
        "timeout": 10000
      }]
    }],
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "mm index --flush 2>>/tmp/mm-hook.log; mm session end --auto 2>>/tmp/mm-hook.log || true",
        "timeout": 10000
      }]
    }]
  }
}
```

### Hook Event Summary

| Hook Event | Trigger Timing | memtomem Action |
|------------|---------------|----------------|
| `SessionStart` | When a Claude Code session starts | `mm session start --idempotent --auto-end-stale 24h` → Resume the active session for `claude-code`, or open a new one and close orphans older than 24h |
| `UserPromptSubmit` | When a prompt is submitted | `mm search` → Automatically inject relevant memory into context |
| `PostToolUse` (Write) | After new file creation | `mm index --debounce-window 5` → Record the file in the debounce queue and drain entries silent ≥5s; rapid consecutive writes restart the window so a burst is indexed once at the end |
| `Stop` | When the agent stops | `mm index --flush; mm session end --auto` → Synchronously flush any pending debounced files, then close the session with a structured summary |

### Automation Flow

```
Claude Code starts
  → SessionStart hook → mm session start --idempotent (resume or open)
  → User submits prompt (>20 chars)
  → UserPromptSubmit hook → mem_search context injection
  → Claude creates new files
  → PostToolUse hook → mm index --debounce-window 5 (record + drain stale)
  → Agent stops
  → Stop hook → mm index --flush; mm session end --auto
```

### Important Caveats

- **Short prompt guard**: Prompts under 20 characters are skipped to avoid noise from "yes", "ok", etc.
- **Input sanitization**: `printf '%s'` + `head -c 500` prevent shell injection and cap query length.
- **Error logging**: `2>>/tmp/mm-hook.log` preserves errors for debugging. Avoid `2>/dev/null` which hides real failures.
- **Stop hook = session close**: Use `mm session end --auto` in the Stop hook to close the active session with a structured summary. Don't use a Stop hook to call `mm add` with raw timestamps — those pollute search.
- **SessionStart hook = idempotent resume**: `mm session start --idempotent` resumes the active session for the same `--agent-id` instead of creating a new row, so a Claude Code restart inherits the previous session's `mm activity log` writes. `--auto-end-stale 24h` closes any active session older than 24h before the idempotency check — this is how orphans from a crashed previous run get cleaned up. The 24h cutoff also means that resuming Claude Code the morning after deliberately ends the prior day's session and starts fresh; lower the cutoff (e.g. `30m`) if you want shorter resume windows, raise it (e.g. `7d`) if you want sessions to span breaks. The idempotent path is single-process safe but not concurrency-safe — two parallel SessionStart hooks could both create new sessions; Claude Code's hook runner fires them serially per session, which is the supported case.
- **Write only**: `Edit` is excluded from PostToolUse — edited files are already indexed, so re-indexing on every edit is redundant.
- **Allowlist + blocklist**: `PostToolUse[Write]` only indexes canonical source extensions (`md`, `py`, `ts`/`tsx`, `js`/`jsx`, `go`, `rs`, `rb`, `java`, `kt`, `swift`, `c`/`cpp`/`h`/`hpp`, `sh`, `toml`, `yaml`/`yml`, `json`) and skips build / cache / VCS paths (`node_modules`, `dist`, `build`, `target`, `.next`, `.nuxt`, `__pycache__`, `.git`, `.venv`/`venv`, `coverage`, `.cache`) inline. Patterns include both leading-segment (`node_modules/*`) and any-segment (`*/node_modules/*`) forms for both absolute and relative `tool_input.file_path` values. Extension matching is case-sensitive — `*.MD` / `*.JS` would skip the allowlist; rename or extend the patterns if your repo uses uppercase. Adjust the `case` statements in `hooks.json` for project-specific needs — they are inline, easy to extend.
- **Debounce mechanics**: `mm index --debounce-window 5` records the file in `~/.memtomem/index_debounce_queue.json` (flock-protected) and drains entries that have been silent ≥5 seconds. Each Write hook fire restarts the window for that path, so a codegen burst indexes the final state once after the burst ends rather than once per Write. The Stop hook chains `mm index --flush` (synchronous drain — blocks until every queued file is indexed) before `mm session end --auto` to ensure session-end indexing isn't deferred. `mm index --status` prints a snapshot of the queue (depth + oldest entry) for telemetry; it's race-prone and not a correctness primitive — for "is the queue empty?" use `--flush`. RFC-B (PreCompact, deferred — needs Claude Code's PreCompact payload contract) will use a future `mm index --flush --paths <list>` for selective drain at checkpoint time.
- **STM proxy overlap**: If using [memtomem-stm](https://github.com/memtomem/memtomem-stm) (separate package), hooks are redundant — the proxy already handles surfacing and indexing.

---

## Tool Usage Guidelines (Add to CLAUDE.md)

Adding the following to your project's `CLAUDE.md` helps Claude Code properly utilize memtomem tools:

```markdown
## Memory Tool Usage Guidelines

### Claude Code Built-in Memory (CLAUDE.md, MEMORY.md)
- Project rules, coding conventions, configuration guidelines
- Current conversation context, task progress
- Simple facts already in MEMORY.md

### memtomem (`mem_search`, `mem_index`, `mem_recall`)
- Project documents, ADRs, architecture document search
- Code pattern history, debugging records, decision records
- Detailed information beyond MEMORY.md's 200-line limit

### Principles
- Rules stated in CLAUDE.md → Follow as-is
- Something to find in project documents/history → `mem_search`
- Check recent work records → `mem_recall`

### Dual Memory Search (MEMORY.md + mem_search)
When users ask about past records or decisions ("previously", "what was decided", "what was it" etc.):
1. Check MEMORY.md (auto-loaded 200 lines) first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Fully Automated Hooks Pipeline

With hooks configured, tools operate automatically without manual invocation by the developer.

```
User: "Refactor the auth middleware"

1. UserPromptSubmit hook auto-executes
   → mem_search("auth middleware refactoring") → 3 related previous decisions injected
2. Claude analyzes existing code and performs refactoring
3. Claude creates new files → PostToolUse hook auto-indexes them
4. Claude saves key decisions via mem_add (agent-driven, not automated)
```

### Scenario B: Dual Memory Search -- Simultaneous MEMORY.md + mem_search

Claude Code's auto-memory only auto-loads the first 200 lines of MEMORY.md.
As a project grows, important information gets truncated.
**Adding the dual memory search principle to CLAUDE.md** ensures both sources are automatically checked.

Content to add to CLAUDE.md:
```markdown
## Memory Search Principle
When users ask about past records or decisions ("previously", "what was decided", "what was it" etc.):
1. Check MEMORY.md (auto-loaded 200 lines) first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

Conversation example:
```
User: "What was the caching strategy we decided on before?"

Agent:
1. Check MEMORY.md → Found some cache-related items ("Using Redis LRU")
2. mem_search("caching strategy decision") → Returns detailed decision record
3. Synthesize both results:
→ "MEMORY.md has 'Using Redis LRU' recorded, and according to the detailed record,
   the decision was for Redis Cluster + Local LRU 2-tier cache (2026-03-10)"
```

> **Key Point**: CLAUDE.md is auto-loaded at the start of every conversation, so stating the principle here
> enables dual search in all conversations without separate hooks.

### Scenario C: Project Document Indexing + Auto-Reference During Code Writing

```
User: "Implement endpoints based on the API design document"

Agent:
1. mem_search("API design endpoint spec") → Returns chunks from docs/api-spec.md
2. Generate code matching the spec
3. PostToolUse hook → Auto-indexes the generated file
→ "Previously created API" searchable in the next session
```

---

## Built-in Memory vs memtomem Comparison

| Feature | Claude Code Built-in | memtomem |
|---------|---------------------|---------|
| Semantic search | None (full loading or filename-based) | BM25 + Dense + RRF hybrid search |
| Auto memory | MEMORY.md 200-line limit | Unlimited semantic search |
| Hooks integration | Event emission only | Hooks + CLI for automation (SessionStart, UserPromptSubmit, PostToolUse, Stop) |

---

## Frequently Asked Questions

**Q: Does CLAUDE.md or MEMORY.md go away?**
No. memtomem operates as separate MCP tools (`mem_search`, etc.) and coexists independently with the existing system. Continue using CLAUDE.md for project instructions as before.

**Q: Do hooks slow down Claude Code?**
Searches typically complete within 100–500ms depending on the embedding provider. The `timeout: 5000` setting ensures hooks don't block the session. Errors are logged to `/tmp/mm-hook.log` instead of discarded, so you can diagnose issues without disrupting the session.

**Q: It doesn't work after changing `.mcp.json`.**
Restart Claude Code or use the `/mcp` command to reconnect to MCP servers. Old processes may be using cached modules.

**Q: Can the same content be stored in both auto-memory and memtomem?**
Yes. Auto-memory automatically extracts from conversations, while memtomem only handles explicitly indexed/added targets.

---

## Cross-runtime agent context with `mm context`

If you also use Gemini CLI or Codex CLI on the same repo, treat `.memtomem/` as the single source of truth and let memtomem fan it out. Claude Code is the richest target — it preserves every canonical sub-agent field (`name`, `description`, `tools`, `model`, `skills`, `isolation`) without loss, so Claude is the natural place to author canonical agents and skills.

```bash
# Mirror .memtomem/skills/<name>/SKILL.md to .claude/skills/, .gemini/skills/, .agents/skills/
mm context sync --include=skills

# Fan out .memtomem/agents/<name>.md to .claude/agents/, .gemini/agents/, .codex/agents/
mm context sync --include=agents

# Fan out .memtomem/commands/<name>.md to .claude/commands/*.md, .gemini/commands/*.toml, and ~/.codex/prompts/*.md
mm context sync --include=commands
```

Sub-agent conversions are lossy for non-Claude targets — Gemini drops `skills` + `isolation`, Codex additionally drops `tools`, `kind`, `temperature`. Slash commands fan out to all three runtimes — Codex keeps `description` / `argument-hint` and the `$ARGUMENTS` placeholder natively, dropping only `allowed-tools` and `model` (Codex custom prompts are upstream-deprecated; prefer a skill for new workflows). memtomem reports every dropped field; add `--strict` to fail if you need 1:1 fidelity. Run `mm context --help` for the full per-runtime field-drop matrix.

---

## Next Steps

- [Reference](../reference.md) — Complete feature reference
- [Configuration](../configuration.md) — All `MEMTOMEM_*` environment variables
