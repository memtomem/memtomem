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

Example of a successful response:
```
memtomem status:
  - Storage backend: SQLite
  - Total chunks: 0 (not yet indexed)
  - Embedding model: ollama/nomic-embed-text
```

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
        "command": "mm index \"${tool_input.file_path}\" 2>>/tmp/mm-hook.log || true",
        "timeout": 10000
      }]
    }],
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "mm session end --auto 2>>/tmp/mm-hook.log || true",
        "timeout": 5000
      }]
    }]
  }
}
```

### Hook Event Summary

| Hook Event | Trigger Timing | memtomem Action |
|------------|---------------|----------------|
| `UserPromptSubmit` | When a prompt is submitted | `mm search` → Automatically inject relevant memory into context |
| `PostToolUse` (Write) | After new file creation | `mm index` → Automatically index the new file |
| `Stop` | When the agent stops | `mm session end --auto` → Close session with structured summary |

### Automation Flow

```
User submits prompt (>20 chars)
  → UserPromptSubmit hook → mem_search context injection
  → Claude creates new files
  → PostToolUse hook → mem_index auto-indexing
  → Agent stops
  → Stop hook → mm session end --auto
```

### Important Caveats

- **Short prompt guard**: Prompts under 20 characters are skipped to avoid noise from "yes", "ok", etc.
- **Input sanitization**: `printf '%s'` + `head -c 500` prevent shell injection and cap query length.
- **Error logging**: `2>>/tmp/mm-hook.log` preserves errors for debugging. Avoid `2>/dev/null` which hides real failures.
- **Stop hook = session close**: Use `mm session end --auto` in the Stop hook to close the active session with a structured summary. Don't use a Stop hook to call `mm add` with raw timestamps — those pollute search.
- **Write only**: `Edit` is excluded from PostToolUse — edited files are already indexed, so re-indexing on every edit is redundant.
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
| Hooks integration | Event emission only | Hooks + CLI for automation (UserPromptSubmit, PostToolUse) |

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

# Fan out .memtomem/agents/<name>.md to .claude/agents/, .gemini/agents/, ~/.codex/agents/
mm context sync --include=agents

# Fan out .memtomem/commands/<name>.md to .claude/commands/*.md, .gemini/commands/*.toml, and ~/.codex/prompts/*.md
mm context sync --include=commands
```

Sub-agent conversions are lossy for non-Claude targets — Gemini drops `skills` + `isolation`, Codex additionally drops `tools`, `kind`, `temperature`. Slash commands fan out to all three runtimes — Codex keeps `description` / `argument-hint` and the `$ARGUMENTS` placeholder natively, dropping only `allowed-tools` and `model` (Codex custom prompts are upstream-deprecated; prefer a skill for new workflows). memtomem reports every dropped field; add `--strict` to fail if you need 1:1 fidelity. Run `mm context --help` for the full per-runtime field-drop matrix.

---

## Next Steps

- [Reference](../reference.md) — Complete feature reference
- [Configuration](../configuration.md) — All `MEMTOMEM_*` environment variables
