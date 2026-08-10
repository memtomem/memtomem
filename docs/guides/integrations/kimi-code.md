# Kimi Code x memtomem Integration Guide

Kimi Code can use the same local memtomem store as Claude Code and Codex CLI.
The integration has two independent parts: one stdio MCP registration for the
memory tools, and a portable Agent Skills bundle for guided workflows. The
bundle is not a Kimi plugin and does not install hooks or slash commands.

## Register the MCP server

Kimi reads `~/.kimi-code/mcp.json`, or `$KIMI_CODE_HOME/mcp.json` when that
environment variable is set. `memtomem==0.4.0` writes that path, so
`uvx --from 'memtomem==0.4.0' mm init --mcp kimi` is the shortest route.
Releases up to and including `0.3.14` wrote the legacy `~/.kimi/mcp.json`
layout, which current Kimi Code does not read — move the entry if you
registered with one of those. To write the file by hand instead, it needs
exactly one `memtomem` entry:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem==0.4.0", "memtomem-server"],
      "env": {"MEMTOMEM_TOOL_MODE": "core"}
    }
  }
}
```

From a source checkout of this repository, `uv run mm init --mcp kimi`
already writes the current path and can replace the manual file.

If Claude Code and Codex CLI are already configured, do not create a second
Kimi entry under another name. Each client may run its own stdio server
process, but all three entries must point to the same memtomem configuration
and store.

## Load the skills

During development, point Kimi at the generated bundle:

```bash
kimi --skills-dir /path/to/memtomem/packages/memtomem-kimi-skills/skills
```

For regular use, copy the individual children of `skills/` into
`~/.kimi-code/skills/`. Start a new Kimi session after changing MCP or skill
configuration.

The bundle contains seven workflows. Search, recall, and status are read-only;
remember, index, setup, and handoff require an explicit request. To verify the
connection, ask Kimi:

```text
Use the memtomem-status skill to inspect the current memory index.
```

## Project handoff

From a Git worktree, initialize the private project tier once:

```bash
cd /path/to/project
uvx --from 'memtomem==0.4.0' mm mem init --scope project_local
```

Then use the handoff skill explicitly:

```text
Use the memtomem-handoff skill to resume the newest handoff for kimi-code.
```

The skill reads only `scope=project_local` from `shared:<project-slug>` and
compares the stored root, commit, and worktree summary with live Git. It never
falls back to personal memory or treats the record as authoritative.

See [Cross-runtime sequential handoff](cross-runtime-handoff.md) for the full
Claude Code → Codex CLI → Kimi Code round trip.
