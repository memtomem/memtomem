---
name: setup
description: Guide through initial memtomem setup and optional hooks activation.
allowed-tools: mcp__memtomem__mem_status, mcp__memtomem__mem_config, mcp__memtomem__mem_index
disable-model-invocation: true
---

Guide the user through memtomem initial setup:

## Step 1: Status Check

Run `mem_status` to check current state. Verify:
- Storage backend is initialized
- Embedding provider is reachable (ollama by default at localhost:11434)

If embedding provider is not running, guide the user to start it:
```bash
# Ollama (default)
ollama pull nomic-embed-text && ollama serve

# Or switch to OpenAI
# Set MEMTOMEM_EMBEDDING__PROVIDER=openai and MEMTOMEM_EMBEDDING__API_KEY
```

## Step 2: Index Memory Directory

Ask the user which directory contains their notes/documents.
Run `mem_index` on that directory. Show results.

Default directory: `~/.memtomem/memories`

## Step 3: Verify Search

Run a test search with `mem_search` using a topic from the indexed files.
Confirm results are returned.

## Step 4: Hooks Activation (Optional)

The plugin includes automation hooks that:
- **UserPromptSubmit**: Auto-search relevant memories on every prompt
- **PostToolUse**: Auto-reindex files after edits

Hooks require the memtomem CLI in PATH. Guide the user:
```bash
# PyPI
uv tool install memtomem
# or: pipx install memtomem

# Source (if running from git clone)
# uv run mm ...
```

After CLI installation, hooks will activate automatically.
Without CLI, hooks fail silently — MCP tools still work normally.

## Step 5: CLAUDE.md Integration

Suggest adding dual memory search guidelines to the project's CLAUDE.md:

```markdown
## Memory Search Principle
When users ask about past records or decisions:
1. Check MEMORY.md (auto-loaded 200 lines) first
2. If absent or insufficient, use `mem_search` for semantic search
3. Synthesize both results in the response
```
