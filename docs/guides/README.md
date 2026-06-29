# memtomem guides

Every guide stands on its own, but if you're picking up memtomem for the first
time this is the suggested reading order — get set up, tune to taste, then reach
for the reference as you need it.

## Set up

1. **[Getting Started](getting-started.md)** — install, the setup wizard, and
   your first memories. A working setup in under five minutes.
2. **[MCP Client Setup](mcp-clients.md)** — connect any MCP editor — Claude Code,
   Cursor, Codex CLI, Antigravity, Windsurf, Claude Desktop, Gemini / Kimi CLI.
   New here? Claude Code is the one-command setup. Per-editor deep dives:
   - [Claude Code](integrations/claude-code.md)
   - [Cursor](integrations/cursor.md)
   - [Claude Desktop](integrations/claude-desktop.md)

## Tune

3. **[Configuration](configuration.md)** — every `MEMTOMEM_*` setting and how the
   config layers (`config.json`, `config.d/` fragments, environment variables)
   merge.
4. **[Embeddings](embeddings.md)** — embedding providers and the model matrix:
   BM25-only (the default), ONNX, Ollama, and OpenAI.
5. **[LLM Providers](llm-providers.md)** — enable the LLM-powered features (query
   expansion, fact extraction): Ollama, OpenAI, Anthropic, and compatible
   endpoints.

## Power features

6. **[Context Gateway](context-gateway.md)** — keep one Store of Skills,
   Commands, and Subagents and sync it to every AI tool you use.
7. **[Multi-device sync](multi-device-sync.md)** — sync your markdown memories
   across personal devices via a private git repo.

## Reference & lifecycle

8. **[Reference](reference.md)** — the complete reference for every action, tool,
   and pattern. Reach for it once you're set up.
9. **[Uninstalling memtomem](uninstall.md)** — clean removal, with `--keep-data`
   to preserve your memories.
