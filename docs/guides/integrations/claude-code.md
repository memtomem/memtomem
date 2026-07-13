# Claude Code x memtomem Integration Guide

**Audience**: Developers using Claude Code who want to build memory automation with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Claude Code installed
**Estimated Time**: About 15 minutes

**On this page**

- [Overview](#overview)
- [MCP Server Setup](#mcp-server-setup)
- [Verify Connection](#verify-connection)
- [First Indexing](#first-indexing)
- [Hooks Automation Setup](#hooks-automation-setup)
- [Tool Usage Guidelines (Add to CLAUDE.md)](#tool-usage-guidelines-add-to-claudemd)
- [Usage Scenarios](#usage-scenarios)
- [Built-in Memory vs memtomem Comparison](#built-in-memory-vs-memtomem-comparison)
- [Frequently Asked Questions](#frequently-asked-questions)
- [Cross-runtime agent context with mm context](#cross-runtime-agent-context-with-mm-context)
- [Next Steps](#next-steps)

---

## Overview

Claude Code has its own memory system including CLAUDE.md, MEMORY.md, topic files, and hooks.
memtomem **does not replace** these, but complements Claude Code with **semantic search** it lacks.
The most powerful automation pipeline is achieved when combined with Claude Code's hooks system.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| Project instructions (full loading) | CLAUDE.md |
| Auto-memory index (200-line limit) | MEMORY.md + auto-memory |
| Per-topic file on-demand reading | Claude Code built-in |
| Project document **semantic search** | memtomem (`mem_search`) |
| Hooks-based automation pipeline | memtomem CLI + hooks |

---

## MCP Server Setup

### Option A: Install the safe base plugin

The memtomem plugin bundles the exact-pinned MCP server and six focused slash
commands. Read workflows can be selected automatically; write and setup
workflows require direct invocation.

```
/plugin marketplace add memtomem/memtomem
/plugin install memtomem@memtomem
```

The plugin launches the server via an exact reviewed `uvx --from
memtomem==<version>` pin, so [uv](https://docs.astral.sh/uv/) must be on your
PATH. BM25 works with the default `embedding.provider=none`; embeddings are
optional.

> **Already registered via `claude mcp add`?** Nothing runs twice —
> Claude Code detects that the plugin bundles the same server command
> and suppresses the plugin-managed copy, so your manual registration
> keeps winning and tools keep their `mcp__memtomem__mem_*` names.
> To switch to the plugin-managed server (tools become
> `mcp__plugin_memtomem_memtomem__mem_*`), remove the manual entry:
>
> ```bash
> claude mcp remove memtomem
> ```
>
> Either way the bundled slash commands work — their allowlists cover both
> tool namespaces. If your own settings
> allowlist `mcp__memtomem__mem_*`, update it when you remove the
> manual entry.

Prefer manual registration without shipped skills? Use
Option B below.

### Option B: Register the MCP server manually

#### Pick an installation scope

Claude Code offers three MCP configuration scopes. Pick the one that
matches how you want to share this server:

| Scope | Storage | Shared with | When to use |
|-------|---------|-------------|-------------|
| `local` (default) | `~/.claude.json` → `projects."<cwd>".mcpServers` | Only this project × this user | Personal setup — private paths/tokens, or testing before committing |
| `project` | `<project-root>/.mcp.json` (committed to git) | Everyone who clones the repo | Team-wide shared server |
| `user` | `~/.claude.json` → top-level `mcpServers` | This user across every project | General-purpose server not tied to one project |

**Precedence** when the same server name exists in multiple scopes:
`local` > `project` > `user` > plugins > Claude.ai connectors. Adding a
`local` entry lets you override a shared `project` server with personal
credentials without editing the committed file.

**Trust prompt**: `project` servers from `.mcp.json` require
workspace-trust approval on first use — cloning an unknown repo never
silently spawns an MCP server.

#### Add via command (`local` / `user`)

```bash
# User scope — install once, available in every project
claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server

# Local scope — this project only, not committed (omitting -s is the same)
claude mcp add memtomem -s local -- uvx --from memtomem memtomem-server

# Source install (running from a git clone)
# claude mcp add memtomem -s user -- uv run --directory /path/to/memtomem memtomem-server
```

Both `-s local` and `-s user` write to `~/.claude.json` — no need to edit
that file by hand.

#### Project scope via `.mcp.json`

For a team-shared setup, commit a `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"]
    }
  }
}
```

Teammates see this server after approving the workspace-trust prompt on
first use. To run against personal credentials without touching the
shared file, add a `-s local` entry with the same name — local wins.

The server reads `~/.memtomem/config.json`. Add a client `env` block only for
an intentional highest-precedence override; see the
[MCP client configuration guide](../mcp-clients.md#10-environment-variable-overrides).

---

## Verify Connection

In Claude Code (or run `/memtomem:status` if using the plugin):

```
Call the mem_status tool
```

Example of a successful response (Ollama config; first 13 lines of the
full report — the `Embedding` and `Dimension` rows change with the
provider picked in the wizard):

```
memtomem Status
==============
Storage:   sqlite
DB path:   ~/.memtomem/memtomem.db
Embedding: ollama / nomic-embed-text
Dimension: 768
Top-K:     10
RRF k:     60

Index stats
-----------
Total chunks:  0
Source files:  0
...
```

Or skip the editor and run the same check directly:

```bash
mm status
```

`mm status` is a CLI mirror of `mem_status` (same output) — handy when
the editor hasn't reconnected yet, or for scripted health checks.

> **MCP Reconnection**: After changing `.mcp.json`, restart Claude Code or use the `/mcp` command to reconnect.

---

## First Indexing

```
Index my ~/notes directory
```

Agent:
```
mem_index(path="~/notes", recursive=True)
→ Indexing complete:
  - Files scanned: 47
  - Total chunks: 1284
  - Indexed: 1284
  - Skipped (unchanged): 0
  - Deleted (stale): 0
  - Duration: 3200ms
```

---

## Hooks Automation Setup

Automation is intentionally a second plugin because it reads every submitted
prompt and indexes files after successful Write/Edit tool calls. Install it
only when those side effects are wanted:

```text
/plugin install memtomem-automation@memtomem
```

Install the exact CLI version expected by the automation bundle:

```bash
uv tool install 'memtomem==0.3.8'
```

The bundled dispatcher reads Claude's hook JSON from stdin; it never expands
prompt or tool fields as shell variables. It validates the `mm` version at
session start and fails open when input or dependencies are invalid. Logs live
under `${CLAUDE_PLUGIN_DATA}/hook.log` and contain command status, not prompt
content. The dispatcher runs through `uv`, including on Windows where a
`python3` alias may not exist.

<details>
<summary>Which settings file gets written? (hook tiers)</summary>

> **Tier** (ADR-0010 §3; ADR-0016 §2 settings special-case): for
> settings, the `hooks.target_scope` tier selects the **runtime fan-out
> target** under `~/.claude/` or `<project>/.claude/`, not a canonical
> residency — settings have one canonical file at
> `<project>/.memtomem/settings.json` regardless of tier. The three
> values: `user` (default) → `~/.claude/settings.json`; `project_shared`
> → `<project>/.claude/settings.json` (committed); `project_local` →
> `<project>/.claude/settings.local.json` (gitignored). CLI
> per-invocation override: `mm context sync --include=settings
> --scope=project_local`. In the Web Context Gateway, the Hooks panel
> follows the selected tier (`target_scope`) just like Skills and
> Subagents.

</details>

### Hook Event Summary

| Hook Event | Trigger Timing | memtomem Action |
|------------|---------------|----------------|
| `SessionStart` | When a Claude Code session starts | Validate the exact `mm` dependency; do not create an episodic session |
| `UserPromptSubmit` | Before a prompt is processed | Search prompts longer than 20 characters and return up to three results as `additionalContext` |
| `PostToolUse` (`Write|Edit`) | After a Write/Edit tool call | Validate the path and queue supported source files with a 5-second debounce window |
| `Stop` | After each completed response | Flush the index queue; never close the memtomem session |

### Important Caveats

- **Short prompt guard**: Prompts of 20 characters or fewer are skipped to avoid noise from "yes", "ok", etc.
- **Input safety**: JSON is parsed without invoking a shell and queries are capped at 500 characters.
- **Error logging**: diagnostics are isolated in `${CLAUDE_PLUGIN_DATA}/hook.log`.
- **No session lifecycle automation**: `Stop` fires after every response, not when the Claude session exits. Session start/end tracking remains manual.
- **Allowlist + blocklist**: Write and Edit hooks accept supported source extensions case-insensitively and skip build, cache, virtualenv, dependency, and VCS directories.
- **Debounce mechanics**: changed paths enter the memtomem queue and `Stop` performs the final synchronous flush.
- **STM proxy overlap**: If using [memtomem-stm](https://github.com/memtomem/memtomem-stm) (separate package), hooks are redundant — the proxy already handles surfacing and indexing.

### Detecting duplicate hooks across tiers

Claude Code 2.x merges hook entries from all three settings tiers
(`~/.claude/settings.json`, `<project>/.claude/settings.json`,
`<project>/.claude/settings.local.json`) additively, so a memtomem-managed
hook duplicated across tiers fires once per tier — silent double-execution.

`mm context sync --include=settings` and the Web UI hooks panel scan the
non-active tiers before write and surface a non-blocking warning when
duplicates exist. For CI / scripting use, run the on-demand check:

```bash
mm context settings-doctor               # exits 0 if clean, 1 if duplicates
mm context settings-doctor --json        # structured output for scripting
mm context settings-doctor --scope=project_local   # one-shot scope override
```

<details>
<summary>Fixing duplicates — migrate and copy hooks across tiers and projects</summary>

The match is by canonical signature (event + matcher + command shape, with
whitespace normalized) so a hand-edited variant of a memtomem-managed entry
still classifies. Detection is non-mutating; the action surface is the
companion `mm context settings-migrate` subcommand:

```bash
mm context settings-migrate --from=user --to=project_local           # dry-run
mm context settings-migrate --from=user --to=project_local --apply   # mutate
mm context settings-migrate --from=user --to=project_local --json    # CI / scripting
```

Default is a dry-run preview; pass `--apply` to mutate disk. The migrator
takes the canonical rule from `.memtomem/settings.json` (so the target
ends up byte-clean rather than carrying the source-side whitespace
variant) and removes the matched inner-hook entries from the source tier.
Idempotent — re-running after a clean migration finds nothing to move.

When the source or target lives outside the project root (e.g.
`--from=user`, which resolves to `~/.claude/settings.json`), `--apply`
prompts for confirmation; pass `--yes` to skip the prompt in scripts.

`settings-migrate` moves entries between tiers of ONE project. To propagate
a single hook to ANOTHER project ("I want this guard hook in project B
too"), use the cross-project sibling:

```bash
mm context settings-copy --event PostToolUse --matcher "Edit|Write" \
    --to-project ~/work/project-b                                     # dry-run
mm context settings-copy --event PostToolUse --matcher "Edit|Write" \
    --to-project ~/work/project-b --apply --confirm-project-shared    # mutate
```

`--to-project` takes a `p-<sha12>` scope_id from `mm context projects list`
or a filesystem path. The copy writes the destination's canonical
`.memtomem/settings.json` (so the destination's own syncs keep the rule
alive) plus the destination-tier Claude settings file (`--to <tier>`,
defaulting to the resolved `hooks.target_scope`); Codex/Gemini/Kimi pick
the entry up on the destination's next
`mm context sync --include=settings` — the exact command is printed.
Because the destination canonical is git-tracked, `--apply` requires
`--confirm-project-shared` whenever something would actually be written,
and the privacy scan runs for every destination tier with no force valve.
Re-runs are idempotent no-ops; a same-matcher rule with different content
at the destination is skipped with the colliding entry named (never
duplicated). When several entries share `(event, matcher)`, disambiguate
with `--hook-command <substring>`.

</details>

---

## Tool Usage Guidelines (Add to CLAUDE.md)

Adding the following to your project's `CLAUDE.md` helps Claude Code properly utilize memtomem tools:

```markdown
## Memory Tool Usage Guidelines

### Claude Code Built-in Memory (CLAUDE.md, MEMORY.md)
- Project rules, coding conventions, configuration guidelines
- Current conversation context, task progress
- Simple facts already in MEMORY.md

### memtomem (`mem_search`, `mem_index`, `mem_recall`)
- Project documents, ADRs, architecture document search
- Code pattern history, debugging records, decision records
- Detailed information beyond MEMORY.md's 200-line limit

### Principles
- Rules stated in CLAUDE.md → Follow as-is
- Something to find in project documents/history → `mem_search`
- Check recent work records → `mem_recall`

### Dual Memory Search (MEMORY.md + mem_search)
When users ask about past records or decisions ("previously", "what was decided", "what was it" etc.):
1. Check MEMORY.md (auto-loaded 200 lines) first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Optional Automation Pipeline

With `memtomem-automation` installed, retrieval and indexing run automatically.

```
User: "Refactor the auth middleware"

1. UserPromptSubmit hook auto-executes
   → mem_search("auth middleware refactoring") → 3 related previous decisions injected
2. Claude analyzes existing code and performs refactoring
3. Claude writes or edits source files → PostToolUse queues them for indexing
4. Claude saves key decisions via mem_add (agent-driven, not automated)
```

### Scenario B: Dual Memory Search -- Simultaneous MEMORY.md + mem_search

Claude Code's auto-memory only auto-loads the first 200 lines of MEMORY.md.
As a project grows, important information gets truncated.
**Adding the dual memory search principle to CLAUDE.md** ensures both sources are automatically checked.

Content to add to CLAUDE.md:
```markdown
## Memory Search Principle
When users ask about past records or decisions ("previously", "what was decided", "what was it" etc.):
1. Check MEMORY.md (auto-loaded 200 lines) first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

Conversation example:
```
User: "What was the caching strategy we decided on before?"

Agent:
1. Check MEMORY.md → Found some cache-related items ("Using Redis LRU")
2. mem_search("caching strategy decision") → Returns detailed decision record
3. Synthesize both results:
→ "MEMORY.md has 'Using Redis LRU' recorded, and according to the detailed record,
   the decision was for Redis Cluster + Local LRU 2-tier cache (2026-03-10)"
```

> **Key Point**: CLAUDE.md is auto-loaded at the start of every conversation, so stating the principle here
> enables dual search in all conversations without separate hooks.

### Scenario C: Project Document Indexing + Auto-Reference During Code Writing

```
User: "Implement endpoints based on the API design document"

Agent:
1. mem_search("API design endpoint spec") → Returns chunks from docs/api-spec.md
2. Generate code matching the spec
3. PostToolUse hook → Auto-indexes the generated file
→ "Previously created API" searchable in the next session
```

---

## Built-in Memory vs memtomem Comparison

| Feature | Claude Code Built-in | memtomem |
|---------|---------------------|---------|
| Semantic search | None (full loading or filename-based) | Hybrid search: keyword (BM25) + semantic (vector), fused with RRF |
| Auto memory | MEMORY.md 200-line limit | Unlimited semantic search |
| Hooks integration | Event emission only | Optional plugin for UserPromptSubmit, PostToolUse, and Stop flush |

*New to these terms? **BM25** is keyword search, **dense (vector)** is meaning-based search, and **RRF** (Reciprocal Rank Fusion) merges the two — see the [Reference glossary](../reference.md#glossary).*

---

## Frequently Asked Questions

**Q: Does CLAUDE.md or MEMORY.md go away?**
No. memtomem operates as separate MCP tools (`mem_search`, etc.) and coexists independently with the existing system. Continue using CLAUDE.md for project instructions as before.

**Q: Do hooks slow down Claude Code?**
Only the optional automation plugin installs hooks. Prompt search runs before Claude processes the prompt and has a five-second cap; failures are fail-open. Diagnostics are written under `${CLAUDE_PLUGIN_DATA}/hook.log`.

**Q: It doesn't work after changing `.mcp.json`.**
Restart Claude Code or use the `/mcp` command to reconnect to MCP servers. Old processes may be using cached modules.

**Q: Can the same content be stored in both auto-memory and memtomem?**
Yes. Auto-memory automatically extracts from conversations, while memtomem only handles explicitly indexed/added targets.

---

## Cross-runtime agent context with `mm context`

If you also use Gemini CLI (or its Antigravity CLI successor) or Codex CLI on the same repo, treat `.memtomem/` as the single source of truth and let memtomem fan it out. Claude Code is the richest target — it preserves the canonical sub-agent fields most authors set (`name`, `description`, `tools`, `model`, `skills`, `isolation`), dropping only the rarely-used `kind` and `temperature`, so Claude is the natural place to author canonical agents and skills.

```bash
# Mirror .memtomem/skills/<name>/SKILL.md to .claude/skills/, .gemini/skills/, .agents/skills/
mm context sync --include=skills

# Fan out .memtomem/agents/<name>.md to .claude/agents/, .gemini/agents/, .codex/agents/
mm context sync --include=agents

# Fan out .memtomem/commands/<name>.md to .claude/commands/*.md and .gemini/commands/*.toml
mm context sync --include=commands
```

Sub-agent conversions are lossy for non-Claude targets — Gemini drops `skills` + `isolation`, Codex additionally drops `tools`, `kind`, `temperature`. Slash commands fan out to **Claude + Gemini only** — Codex command fan-out is not implemented (Codex custom prompts are upstream-deprecated; use a skill for Codex command-like workflows). memtomem reports every dropped field; add `--strict` to fail if you need 1:1 fidelity. Run `mm context --help` for the full per-runtime field-drop matrix.

---

## Next Steps

- [Reference](../reference.md) — Complete feature reference
- [Configuration](../configuration.md) — All `MEMTOMEM_*` environment variables
