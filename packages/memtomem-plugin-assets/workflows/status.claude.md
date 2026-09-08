## Registration check (Claude Code)

If both `mcp__memtomem__mem_*` and `mcp__plugin_memtomem_memtomem__mem_*`
tools are visible, warn about possible duplicate registrations. Other aliases
exposing the same memtomem tool family also warrant a check. Tool visibility
alone does not prove two live processes or a shared database; a single visible
namespace does not prove that no other registration exists.

Recommend `/mcp` and `mm doctor --claude-mcp` from the project's root directory.
Use the diagnostic's exact registration name and scope for any removal advice:
`claude mcp remove memtomem -s user` is only for that user-scope name. To keep
the manual setup instead, uninstall `/plugin uninstall memtomem@memtomem`.
Never remove either registration yourself. Do not claim to have run the
diagnostic unless it was actually executed. With no visible duplication,
report the memory status normally without an extra warning.
