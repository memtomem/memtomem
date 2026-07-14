# memtomem guides

Start with one successful memory round trip, then pick the task you need. The
guides are organized by outcome rather than by feature name.

<a id="set-up"></a>
## Start here

1. **[Getting Started](getting-started.md)** — install memtomem, run setup,
   save one memory, and find it again in under five minutes.
   - **Korean plugin-first path:** [한국어 바이브코딩 빠른 시작](vibe-coding-getting-started-ko.md)
     — Claude Code 또는 Codex CLI에서 10~15분 안에 기억을 저장·검색합니다.
2. **[MCP Client Setup](mcp-clients.md)** — connect Claude Code, Cursor,
   Codex CLI, Antigravity, Windsurf, Claude Desktop, Gemini CLI, or Kimi CLI.
   Client-specific deep dives:
   - [Claude Code](integrations/claude-code.md)
   - [Codex](integrations/codex.md)
   - [Cursor](integrations/cursor.md)
   - [Claude Desktop](integrations/claude-desktop.md)
3. **[Use cases](use-cases.md)** — nine runnable scenarios for multi-tool
   developers, individual knowledge bases, and teams.

## Work with memories

- **Save and search** — [Core memory tools](reference/core-memory-tools.md)
  covers `mm add`, `mm index`, `mm search`, `mem_add`, and `mem_search`.
- **Bring in existing tool memory** — [Data, config & CLI](reference/data-config-cli.md)
  covers `mm ingest` for Claude, Codex, Gemini/Antigravity, Obsidian, and
  Notion sources.
- **Organize and maintain** — [Organization & maintenance](reference/organization-maintenance.md)
  covers namespaces, deduplication, decay, tagging, and memory health.
- **Automate upkeep** — [Automation](reference/automation.md) covers policies
  and scheduled jobs.
- **Pin and review context** — [Pinned Context](pinned-context.md) covers
  pinned-first composition, the approval queue, and LangGraph `BaseStore`.

<a id="tune"></a>
## Improve search quality

- **[Embeddings](embeddings.md)** — choose BM25-only, ONNX, Ollama, or OpenAI
  and switch models safely.
- **[LLM Providers](llm-providers.md)** — enable optional query expansion,
  extraction, auto-tagging, and summaries.
- **[Configuration](configuration.md)** — understand config files,
  precedence, environment variables, and advanced tuning.

<a id="power-features"></a>
## Share and sync

- **[Context Gateway](context-gateway.md)** — keep one Store of Skills,
  Commands, and Subagents and sync it to supported AI runtimes.
- **[Multi-device sync](multi-device-sync.md)** — sync markdown memories
  across your own machines with a private git repository.

## Operate and recover

- **[Operations & troubleshooting](reference/operations.md)** — run the Web
  UI, diagnose indexing/search issues, audit historical content, and recover
  safely.
- **[Uninstalling memtomem](uninstall.md)** — remove the runtime while keeping
  or deleting stored data explicitly.

<a id="reference--lifecycle"></a>
## Reference

- **[Reference index](reference.md)** — concepts, tool modes, MCP tools, and
  links to every topic reference.
