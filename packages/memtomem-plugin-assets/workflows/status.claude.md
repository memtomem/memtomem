## Registration check (Claude Code)

If both `mcp__memtomem__mem_*` and `mcp__plugin_memtomem_memtomem__mem_*`
tools are visible, warn about possible duplicate registrations. Other aliases
exposing the same memtomem tool family also warrant a check. Tool visibility
alone does not prove two live processes or a shared database; a single visible
namespace does not prove that no other registration exists.

Recommend `/mcp` first. Published PyPI 0.5.0 does not include the `--claude-mcp`
diagnostic option, and installing the plugin does not put `mm` on the user's PATH.
Only if the user has a source checkout containing the diagnostic, offer this
optional command:

```bash
uv run --project /path/to/memtomem --package memtomem mm doctor --claude-mcp
```

Replace `/path/to/memtomem` with that checkout and run from the project being
inspected. `--project` preserves that current directory. Without such a checkout,
use `/mcp` and the scope-specific guidance below.

Use the confirmed registration name and scope for any removal advice:
`claude mcp remove memtomem -s user` is only for that user-scope name. To keep
the manual setup instead, uninstall `/plugin uninstall memtomem@memtomem`.
Never remove either registration yourself. Do not claim to have run the
diagnostic unless it was actually executed. With no visible duplication,
report the memory status normally without an extra warning.
