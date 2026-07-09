# memtomem — Claude Code Plugin

Claude Code plugin for the memtomem long-term memory MCP server
(hybrid BM25 + dense search across markdown memories).

One install bundles:

- **MCP server** — launched on demand via `uvx --from memtomem memtomem-server`
  (requires [uv](https://docs.astral.sh/uv/) on PATH)
- **Slash commands** — `/memtomem:setup`, `/memtomem:remember`,
  `/memtomem:search`, `/memtomem:recall`, `/memtomem:index`,
  `/memtomem:status`, `/memtomem:summarize`
- **Automation hooks** — session lifecycle, prompt-time memory surfacing,
  write-time indexing (these shell out to the `mm` CLI, so install it with
  `uv tool install 'memtomem[all]'` to activate them)
- **memory-curator agent** — dedup / auto-tag / decay curation

## Install

```
/plugin marketplace add memtomem/memtomem
/plugin install memtomem@memtomem
```

If you previously registered the server manually, Claude Code suppresses
the plugin-managed copy (nothing runs twice) and your manual entry keeps
winning. Remove it (`claude mcp remove memtomem`) to switch to the
plugin-managed server.

## Docs

- [Claude Code integration guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/integrations/claude-code.md)
  — setup, hooks reference, CLAUDE.md guidelines
- [memtomem README](https://github.com/memtomem/memtomem) — project overview
- [MCP clients guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md)
  — manual registration for other clients
