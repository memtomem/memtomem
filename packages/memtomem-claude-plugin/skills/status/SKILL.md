---
name: status
description: Check memtomem configuration, storage, index counts, dense coverage, and warnings. Use when search is empty, degraded, or needs diagnosis.
allowed-tools: mcp__plugin_memtomem_memtomem__mem_status, mcp__memtomem__mem_status
---

# Check memory status
Call `mem_status` once. Report the storage backend and database path, embedding state, source and chunk counts, dense-vector coverage, and warnings that the tool actually returns.

Treat `provider=none` and BM25-only coverage as a supported default, not a failed setup. Do not claim namespace totals or other fields absent from the response.

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
