# OpenCode x memtomem Integration Guide

`opencode-memtomem` is a configuration-only OpenCode plugin. It installs the
exact-pinned memtomem MCP server, six slash commands, and three read-only
skills without adding event hooks or automatic indexing.

## Compatibility

- OpenCode `>=1.17.18 <2`
- macOS, Linux, or Windows through WSL
- `uvx` available on `PATH`

Native Windows has not yet been verified.

## Install today

The npm plugin is not published yet. Configure memtomem as a local MCP server
in `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memtomem": {
      "type": "local",
      "command": ["uvx", "--isolated", "--from", "memtomem[all]==0.3.11", "memtomem-server"],
      "enabled": true,
      "timeout": 60000,
      "environment": {"MEMTOMEM_TOOL_MODE": "core"}
    }
  }
}
```

Restart OpenCode and call `memtomem_mem_status`. This manual path exposes the
MCP tools but not the plugin's bundled slash commands and skills.

After the npm package is published, add it through OpenCode's singular
`plugin` configuration key (there is no `opencode plugin add` command):

```json
{"plugin": ["opencode-memtomem@0.1.1"]}
```

For development from this repository, build the package and point the same
`plugin` array at `packages/opencode-memtomem/dist/server.js`.

Restart OpenCode, then verify:

```text
/memtomem-status
/memtomem-search deployment decisions
```

## Included surfaces

| Surface | Included behavior |
|---|---|
| MCP | Exact-pinned `memtomem==0.3.11`, core tool mode (plugin); `[all]` no-install runtime (manual MCP) |
| Commands | `memtomem-search`, `recall`, `status`, `remember`, `index`, `setup` |
| Skills | Read-only `search`, `recall`, and `status` |

OpenCode prefixes MCP tools with the server name, so prompts use names such as
`memtomem_mem_search`.

## Permission policy

The plugin allows the dedicated read tools, asks before add/index, and denies
the broad `memtomem_mem_do` dispatcher. Existing memtomem-specific permission
rules, an existing `mcp.memtomem`, same-name commands, and same-name user
skills take precedence.

OpenCode's `--auto` option approves `ask` decisions. Agent-level permissions
can also override global permissions, so review both before using write
commands. The plugin deliberately does not install automation hooks; indexing
occurs only through an explicit command or tool call.
