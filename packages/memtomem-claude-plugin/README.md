# memtomem — Claude Code Plugin

Safe Claude Code workflows for the memtomem Markdown-first memory MCP server.
BM25 works without an embedding provider; dense retrieval is optional.

One install bundles:

- **Exact-pinned MCP server** — launched on demand with the reviewed core version
- **Read workflows** — `/memtomem:search`, `/memtomem:recall`, `/memtomem:status`
- **Explicit workflows** — `/memtomem:remember`, `/memtomem:index`, `/memtomem:setup`

The base plugin does not run background hooks or destructive curation. Install
`memtomem-automation@memtomem` separately to opt into prompt-time retrieval and
write-time indexing.

## Install

```
/plugin marketplace add memtomem/memtomem
/plugin install memtomem@memtomem
```

On a completely fresh machine or HOME, initialize the user-owned store once:

```bash
uvx --from 'memtomem==0.3.12' mm init --preset minimal --non-interactive
mm status
```

The plugin intentionally cannot perform this trust-establishing step over MCP.
For project-specific memories, preserve the project root and create the
gitignored local tier explicitly:

```bash
cd /path/to/project
mm mem init --scope project_local
```

After that, `/memtomem:setup /path/to/notes` performs a one-shot index and
verifies search. One-shot indexing does not add a watched source directory.

If you previously registered the server manually, Claude Code suppresses
the plugin-managed copy (nothing runs twice) and your manual entry keeps
winning. Remove it (`claude mcp remove memtomem`) to switch to the
plugin-managed server.

## Docs

- [Claude Code integration guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/integrations/claude-code.md)
  — setup, optional automation, and CLAUDE.md guidelines
- [memtomem README](https://github.com/memtomem/memtomem) — project overview
- [MCP clients guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md)
  — manual registration for other clients
