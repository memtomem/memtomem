# Claude Code Automation with Hooks

**Audience**: Users who want to automate memtomem memory in Claude Code
**Prerequisite**: memtomem CLI installed (`uv tool install memtomem`, or `uv run mm ...` from a git clone), using Claude Code

---

## Overview

Claude Code's hook system can automate manual MCP tool calls.

| Feature | Manual | Automated with Hooks |
|---------|--------|---------------------|
| Search related memories on prompt | Call `mem_search` each time | **Automatic** — UserPromptSubmit hook |
| Reindex after `.md` edits | Call `mem_index` each time | **Automatic** — PostToolUse hook |
| Save summary on session end | Lost if forgotten | **Automatic** — Stop hook |

> **Note**: Hooks require the CLI (`uv tool install memtomem`, or `uv run mm ...` from a git clone). `mm` is a shorthand alias for `memtomem`. The MCP server (`memtomem-server`) is a separate entry point for AI client connections.

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
        "command": "mm search \"${prompt}\" --top-k 3 --format context 2>/dev/null || true",
        "timeout": 5000
      }]
    }],
    "PostToolUse": [{
      "matcher": "Write|Edit|MultiEdit",
      "hooks": [{
        "type": "command",
        "command": "mm index \"${tool_input.file_path}\" 2>/dev/null || true",
        "timeout": 10000
      }]
    }],
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "mm add \"Session end: $(date '+%Y-%m-%d %H:%M')\" --tags session,auto 2>/dev/null || true",
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

### PostToolUse — Automatic Indexing

When Claude modifies a file with Write/Edit/MultiEdit, it is automatically reindexed.

### Stop — Session Summary Save

When a session ends, key content is automatically saved to memory.

---

## CLI Commands Used by Hooks

| Command | Description |
|---------|-------------|
| `mm search "query" --top-k 3 --format context` | Search memory, output markdown for context injection |
| `mm index /path/to/file` | Index a file or directory |
| `mm add "content" --tags "tag1,tag2"` | Add a memory entry |

---

## Troubleshooting

### Hook Error When Ollama Is Not Running

Hook commands use the `2>/dev/null || true` pattern to suppress errors.
Even if Ollama is down, the Claude session will not be interrupted.

To inspect errors, temporarily remove `2>/dev/null` from the command.

### Hooks Not Taking Effect

1. Verify the settings file path: `~/.claude/settings.json` (global) or `.claude/settings.json` (project)
2. Restart Claude Code after modifying settings
3. Test the CLI command directly: `mm search "test" --top-k 3`

---

## Next Steps

- [Practical Use Cases](use-cases.md) — Agent workflow scenarios
- [MCP Client Configuration](mcp-clients.md) — Editor-specific MCP setup
