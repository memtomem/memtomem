# Claude Code Automation with Hooks

**Audience**: Users who want to automate memtomem memory in Claude Code
**Prerequisite**: memtomem CLI installed (`uv tool install memtomem`, or `uv run mm ...` from a git clone), using Claude Code

---

## Overview

Claude Code's hook system can automate manual MCP tool calls.

| Feature | Manual | Automated with Hooks |
|---------|--------|---------------------|
| Search related memories on prompt | Call `mem_search` each time | **Automatic** — UserPromptSubmit hook |
| Reindex after new file creation | Call `mem_index` each time | **Automatic** — PostToolUse hook |

> **Note**: Hooks require the CLI (`uv tool install memtomem`, or `uv run mm ...` from a git clone). `mm` is a shorthand alias for `memtomem`. The MCP server (`memtomem-server`) is a separate entry point for AI client connections.

> **STM proxy users**: If you use the STM proxy (`memtomem-stm`), it already provides automatic memory surfacing and indexing. Hooks are redundant in that setup and can be skipped.

---

## Hook Configuration

Add the following to `~/.claude/settings.json` (or `.claude/settings.json` in your project root for per-project config):

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

---

## How Each Hook Works

### UserPromptSubmit — Automatic Memory Search

When a prompt is submitted, it searches for related memories and injects them into Claude's context.

```
User: "Tell me the deployment rollback procedure"
→ Hook searches memtomem for "deployment rollback procedure"
→ Top 3 results are injected into Claude context
→ Claude answers based on memory
```

**Safeguards built into the command**:
- `printf '%s'` prevents shell injection from prompt content
- `head -c 500` caps query length to avoid excessive processing
- `[ ${#P} -gt 20 ]` skips short prompts ("yes", "ok", "commit") that would return noise

### PostToolUse — Automatic Indexing

When Claude creates a new file with Write, it is automatically indexed. Only the `Write` tool is matched — `Edit` is excluded because edited files are typically already indexed, and matching every edit causes redundant re-indexing (10 edits in one task = 10 index calls).

---

## Why No Stop Hook?

A naive Stop hook like `mm add "Session end: 2026-04-09"` saves meaningless timestamps that pollute search results over time. If you need session summaries, let the agent decide what to save via `mem_add` during the conversation — the agent has context about what was important.

---

## CLI Commands Used by Hooks

| Command | Description |
|---------|-------------|
| `mm search "query" --top-k 3 --format context` | Search memory, output markdown for context injection |
| `mm index /path/to/file` | Index a file or directory |
| `mm add "content" --tags "tag1,tag2"` | Add a memory entry |

---

## Troubleshooting

### Checking Hook Errors

Hook commands log errors to `/tmp/mm-hook.log` instead of discarding them. Check this file to diagnose issues:

```bash
tail -20 /tmp/mm-hook.log
```

> **Caution**: Avoid `2>/dev/null` which silently hides real errors (DB corruption, disk full, embedding failures). Use `2>>/tmp/mm-hook.log` to preserve debuggability while keeping the session clean.

### Hooks Not Taking Effect

1. Verify the settings file path: `~/.claude/settings.json` (global) or `.claude/settings.json` (project)
2. Restart Claude Code after modifying settings
3. Test the CLI command directly: `mm search "test" --top-k 3`

---

## Next Steps

- [Practical Use Cases](use-cases.md) — Agent workflow scenarios
- [MCP Client Configuration](mcp-clients.md) — Editor-specific MCP setup
