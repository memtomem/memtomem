# memtomem

> Long-term memory for AI coding agents, kept as Markdown you own. Core usage is hook-free by default: memory changes happen only when you or your agent call memtomem.

[![PyPI](https://img.shields.io/pypi/v/memtomem)](https://pypi.org/project/memtomem/)
[![Downloads](https://img.shields.io/pypi/dm/memtomem)](https://pypi.org/project/memtomem/)
[![GitHub stars](https://img.shields.io/github/stars/memtomem/memtomem)](https://github.com/memtomem/memtomem/stargazers)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

> 🧪 **Beta** — the nine core MCP tools, the `mm` CLI, and the format of your memory and config files change only with an `Upgrading` note in the [CHANGELOG](CHANGELOG.md). Features marked **experimental** can change without one; see [Stability](#stability). Feedback is welcome: [Issues](https://github.com/memtomem/memtomem/issues) · [Discussions](https://github.com/memtomem/memtomem/discussions).

## The session loop

Three plugin workflows carry a project's memory from one session to the next, and from one coding agent to another. Illustrative output — agent wording and results vary.

```text
# End of a session, in Claude Code
> /memtomem:handoff save codex-cli
handoff_id: 3f2a9c1e-7b4d-4e8a-9c2f-1d5e6a7b8c9d
scope: project_local · namespace: shared:retry-policy · 1 file written, 2 chunks indexed

# Next session, in Codex CLI
> $memtomem-brief
Recent updates and active handoff IDs
  - 3f2a9c1e-7b4d-4e8a-9c2f-1d5e6a7b8c9d  from claude-code → codex-cli
    load it with: $memtomem-handoff resume 3f2a9c1e-7b4d-4e8a-9c2f-1d5e6a7b8c9d
> $memtomem-handoff resume 3f2a9c1e-7b4d-4e8a-9c2f-1d5e6a7b8c9d
git_head matches the live HEAD · worktree clean · in sync with `origin` `refs/heads/main`

# After a few commits, back in Claude Code
> /memtomem:update
mode: incremental · 4 of the latest 10 commits not yet reviewed
saved 2 items · commit-derived: 1 · session-derived: 1
```

`handoff save` writes one Markdown record into the project's own `.memtomem/memories.local`; `brief` only reads; `update` saves decisions, bug fixes, and preferences as separate items. All three run only when you invoke them, and nothing runs in the background. The [Project workflows](docs/guides/project-workflows.md) guide has each one's modes, checks, and host-specific names.

## Why memtomem?

- **Your files stay yours.** Memories are plain `.md` files you can read, edit, and version. The search database is a rebuildable cache.
- **Explicit by default.** Memory operations run on your CLI or MCP calls (`mm add`, `mem_add`, `mem_index`). Optional client hooks are separate, removable integrations, not a hidden runtime default.
- **One store, many tools.** Claude Code, Codex CLI, OpenCode, Kimi Code, and Antigravity share one local store, and any MCP client can search it. [memtomem-stm](https://github.com/memtomem/memtomem-stm) is a separate, optional proxy that surfaces memories proactively.

## Quick Start

### 1. Install

```bash
uv tool install 'memtomem[all]'       # or: pipx install 'memtomem[all]'
mm --version                          # verify install
```

`[all]` bundles ONNX dense embeddings, the Korean tokenizer, the code chunker, and the Web UI (about 340 MB installed, before any model download). The Ollama and OpenAI embedding providers need no extra. A smaller install is under [Other install options](#minimal-install) below.

### 2. Setup

```bash
mm init                               # preset picker, then memory_dir + MCP
```

The interactive picker starts with three presets — **Minimal** (BM25, no downloads), **English (Recommended)** (ONNX `multilingual-e5-small` + auto-discover providers), **Korean-optimized** (ONNX `multilingual-e5-small` + `kiwipiepy` tokenizer); no preset turns on a reranker — plus an **Advanced** entry that opens the full 10-step wizard. Preset paths only ask about the memory directory and MCP registration; everything else is set from the preset.

Choose **Minimal** for the fastest no-download first proof and rerun `mm init` later to add semantic search. For automation, `mm init --non-interactive` picks the minimal preset without prompts. See [Embeddings](docs/guides/embeddings.md) for the model and provider matrix.

<a id="3-use"></a>
### 3. Verify a complete memory round trip

```bash
mm status
mm add "Deployment checklist uses blue-green rollout" --tags ops
mm search "blue-green"
```

The last command should return the sentence you just added. `mm add` writes to your user memory directory and indexes the entry immediately, so this works without an existing notes directory or a connected editor. To bring existing notes into the same index, run `mm index /path/to/your/notes`.

### 4. Connect your coding agent

Install the plugin for your tool — [Claude Code](packages/memtomem-claude-plugin/), [Codex CLI](plugins/memtomem/), [OpenCode](packages/opencode-memtomem/), [Kimi Code](packages/memtomem-kimi-skills/), or [Antigravity](packages/memtomem-antigravity-plugin/) — then, in the project you work on, register the memory tier the session loop writes to:

```bash
cd <your-project> && mm mem init --scope project_local
```

Start a new session and run the brief workflow: `/memtomem:brief` in Claude Code, and the name each other host uses is in the [Project workflows](docs/guides/project-workflows.md) guide. The Korean [바이브코딩 빠른 시작](docs/guides/vibe-coding-getting-started-ko.md) walks this path in Claude Code or Codex CLI in 10–15 minutes. For Cursor, Windsurf, Claude Desktop, Gemini CLI, and other MCP clients, see [MCP Client Setup](docs/guides/mcp-clients.md); from any client, asking it to call the `mem_status` tool confirms the connection.

<a id="4-web-ui-optional"></a>
### 5. Open the Web UI (optional)

```bash
mm web                # dashboard on http://127.0.0.1:8080
mm web -b             # run in the background; logs go to ~/.memtomem/logs/web.log
mm web stop           # stop the tracked Web UI process
```

`mm web` shows the polished page set. `mm web --dev` (or `MEMTOMEM_WEB__MODE=dev`) adds maintainer pages such as Sessions, Working Memory, and Health Report.

<details>
<summary><b>Other install options</b></summary>

<a id="minimal-install"></a>
**Minimal** (no bundled model, about 50 MB):
```bash
uv tool install memtomem             # no extras — no local ONNX model, Web UI or Korean tokenizer until you add them; Ollama/OpenAI embeddings still work
```
Opt in later per feature: `uv tool install --reinstall 'memtomem[onnx,web]'` (see the [extras table](docs/guides/getting-started.md#optional-extras)). List every extra you want to keep, because `--reinstall` replaces the whole set. A plain `mm upgrade` keeps the extras it detects from the current install, but an explicit `mm upgrade --extras` list must name the complete set too.

**Project-scoped** (per-project isolation):
```bash
uv add 'memtomem[all]' && uv run mm init    # all commands need `uv run` prefix
```

**No install** (uvx on demand):
```bash
claude mcp add memtomem -s user -- uvx --isolated --from "memtomem[all]==0.7.2" memtomem-server
```

</details>

<details>
<summary><b>Troubleshooting the install</b></summary>

- **`mm --version` shows an older version than the [latest release](https://github.com/memtomem/memtomem/releases)** right after installing: `uv` is likely serving cached PyPI metadata. Re-run with `uv tool install 'memtomem[all]' --refresh`, or clear the cache first with `uv cache clean memtomem`. To upgrade an existing uv tool install, use `mm upgrade` rather than re-running `uv tool install`; see the [CLI reference](docs/guides/reference/data-config-cli.md#cli-reference) for what it preserves and stops.
- **`mm: command not found`**: `uv tool install` drops the shim into `~/.local/bin`, which is not on `$PATH` in fresh shells on macOS/Linux. Run `uv tool update-shell`, then open a new shell and re-run `mm --version`.
- **Duplicate tools after upgrading the Claude plugin**: re-check `/mcp` after reloading. The plugin launches `memtomem[onnx]==0.7.2`. A manual registration whose launch differs — base-only `memtomem`, or the ONNX launch pinned to an earlier release — no longer matches for deduplication. Confirm the manual entry's name and scope, then align its launch command with the plugin or remove the redundant registration.
- **Indexing vs. discovery (Claude Code)**: provider memory folders that setup auto-discovers (such as `~/.claude/projects/*/memory/`) are added to the search *index*. That is separate from the Web UI's opt-in Context Gateway scan of `~/.claude/projects/`, which discovers project *roots* for Skills, Commands, and Subagents; see [Configuration → Context Gateway](docs/guides/configuration.md#context-gateway).

</details>

## What else is inside

| Your memory, your files | Operate it | Plug it in |
|---|---|---|
| Markdown is the source of truth; the search database is a rebuildable cache | [Web UI](docs/guides/reference/operations.md) for search, sources, tags, timeline, and dedup (`mm web`) | Nine core MCP tools, with `mem_do` routing every other action so agents load a small tool surface |
| Hybrid search: BM25 keyword + dense vector + RRF fusion in one query | [Namespaces](docs/guides/reference/organization-maintenance.md), auto-derived from folder names and labelled from the Web UI | [Context Gateway](docs/guides/context-gateway.md): keep Skills, Commands, and Subagents in one Store and push them to your AI runtimes |
| Semantic chunking: heading-aware Markdown, AST-based Python, tree-sitter JS/TS, structure-aware JSON/YAML/TOML | [Maintenance](docs/guides/reference/organization-maintenance.md): near-duplicate detection, time-based decay, TTL expiration, auto-tagging | [Pinned Context](docs/guides/pinned-context.md): small user/project/agent Markdown blocks composed ahead of retrieved results |
| Incremental indexing: chunk-level SHA-256 diff, so only changed chunks are re-embedded | [Scheduled jobs](docs/guides/reference/automation.md) for compaction, decay, dead-link cleanup, and dedup scans | LangGraph `MemtomemBaseStore`, plus the [Hybrid Store](docs/guides/langgraph-hybrid-store.md) with SQLite persistence and BM25/dense/RRF retrieval |
| | [Scriptable CLI](docs/guides/reference/data-config-cli.md#cli-reference): `--json` output on status, search, recall, and the write commands; `mm warmup` pre-loads local models | [memtomem-stm](https://github.com/memtomem/memtomem-stm): optional proxy for proactive surfacing, compression, and caching |

## Stability

memtomem is beta (`0.x`). The nine core MCP tools, the `mm` CLI, and the format of your memory files and config files change only with an `Upgrading` note in the [CHANGELOG](CHANGELOG.md), and behind a deprecation window where one is possible. Other features are supported — maintained, with their changes recorded in the CHANGELOG — without that guarantee.

These are **experimental** — their behaviour, output, or format can still change or go away without a window, because a decision that shapes them is still open or an LLM writes what they produce:

- **Multi-agent memory** — `mem_agent_register`, `mem_agent_search`, `mem_agent_share`, and per-agent session scopes (`mem_session_start` with `agent_id`). Open decisions: [#1477](https://github.com/memtomem/memtomem/issues/1477), [#1478](https://github.com/memtomem/memtomem/issues/1478).
- **Memory export and import bundles** — `mem_export` / `mem_import` and the Web export/import. How a backup made before 0.7.0 (bundle v1–v3) that holds project memories should be restored is still open.
- **LLM-written output** — anything memtomem's configured LLM (`MEMTOMEM_LLM__ENABLED=true`) writes: LLM auto-tagging and entity extraction, `strategy="llm"` query expansion, LLM consolidation and policy summaries, session auto-summaries, source summaries (`indexing.auto_summarize`), and contextual chunk enrichment (`indexing.enrich_chunk_context`). The rule-based modes beside them are supported.
- **Claude projects scan** in the Context Gateway (`MEMTOMEM_CONTEXT_GATEWAY__EXPERIMENTAL_CLAUDE_PROJECTS_SCAN`).
- **Antigravity CLI plugin** ([`packages/memtomem-antigravity-plugin/`](packages/memtomem-antigravity-plugin/)).

## Works with

| Package | What it adds | Session loop |
|---------|--------------|--------------|
| [**memtomem**](https://pypi.org/project/memtomem/) | Core: MCP server, CLI, Web UI, hybrid search, storage | — |
| [**Claude Code plugin**](packages/memtomem-claude-plugin/) | Exact-pinned MCP server and slash-command workflows | handoff · brief · update |
| [**Claude Code automation plugin**](packages/memtomem-claude-automation-plugin/) | Optional hooks for prompt-time search and write-time indexing | — |
| [**Codex plugin**](plugins/memtomem/) | Exact-pinned MCP server and skills for Codex CLI | handoff · brief · update |
| [**opencode-memtomem**](packages/opencode-memtomem/) | Exact-pinned MCP server, commands, and read skills for OpenCode | handoff · brief · update |
| [**Kimi Code skills**](packages/memtomem-kimi-skills/) | Skill bundle for Kimi Code (not a plugin) | handoff · brief · update |
| [**Antigravity CLI plugin**](packages/memtomem-antigravity-plugin/) | Exact-pinned MCP server and skills for `agy` — [experimental](#stability) | handoff · brief · update |
| [**Hermes Agent plugin**](packages/memtomem-hermes-plugin/) | Exact-pinned MCP server and the search, recall, status, remember, index, and setup skills | — (needs a project root) |
| [**Hermes memory provider**](packages/memtomem-hermes-memory/) | Recall-only provider that adds relevant notes to each Hermes turn | — |
| [**memtomem-stm**](https://github.com/memtomem/memtomem-stm) | STM proxy: proactive memory surfacing via tool interception | — |

## Documentation

Hosted at **[memtomem.com](https://memtomem.com)** and available as Markdown in this repo.

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/guides/getting-started.md) | Install, configure, save and find your first memory |
| [한국어 바이브코딩 빠른 시작](docs/guides/vibe-coding-getting-started-ko.md) | Claude Code·Codex CLI에서 10~15분 안에 기억 저장·검색 |
| [Project workflows](docs/guides/project-workflows.md) | handoff, brief, and update: modes, checks, and names per host |
| [MCP Client Setup](docs/guides/mcp-clients.md) | Editor-specific configuration |
| [Use cases](docs/guides/use-cases.md) | Nine runnable scenarios for multi-tool developers, personal knowledge bases, and teams |
| [All guides](docs/guides/README.md) | Reference, configuration, embeddings, operations, and more, in a suggested reading order |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and the contributor guide.

## License

[Apache License 2.0](LICENSE). Contributions are accepted under the terms of the [Contributor License Agreement](CLA.md).
