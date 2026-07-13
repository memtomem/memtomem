# opencode-memtomem

Safe, configuration-only memtomem integration for OpenCode 1.17.18 through the
current v1 line. It adds an exact-pinned local MCP server, six slash commands,
and three read-only skills. It does not add event hooks or automatic indexing.

## Install

```bash
opencode plugin add opencode-memtomem@0.1.0
```

Restart OpenCode, then run `/memtomem-status` or `/memtomem-search topic`.
`uvx` must be available on `PATH`; the plugin starts
`memtomem==0.3.9` on demand.

The plugin supports macOS, Linux, and Windows through WSL. Native Windows has
not been verified.

## Safety and precedence

- Search, recall, status, stats, list, and read tools are allowed.
- Add and index require confirmation.
- The broad `mem_do` dispatcher is denied.
- An existing `mcp.memtomem`, command of the same name, same-named user skill,
  or memtomem-specific permission rule wins over the plugin default.

OpenCode's `--auto` mode approves `ask` decisions, and agent-level permissions
can override global settings. Review those settings before using write commands.
