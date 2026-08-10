# memtomem for Kimi Code

This is a portable Kimi Code skill bundle, not a plugin. It contains the
same seven workflow contracts shipped for Claude Code and Codex CLI; Kimi
loads them from its Agent Skills directory while MCP registration remains a
separate, explicit step.

## Use from this repository

Register the MCP server once by creating `~/.kimi-code/mcp.json` as shown in
the [Kimi Code integration guide](../../docs/guides/integrations/kimi-code.md).
(`memtomem==0.4.0` and later write that path, so `mm init --mcp kimi` also
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
