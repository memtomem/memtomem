## Registration check (Claude Code)

Before reporting results, note which memtomem tool namespaces this session
exposes. If both `mcp__memtomem__mem_*` and `mcp__plugin_memtomem_memtomem__mem_*`
tools are available, warn about possible duplicate registrations. Other aliases
exposing the same memtomem tool family also warrant a registration check.
Tool visibility alone does not prove two live processes or a shared database.
Recommend `/mcp` first to determine registration names and scopes. A single
visible namespace does not prove that no other registration exists.

Published PyPI 0.5.0 does not include the `--claude-mcp` diagnostic option, and
installing the plugin does not put `mm` on the user's PATH. Only if the user has
a source checkout containing the diagnostic, offer this optional command:

```bash
uv run --project /path/to/memtomem --package memtomem mm doctor --claude-mcp
```

Replace `/path/to/memtomem` with that checkout and run from the project being
inspected. `--project` preserves that current directory. Without such a checkout,
use `/mcp` and the scope-specific guidance below.

Name both remediations: keep the plugin by removing the confirmed manual entry
(`claude mcp remove memtomem -s user` for user scope; use `-s local` or
`-s project` for those scopes and replace the name for an alias), or keep the
manual entry by running `/plugin uninstall memtomem@memtomem`. Never remove
either registration yourself. Do not claim to have run the diagnostic unless
it was actually executed. If no duplication is visible, skip the warning silently.
