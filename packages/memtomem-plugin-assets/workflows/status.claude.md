## Registration check (Claude Code)

If both `mcp__memtomem__mem_*` and `mcp__plugin_memtomem_memtomem__mem_*`
tools are visible, warn about possible duplicate registrations. Other aliases
exposing the same memtomem tool family also warrant a check. Tool visibility
alone does not prove two live processes or a shared database; a single visible
namespace does not prove that no other registration exists.

Recommend `/mcp` first. The `--claude-mcp` diagnostic ships in the released
CLI, but installing the plugin does not put `mm` on the user's PATH — it
registers an MCP server, not the CLI. So offer the pinned form, which works
either way:

```bash
uvx --from "memtomem[all]==0.6.1" mm doctor --claude-mcp
```

If the user says they already have the CLI installed, `mm doctor --claude-mcp`
is the same check. Run it from the project being inspected. Neither is required
— `/mcp` and the scope-specific guidance below cover the same ground.

Use the confirmed registration name and scope for any removal advice:
`claude mcp remove memtomem -s user` is only for that user-scope name. To keep
the manual setup instead, uninstall `/plugin uninstall memtomem@memtomem`.
Never remove either registration yourself. Do not claim to have run the
diagnostic unless it was actually executed. With no visible duplication,
report the memory status normally without an extra warning.
