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
| Track tool activity | Call `mem_add` manually | **Automatic** — PostToolUse activity hook |
| Close session on stop | Call `mem_session_end` | **Automatic** — Stop hook |

> **Note**: Hooks use the `mm` CLI, not the MCP server. Install via `uv tool install memtomem` (recommended), `pipx install memtomem`, or use `uv run mm ...` from a source checkout.

> **STM proxy users**: If you use the [memtomem-stm](https://github.com/memtomem/memtomem-stm) proxy (separate package), it already provides automatic memory surfacing and indexing. Hooks are redundant in that setup and can be skipped.

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
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [{
          "type": "command",
          "command": "mm index \"${tool_input.file_path}\" 2>>/tmp/mm-hook.log || true",
          "timeout": 10000
        }]
      },
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [{
          "type": "command",
          "command": "mm activity log --type tool_call -c \"${tool_name}\" 2>>/tmp/mm-hook.log || true",
          "timeout": 3000
        }]
      }
    ],
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

### PostToolUse — Activity Tracking

When Claude uses a mutation tool (Write, Edit, MultiEdit), the tool name is logged to the current session's activity timeline via `mm activity log`. These events are viewable in `mm web` under the Sessions panel.

Activity logging only runs if a session is active (`mm session start` was called). If no session is active, the hook silently skips.

### Stop — Session Auto-Close

When the agent stops, any active session is automatically closed with an auto-generated summary (event counts). This replaces a naive `mm add` approach — raw timestamps pollute search, while session summaries are structured and filterable.

---

## Session Workflow

Start a session before working, and the hooks handle the rest:

```bash
mm session start --agent-id "developer" --title "Feature: auth module"
# ... work in Claude Code ...
# PostToolUse hooks log Write/Edit activity automatically
# Stop hook closes the session when done

mm session list                    # view all sessions
mm session events <session-id>     # view activity timeline
```

For headless automation (ralph loops, `claude -p`):

```bash
mm session wrap --agent-id "qa-bot" -- claude -p "run the test suite"
```

---

## CLI Commands Used by Hooks

| Command | Description |
|---------|-------------|
| `mm search "query" --top-k 3 --format context` | Search memory, output markdown for context injection |
| `mm index /path/to/file` | Index a file or directory |
| `mm add "content" --tags "tag1,tag2"` | Add a memory entry |
| `mm session start --agent-id NAME` | Start a tracked session |
| `mm session end --auto` | End session with auto-generated summary |
| `mm activity log --type TYPE -c "..."` | Log an activity event to current session |
| `mm session wrap -- COMMAND` | Wrap a command with session lifecycle |

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
