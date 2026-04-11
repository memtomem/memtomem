# Gemini CLI x memtomem Integration Guide

**Audience**: Developers using Google Gemini CLI who want to add project knowledge search with memtomem
**Prerequisites**: memtomem installation complete ([Quick Start](../getting-started.md)), Gemini CLI installed
**Estimated Time**: About 10 minutes

---

## Overview

[Gemini CLI](https://github.com/google-gemini/gemini-cli) is Google's terminal-based AI agent
that uses Gemini 2.5 Pro/Flash models for code generation, review, and debugging.
Gemini CLI natively supports MCP servers, and connecting memtomem adds
**project document semantic search** and **code pattern indexing**.

### Role Separation Between the Two Systems

| Purpose | Responsible System |
|---------|--------------------|
| Code generation, review, debugging | Gemini CLI built-in |
| Project document semantic search | memtomem (`mem_search`) |
| Code pattern indexing (Markdown, JSON/YAML/TOML, Python/JS/TS) | memtomem (`mem_index`) |

---

## Step 1: MCP Registration

### Basic Configuration

Create or edit the `~/.gemini/settings.json` file:

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      
      "env": {
        "MEMTOMEM_STORAGE__SQLITE_PATH": "~/.memtomem/memtomem.db",
        "MEMTOMEM_INDEXING__MEMORY_DIRS": "[\"~/notes\"]"
      }
    }
  }
}
```

Restart Gemini CLI after configuring.

> **If you already have `mcpServers`**: Simply add the `"memtomem"` key to the existing object.

---

## Step 2: Verify Connection

In Gemini CLI:

```
Call the mem_status tool and tell me the memtomem connection status
```

Example of a successful response:
```
memtomem status:
  - Storage backend: SQLite
  - Total chunks: 0 (not yet indexed)
  - Embedding model: ollama/nomic-embed-text
```

---

## Step 3: First Indexing

```
Index my project docs directory
```

Agent:
```
mem_index(path="./docs", recursive=True)
→ "Indexed 23 files, 567 chunks in 1.8s"
```

---

## Tool Usage Guidelines (Add to GEMINI.md)

Adding the following to the `GEMINI.md` file in the project root helps Gemini
properly utilize memtomem tools:

```markdown
## Memory Tool Usage Guidelines

### memtomem (`mem_search`, `mem_index`, `mem_recall`)
- Search project ADRs, architecture documents, team onboarding materials
- Code pattern history, debugging records, decision records
- Long-term knowledge base managed as markdown files
- Time-range queries (`mem_recall`)

### Principles
- Something to find in project documents or code → `mem_search`
- When recording new knowledge/decisions → `mem_add`
- Check recent records → `mem_recall`

### Dual Memory Search
When users ask about past records ("previously", "what was decided", "what was it" etc.):
1. Check GEMINI.md / context first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```

---

## Usage Scenarios

### Scenario A: Project Document Indexing + Reference During Code Writing

```
User: "Implement endpoints based on the API design document"

Agent:
1. mem_search("API design endpoint spec") → Returns chunks from docs/api-spec.md
2. Generate code matching the spec
→ "Based on the API documentation, I implemented 3 endpoints: /users, /orders, /products."
```

---

## Built-in Features vs memtomem Comparison

| Feature | Gemini CLI Built-in | memtomem |
|---------|---------------------|---------|
| Code generation/review | **Built-in** (Gemini model) | None (complementary area) |
| Hybrid search (BM25 + Dense) | None | BM25 + Dense + RRF fusion |
| Markdown indexing | None | Heading-based semantic chunker |

---

## Frequently Asked Questions

**Q: memtomem tools aren't showing up in Gemini CLI.**
Add the `mcpServers` configuration to `~/.gemini/settings.json` and restart Gemini CLI. For per-project settings, write to `.gemini/settings.json`.

**Q: Can Gemini CLI and Claude Code share the same memtomem DB?**
Yes. Specifying the same `MEMTOMEM_STORAGE__SQLITE_PATH` allows both tools to access the same memory. However, be aware of potential SQLite WAL mode conflicts with frequent concurrent writes.

---

## Cross-runtime agent context with `mm context`

If you also use Claude Code or Codex CLI on the same repo, treat `.memtomem/` as the single source of truth and let memtomem fan it out. Gemini supports the `kind` and `temperature` sub-agent fields that Claude doesn't — authoring canonical agents with those fields set means Claude will drop them on its side with a warning.

```bash
# Skills: .memtomem/skills/<name>/SKILL.md fans out to .claude/skills/, .gemini/skills/, .agents/skills/
mm context sync --include=skills

# Sub-agents: .memtomem/agents/<name>.md → .claude/agents/, .gemini/agents/, ~/.codex/agents/
mm context sync --include=agents

# Slash commands: .memtomem/commands/<name>.md → .claude/commands/*.md and .gemini/commands/*.toml
mm context sync --include=commands
```

> **Note**: Gemini CLI's sub-agent format is marked experimental as of 2026-03; the frontmatter schema may still change. memtomem currently maps `name`, `description`, `tools`, `model`, `kind`, `temperature` and drops the Claude-specific `skills` + `isolation` fields on the Gemini side.

For slash commands memtomem parses Claude-flavoured Markdown (`$ARGUMENTS` placeholder) and emits Gemini TOML with the `prompt` + `description` fields and the `{{args}}` placeholder. Claude-only fields (`argument-hint`, `allowed-tools`, `model`) are dropped with a warning. Round-trip Gemini TOML → canonical Markdown is lossless because the TOML schema only has two fields.

See the [full matrix](../user-guide.md#agent-context-management--mm-context) in the user guide.

---

## Next Steps

- [User Guide](../user-guide.md) — Complete feature walkthrough
- [Practical Use Cases](../use-cases.md) — Agent workflow scenarios
