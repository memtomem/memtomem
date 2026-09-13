## Registration check (Claude Code)

Before reporting results, note which memtomem tool namespaces this session
exposes. If both `mcp__memtomem__mem_*` and `mcp__plugin_memtomem_memtomem__mem_*`
tools are available, warn about possible duplicate registrations. Other aliases
exposing the same memtomem tool family also warrant a registration check.
Tool visibility alone does not prove two live processes or a shared database.
Recommend `/mcp` first to determine registration names and scopes. A single
visible namespace does not prove that no other registration exists.

The `--claude-mcp` diagnostic ships in the released CLI, but installing the
plugin does not put `mm` on the user's PATH — it registers an MCP server, not
the CLI. So offer the pinned form, which works either way:

```bash
uvx --from "memtomem[all]==0.6.1" mm doctor --claude-mcp
```

If the user says they already have the CLI installed, `mm doctor --claude-mcp`
is the same check. Run it from the project being inspected. Neither is required
— `/mcp` and the scope-specific guidance below cover the same ground.

Name both remediations: keep the plugin by removing the confirmed manual entry
(`claude mcp remove memtomem -s user` for user scope; use `-s local` or
`-s project` for those scopes and replace the name for an alias), or keep the
manual entry by running `/plugin uninstall memtomem@memtomem`. Never remove
either registration yourself. Do not claim to have run the diagnostic unless
it was actually executed. If no duplication is visible, skip the warning silently.
