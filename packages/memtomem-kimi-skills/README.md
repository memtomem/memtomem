# memtomem for Kimi Code

This is a portable Kimi Code skill bundle, not a plugin. It carries the same
workflow contracts shipped for Claude Code and Codex CLI, including the
session loop — `memtomem-handoff` saves a checkpoint when a session ends,
`memtomem-brief` reads it back when the next one starts, and `memtomem-update`
turns finished work and recent commits into durable memories (see
[Project workflows](../../docs/guides/project-workflows.md)). Kimi loads the
skills from its Agent Skills directory while MCP registration remains a
separate, explicit step.

## Use from this repository

Register the MCP server once by creating `~/.kimi-code/mcp.json` as shown in
the [Kimi Code integration guide](../../docs/guides/integrations/kimi-code.md).
(memtomem `0.4.0` and later write that path, so `mm init --mcp kimi` also
works; releases up to `0.3.14` wrote the legacy `~/.kimi/mcp.json` layout,
which current Kimi Code does not read.)

Then either copy the generated `skills/` children to `~/.kimi-code/skills/`, or
start Kimi against the bundle directly:

```bash
kimi --skills-dir /path/to/memtomem/packages/memtomem-kimi-skills/skills
```

Start a new Kimi session and invoke `memtomem-status` or the explicit
`memtomem-handoff` skill. The bundle does not add slash commands, hooks,
background capture, or a second MCP server.

For a complete same-Mac Claude Code → Codex CLI → Kimi Code handoff, follow
the [cross-runtime handoff guide](../../docs/guides/integrations/cross-runtime-handoff.md).
