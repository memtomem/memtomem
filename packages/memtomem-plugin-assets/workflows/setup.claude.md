## Registration check (Claude Code)

Before reporting results, note which memtomem tool namespaces this session
exposes. If both `mcp__memtomem__mem_*` and `mcp__plugin_memtomem_memtomem__mem_*`
tools are available, two memtomem servers are running against the same store —
a manual `claude mcp add` entry plus the plugin's pinned server. Tell the user,
and name both remediations: keep the plugin by removing the manual entry
(`claude mcp remove memtomem`, adding `-s user` for a user-scope entry), or keep
the manual entry by running `/plugin uninstall memtomem@memtomem`. Never remove
either registration yourself. If only one namespace is present, skip this check
silently.
