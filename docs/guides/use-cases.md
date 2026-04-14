# Practical Use Cases

**Audience**: Users who want to learn how to combine memtomem MCP tools in real-world workflows
**Prerequisite**: MCP server connected (see [Quick Start](getting-started.md), [MCP Client Configuration](mcp-clients.md))

> **Agent developers**: For advanced memory patterns (episodic, working memory, procedures, multi-agent, reflection), see the [Agent Memory Guide](agent-memory-guide.md).

---

## Getting Started with Coding Tools

### Claude Code

```bash
# PyPI
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Source (if running from git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

> User: "Call mem_stats"
> Agent: `mem_stats()` returns:
> ```
> Memory index statistics:
> - Total chunks: 0
> - Total sources: 0
> - Storage backend: sqlite
> ```

> User: "Index my notes directory"
> Agent: `mem_index(path="/path/to/notes")` returns:
> ```
> Indexing complete:
> - Files scanned: 47
> - Total chunks: 1284
> - Indexed: 1284
> - Skipped (unchanged): 0
> - Deleted (stale): 0
> - Duration: 3200ms
> ```

### Cursor

Edit `~/.cursor/mcp.json`:

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

Restart Cursor, then in chat: "Call mem_stats to check the memtomem connection status"

### Antigravity

Agent panel > `...` > **MCP Servers** > **Manage MCP Servers** > **View raw config**, then add:

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

Restart the Agent session, then ask: "Call mem_stats to check the memtomem status"

---

## Scenario Index

| # | Scenario | Key Tools |
|---|----------|-----------|
| 1 | [Daily Development Notes](#1-daily-development-notes) | `mem_add`, `mem_recall` |
| 2 | [Architecture Decision Records (ADR)](#2-architecture-decision-records-adr) | `mem_index`, `mem_search` |
| 3 | [Team Onboarding Knowledge Base](#3-team-onboarding-knowledge-base) | `mem_batch_add`, `mem_search` |
| 4 | [Meeting Notes Management + Time-Based Queries](#4-meeting-notes-management--time-based-queries) | `mem_add`, `mem_recall` |
| 5 | [Code Review & Debugging Context](#5-code-review--debugging-context) | `mem_add`, `mem_search` |
| 6 | [Automated Memory Integration with Claude Code Hooks](#6-automated-memory-integration-with-claude-code-hooks) | hooks + `mem_search`, `mem_index` |

---

### 1. Daily Development Notes

**Situation**: Record today's work and retrieve it later by date.

**Example agent conversation**:

> User: "I changed the Redis cache eviction policy from LRU to LFU today. Record it."
> Agent: `mem_add(content="Changed Redis cache eviction policy from LRU to LFU. Modified maxmemory-policy in config/redis.yml.", tags="redis,cache,config")` > Saved

> User: "What did I work on last week?"
> Agent: `mem_recall(since="2026-03-01", until="2026-03-08", limit=10)` > Returns list of recent notes

**Tip**: If you omit the `file` parameter, notes are automatically appended to a date-stamped `.md` file.

---

### 2. Architecture Decision Records (ADR)

**Situation**: Manage project architecture decisions in markdown and search them later by keywords.

**Example agent conversation**:

> User: "Index the docs/adr/ directory"
> Agent: `mem_index(path="docs/adr/")` > "Indexed 12 files, 38 chunks"

> User: "Find architecture decisions related to databases"
> Agent: `mem_search(query="database architecture decision")` > Returns ADR-003, ADR-007

**Tip**: memtomem chunks by markdown headings, so well-structured ADRs produce precise search results.

---

### 3. Team Onboarding Knowledge Base

**Situation**: Register frequently asked questions and answers for new developers all at once, then let them search later.

**Example agent conversation**:

> User: "Register the onboarding FAQ all at once"
> Agent: `mem_batch_add(entries=[{"key": "dev-env-setup", "value": "..."}, {"key": "ci-cd-pipeline", "value": "..."}, ...])` > "Added 8 entries"

> User: "What does the CI/CD pipeline look like?"
> Agent: `mem_search(query="CI/CD pipeline structure")` > Returns relevant entries

**Tip**: `mem_batch_add` internally organizes entries as `## key` headings in a single markdown file.

---

### 4. Meeting Notes Management + Time-Based Queries

**Situation**: Record meeting content and retrieve it by time range.

**Example agent conversation**:

> User: "Record today's sprint meeting. Backend team completed the auth module refactoring, frontend will apply the design system next week."
> Agent: `mem_add(content="Sprint meeting: Backend — Auth module refactoring complete. Frontend — Design system adoption planned for next week.", tags="meeting,sprint")` > Saved

> User: "Show me the meeting notes from February"
> Agent: `mem_recall(since="2026-02", until="2026-03")` > List of February meeting notes

**Tip**: `until` is an exclusive upper bound. To query all of February, set `until="2026-03"`.

---

### 5. Code Review & Debugging Context

**Situation**: Record recurring bug patterns or code review feedback, then search when similar errors occur.

**Example agent conversation**:

> User: "Record this bug pattern: When an exception occurs in asyncio.gather, the remaining tasks are not automatically cancelled. Solved by using TaskGroup."
> Agent: `mem_add(content="asyncio.gather exception handling caveat: When one task raises an exception, the remaining tasks are not automatically cancelled. Solution: Use asyncio.TaskGroup (Python 3.11+).", tags="python,asyncio,bug-pattern")` > Saved

> User: "Were there any issues with asyncio task cancellation?"
> Agent: `mem_search(query="asyncio task cancellation exception handling")` > Returns the previously recorded bug pattern

**Tip**: Using a consistent tag like `"bug-pattern"` lets you find all bug patterns with `tag_filter="bug-pattern"`.

---

### 6. Automated Memory Integration with Claude Code Hooks

**Situation**: Automate `mem_search`/`mem_add` with hooks so the agent automatically injects relevant context and saves results without manual tool calls.

**Required Tools**: Claude Code hooks + memtomem CLI (`uv tool install memtomem`, or `uv run mm ...` from a git clone)

**Recommended Hooks** (UserPromptSubmit + PostToolUse only):

| Hook Event | Description | CLI Command |
|------------|-------------|-------------|
| `UserPromptSubmit` | Auto-search related memories on prompt submission | `mm search "$PROMPT" --top-k 3 --format context` |
| `PostToolUse` (Write) | Auto-reindex after new file creation | `mm index "$FILE_PATH"` |

**Configuration**: `~/.claude/settings.json`

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
    }]
  }
}
```

> **Note**: Hooks use the `mm` CLI (not `memtomem-server`). The CLI requires separate installation (`uv tool install memtomem`, or `uv run mm ...` from a git clone). The MCP server is only for AI client connections.

**Important caveats**:

- **Short prompt filtering**: The `[ ${#P} -gt 20 ]` guard skips searches for short prompts like "yes", "ok", "commit" that would return irrelevant results.
- **Input sanitization**: `printf '%s'` and `head -c 500` prevent shell injection from prompt content and cap query length.
- **Stop hook = session close**: Use `mm session end --auto` in the Stop hook to close the active session with a structured summary (see [hooks.md](hooks.md)). Don't use a naive Stop hook (e.g., `mm add "Session end: ..."`) — raw timestamps pollute search results.
- **Error logging**: Use `2>>/tmp/mm-hook.log` instead of `2>/dev/null` so you can diagnose real failures (DB corruption, disk full) without disrupting the session.
- **PostToolUse scope**: Matching only `Write` (not `Edit`) avoids redundant re-indexing on every small edit. Edited files are already indexed; only new files need it.
- **STM proxy overlap**: If you use the [memtomem-stm](https://github.com/memtomem/memtomem-stm) proxy (separate package), it already provides automatic memory surfacing and indexing. Hooks are redundant in that setup.

---

## Next Steps

- [User Guide](user-guide.md) — Complete feature walkthrough
- [MCP Client Configuration](mcp-clients.md) — Editor-specific setup
- [Hands-On Tutorial](hands-on-tutorial.md) — Step-by-step walkthrough
