# opencode-memtomem

Safe, configuration-only memtomem integration for OpenCode 1.17.18 through the
current v1 line. It adds an exact-pinned local MCP server, six slash commands,
and three read-only skills. It does not add event hooks or automatic indexing.

## Install

The published npm release is `opencode-memtomem@0.1.2` (bundling core
`0.3.13`). Do not use an `opencode plugin add` command; OpenCode loads npm
plugins from the singular `plugin` array in `opencode.json`:

```json
{"plugin": ["opencode-memtomem@0.1.2"]}
```

Restart OpenCode, then run `/memtomem-status` or `/memtomem-search topic`.
`uvx` must be available on `PATH`; the plugin starts the exact-pinned
`memtomem[all]==0.3.13` runtime on demand.

If you only need the MCP tools — without the bundled commands and skills —
configure the local MCP server directly instead:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memtomem": {
      "type": "local",
      "command": ["uvx", "--isolated", "--from", "memtomem[all]==0.3.13", "memtomem-server"],
      "enabled": true,
      "timeout": 60000,
      "environment": {"MEMTOMEM_TOOL_MODE": "core"}
    }
  }
}
```

The plugin supports macOS, Linux, and Windows through WSL. Native Windows has
not been verified.

## Safety and precedence

- Search, recall, status, stats, list, and read tools are allowed.
- Add and index require confirmation.
- The broad `mem_do` dispatcher is denied.
- An existing `mcp.memtomem`, command of the same name, same-named user skill,
  or memtomem-specific permission rule wins over the plugin default. The
  dedup is keyed on the exact `memtomem` name — a manual server under any
  other key is not deduplicated and both servers would run, so keep manual
  entries named `memtomem`.

OpenCode's `--auto` mode approves `ask` decisions, and agent-level permissions
can override global settings. Review those settings before using write commands.
