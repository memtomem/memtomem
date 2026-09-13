# memtomem — Claude Code Plugin

Safe Claude Code workflows for the memtomem Markdown-first memory MCP server.
BM25 works without an embedding provider; dense retrieval is optional.

One install bundles:

- **Exact-pinned MCP server** — launched on demand with the reviewed core version
- **Read workflows** — `/memtomem:search`, `/memtomem:recall`, `/memtomem:status`
- **Explicit workflows** — `/memtomem:remember`, `/memtomem:index`, `/memtomem:setup`,
  `/memtomem:handoff`

The base plugin does not run background hooks or destructive curation. Install
`memtomem-automation@memtomem` separately to opt into prompt-time retrieval and
write-time indexing.

## Install

Before installing, inspect existing servers with `/mcp` in Claude Code, then
run the read-only diagnostic from the project you want to inspect:

```bash
uvx --from "memtomem[all]==0.6.1" mm doctor --claude-mcp
```

The pin is there because installing the plugin does not put `mm` on your PATH —
it registers an MCP server, not the CLI. If you already have the CLI installed,
`mm doctor --claude-mcp` is the same check. Neither is required: `/mcp` plus the
scope-specific removal guidance below covers the same ground by hand.

It reports current registrations and predicts conflicts with this release's
plugin command. Exit codes: `0` no detected conflict, `1` duplicate risk,
`2` incomplete inspection. `--json` provides structured findings. Keep your
existing uv installation; resolve any manual MCP registration shown by the
check before installing. The check does not intercept `/plugin install`.

```
/plugin marketplace add memtomem/memtomem
/plugin install memtomem@memtomem
```

On a completely fresh machine or HOME, initialize the user-owned store once:

```bash
uvx --from 'memtomem==0.6.1' mm init --preset minimal --non-interactive --mcp skip
uvx --from 'memtomem==0.6.1' mm status
```

The plugin intentionally cannot perform this trust-establishing step over MCP.
`--mcp skip` keeps the bootstrap from adding a second MCP registration because
the plugin already supplies the server.
For project-specific memories, preserve the project root and create the
gitignored local tier explicitly:

```bash
cd /path/to/project
uvx --from 'memtomem==0.6.1' mm mem init --scope project_local
```

After that, `/memtomem:setup /path/to/notes` performs a one-shot index and
verifies search. One-shot indexing does not add a watched source directory.
Use `/memtomem:handoff save` to leave a compact, project-local checkpoint for
Claude Code, Codex CLI, or Kimi Code; see the cross-runtime guide below.

If you previously registered the server manually, identical command and
arguments are expected to let Claude suppress the plugin copy. Different
commands can expose duplicate tools; tool visibility does not establish live
process count or database identity. Check `/mcp` for the actual session state.

The `--mcp claude` and `--mcp json` initialization modes now check before
registration. A usable
existing connection is preserved and additional registration is skipped.
Conflicting or uninspectable configurations defer registration; use
`--mcp skip` to initialize the store separately. Failed or timed-out Claude
registration never creates a fallback `.mcp.json`.

To switch to the plugin, remove only the manual entry using the diagnostic's
name and scope, for example `claude mcp remove memtomem -s user`. To keep the
manual setup, use `/plugin uninstall memtomem@memtomem`. Neither action removes
your uv installation or memories. Custom launch wrappers, managed policies,
and unresolved project approval require manual review; session-only flags
must be checked in `/mcp`. The diagnostic does not execute configured servers.

## Docs

- [Korean Claude Code/Codex vibe-coding quickstart](https://github.com/memtomem/memtomem/blob/main/docs/guides/vibe-coding-getting-started-ko.md)
  — initialize one store, install one plugin, and verify a memory round trip
- [Claude Code integration guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/integrations/claude-code.md)
  — setup, optional automation, and CLAUDE.md guidelines
- [memtomem README](https://github.com/memtomem/memtomem) — project overview
- [MCP clients guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/mcp-clients.md)
  — manual registration for other clients
- [Cross-runtime handoff guide](https://github.com/memtomem/memtomem/blob/main/docs/guides/integrations/cross-runtime-handoff.md)
  — sequential Claude Code, Codex CLI, and Kimi Code work on one Mac
