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

After the npm package is published:

```bash
opencode plugin add opencode-memtomem@0.1.0
```

For development from this repository, build the package and point OpenCode's
plugin configuration at `packages/opencode-memtomem/dist/server.js`.

Restart OpenCode, then verify:

```text
/memtomem-status
/memtomem-search deployment decisions
```

## Included surfaces

| Surface | Included behavior |
|---|---|
| MCP | `uvx --from memtomem==0.3.10 memtomem-server`, core tool mode |
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
