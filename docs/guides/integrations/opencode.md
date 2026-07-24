# OpenCode x memtomem Integration Guide

`opencode-memtomem` is a configuration-only OpenCode plugin. It installs the
exact-pinned memtomem MCP server, six slash commands, and three read-only
skills without adding event hooks or automatic indexing.

## Compatibility

- OpenCode `>=1.17.18 <2`
- macOS, Linux, or Windows through WSL
- `uvx` available on `PATH`

Native Windows has not yet been verified.

## Install

The published npm release is `opencode-memtomem@0.1.2`, bundling core
`0.3.12`. Version `0.1.3` — the one this repository's source describes,
bundling core `0.3.13` — is not on npm yet. Until it is, the manual MCP
configuration below is the recommended path; it pulls the `0.3.13` runtime
from PyPI directly.

After `0.1.3` is published, add it through OpenCode's singular `plugin`
configuration key (there is no `opencode plugin add` command):

```json
{"plugin": ["opencode-memtomem@0.1.3"]}
```

For development from this repository, build the package and point the same
`plugin` array at `packages/opencode-memtomem/dist/server.js`.

Restart OpenCode, then verify:

```text
/memtomem-status
/memtomem-search deployment decisions
```

### Manual MCP alternative

If you only need the MCP tools — without the plugin's bundled slash commands
and skills — configure memtomem as a local MCP server in `opencode.json`
instead:

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

Restart OpenCode and call `memtomem_mem_status` to verify this path.

## Included surfaces

| Surface | Included behavior |
|---|---|
| MCP | Exact-pinned `memtomem==0.3.13`, core tool mode (plugin); `[all]` no-install runtime (manual MCP) |
| Commands | `memtomem-search`, `recall`, `status`, `remember`, `index`, `setup` |
| Skills | Read-only `search`, `recall`, and `status` |

OpenCode prefixes MCP tools with the server name, so prompts use names such as
`memtomem_mem_search`.

## Permission policy

The plugin allows the dedicated read tools, asks before add/index, and denies
the broad `memtomem_mem_do` dispatcher. Existing memtomem-specific permission
rules, an existing `mcp.memtomem`, same-name commands, and same-name user
skills take precedence. That precedence is keyed on the exact `memtomem`
name: a manual server registered under any other key (say
`mcp."memtomem-local"`) is not deduplicated — both servers would run against
the same store, with tools under both prefixes — so keep manual entries named
`memtomem`.

OpenCode's `--auto` option approves `ask` decisions. Agent-level permissions
can also override global permissions, so review both before using write
commands. The plugin deliberately does not install automation hooks; indexing
occurs only through an explicit command or tool call.
