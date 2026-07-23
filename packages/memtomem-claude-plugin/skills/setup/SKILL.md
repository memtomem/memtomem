---
name: setup
description: Set up and verify a first memtomem memory source. Use for onboarding, choosing an index path, or confirming that search works.
argument-hint: [path]
allowed-tools: mcp__plugin_memtomem_memtomem__mem_status, mcp__memtomem__mem_status, mcp__plugin_memtomem_memtomem__mem_index, mcp__memtomem__mem_index, mcp__plugin_memtomem_memtomem__mem_search, mcp__memtomem__mem_search
disable-model-invocation: true
---

# Set up memtomem

Use `$ARGUMENTS` as the memory source path.
If the request does not clearly specify the memory source path, ask before calling a tool.
1. Call `mem_status` and treat the default `provider=none` BM25-only configuration as healthy.
2. If status says memtomem is not configured, stop before indexing and give the exact terminal bootstrap command from the plugin README. Preserve project context by prefixing it with `cd <project-root> &&` when the setup is project-specific. Retry only after the user completes that explicit trust step.
3. Obtain an explicit notes or memory directory from the request; ask for one when absent.
4. Call `mem_index` on that path with `force=false` and `auto_tag=false`. This is a one-shot index and must not silently register a watcher root.
5. Choose a representative phrase from the indexed material and call `mem_search` to verify retrieval.
6. Report the effective DB path, indexed path, and first-success result. Mention embeddings only as an optional relevance enhancement.

Do not install Ollama, enable automation hooks, or edit host instruction files unless the user separately requests those actions.

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
