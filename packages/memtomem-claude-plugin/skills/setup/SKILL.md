---
name: setup
description: Set up and verify a first memtomem memory source. Use for onboarding, choosing an index path, or confirming that search works.
argument-hint: [path]
allowed-tools: mcp__plugin_memtomem_memtomem__mem_status, mcp__memtomem__mem_status, mcp__plugin_memtomem_memtomem__mem_index, mcp__memtomem__mem_index, mcp__plugin_memtomem_memtomem__mem_search, mcp__memtomem__mem_search
disable-model-invocation: true
---

# Set up memtomem

Use `$ARGUMENTS` as the memory source path.
If the request does not clearly specify the memory source path, ask before calling a tool — and
in a non-interactive context (a subagent or scripted run with nobody to ask), do not
stall and do not guess: stop and report `insufficient_input` naming the missing memory source path.
A request that does specify the memory source path proceeds normally in either context.
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
tools are available, warn about possible duplicate registrations. Other aliases
exposing the same memtomem tool family also warrant a registration check.
Tool visibility alone does not prove two live processes or a shared database.
Recommend `/mcp` first to determine registration names and scopes. A single
visible namespace does not prove that no other registration exists.

The `--claude-mcp` diagnostic ships in the released CLI, but installing the
plugin does not put `mm` on the user's PATH — it registers an MCP server, not
the CLI. So offer the pinned form, which works either way:

```bash
uvx --from "memtomem[all]==0.6.0" mm doctor --claude-mcp
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
