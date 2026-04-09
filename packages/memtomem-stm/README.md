# memtomem-stm

Short-term memory proxy gateway with **proactive memory surfacing** for AI agents.

Sits between your AI agent and upstream MCP servers. Compresses responses to save tokens, caches results, and automatically surfaces relevant memories from memtomem LTM.

```
Agent (Claude Code, Cursor, etc.)
    │
    ▼
┌──────────────────────────────────────┐
│        memtomem-stm (STM)            │
│                                      │
│  Pipeline per tool call:             │
│  1. CLEAN   — strip HTML, dedup      │
│  2. COMPRESS — selective/truncate    │
│  3. SURFACE  — inject LTM memories   │
│  4. INDEX    — auto-index to LTM     │
│                                      │
│  MCP Tools:                          │
│  ├─ stm_proxy_stats                  │
│  ├─ stm_proxy_select_chunks          │
│  ├─ stm_proxy_read_more              │
│  ├─ stm_proxy_cache_clear            │
│  ├─ stm_surfacing_feedback           │
│  ├─ stm_surfacing_stats              │
│  └─ {prefix}__{tool} (proxied)       │
└──────────┬───────────────────────────┘
           │ stdio / SSE / HTTP
     ┌─────┴──────┐
     ▼            ▼
 [filesystem]  [github]
  MCP server    MCP server
```

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [How the Pipeline Works](#how-the-pipeline-works)
- [Compression Strategies](#compression-strategies)
- [Proactive Memory Surfacing](#proactive-memory-surfacing)
- [Response Caching](#response-caching)
- [Auto-Indexing](#auto-indexing)
- [Configuration Reference](#configuration-reference)
- [CLI Commands](#cli-commands)
- [MCP Tools](#mcp-tools-5--proxied)
- [Safety & Resilience](#safety--resilience)
- [Privacy](#privacy)
- [Horizontal Scaling](#horizontal-scaling)
- [Observability](#observability)
- [Data Storage](#data-storage)
- [Testing](#testing)

---

## Installation

```bash
# Standalone (proxy + compression only)
pip install memtomem-stm

# With LTM integration (proactive surfacing)
pip install "memtomem-stm[ltm]"

# With Langfuse tracing
pip install "memtomem-stm[langfuse]"
```

## Quick Start

### 1. Add upstream servers

```bash
# Add a filesystem MCP server
memtomem-stm-proxy add filesystem \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem /home/user/projects" \
  --prefix fs

# Add a GitHub MCP server
memtomem-stm-proxy add github \
  --command npx \
  --args "-y @modelcontextprotocol/server-github" \
  --prefix gh \
  --env GITHUB_TOKEN=ghp_xxx
```

### 2. Configure your MCP client

Point your AI agent's MCP client config to the STM server:

```json
{
  "mcpServers": {
    "memtomem-stm": {
      "command": "memtomem-stm"
    }
  }
}
```

### 3. Use proxied tools

Your agent now sees `fs__read_file`, `gh__search_repositories`, etc. Responses are automatically compressed, cached, and enriched with relevant memories.

### 4. (Optional) Interactive setup via memtomem CLI

If you have the core `memtomem` package installed, run the 8-step wizard:

```bash
mm stm init
```

This detects existing MCP client configs (Claude Code, Cursor, Claude Desktop), lets you select servers to proxy, choose compression strategies, enable caching/Langfuse, and writes everything to `~/.memtomem/stm_proxy.json`.

To undo: `mm stm reset` restores original configs and removes STM.

---

## How the Pipeline Works

Every proxied tool call goes through 4 stages:

### Stage 1: CLEAN

Removes noise from the upstream response before compression:

- **`<script>`/`<style>` removal** — content and tags fully stripped before other processing
- **HTML stripping** — removes tags (preserves code fences and generic types like `List<String>`)
- **Paragraph deduplication** — removes identical paragraphs
- **Link flood collapse** — replaces paragraphs where 80%+ lines are links (10+ lines) with `[N links omitted]`
- **Whitespace normalization** — collapses triple+ newlines to double

Each cleaning step can be individually toggled per server:

```json
{
  "cleaning": {
    "strip_html": true,
    "deduplicate": true,
    "collapse_links": true
  }
}
```

### Stage 2: COMPRESS

Reduces response size to save tokens. See [Compression Strategies](#compression-strategies) below.

### Stage 3: SURFACE

Proactively injects relevant memories from LTM. See [Proactive Memory Surfacing](#proactive-memory-surfacing) below.

Only activates when the compressed response is >= `min_response_chars` (default 5000 chars). For small responses, surfacing is skipped to avoid negative token savings.

### Stage 4: INDEX (optional)

Automatically indexes large responses to memtomem LTM for future retrieval:

```json
{
  "auto_index": {
    "enabled": true,
    "min_chars": 2000,
    "memory_dir": "~/.memtomem/proxy_index",
    "namespace": "proxy-{server}"
  }
}
```

Indexed files are written as markdown with frontmatter (source, timestamp, compression stats).

### Stage 4b: Auto Fact Extraction (optional)

Automatically extracts discrete facts from tool responses using an LLM:

```json
{
  "extraction": {
    "enabled": true,
    "strategy": "llm",
    "llm": {
      "provider": "ollama",
      "model": "qwen3:4b"
    }
  }
}
```

Strategies: `llm` (default, Ollama qwen3:4b with no-think mode), `heuristic` (entity extraction fallback), `hybrid` (merge both), `none`. Each extracted fact is stored as an individual `.md` file and indexed for future search. Deduplication via embedding similarity (threshold 0.92).

Per-tool override: `"extraction": true|false` in `tool_overrides` or `UpstreamServerConfig`.

---

## Compression Strategies

| Strategy | Best for | Description |
|----------|----------|-------------|
| **auto** (default) | All responses | Content-aware: picks the best strategy per response based on content type |
| **hybrid** | Large structured docs | Preserves first ~5K chars + TOC for remainder |
| **selective** | Large structured data | 2-phase: returns TOC only, then retrieve selected sections on demand |
| **truncate** | Simple limiting | Section-aware for markdown (minimum representation for ALL sections, then enriches by relevance); query-aware budget allocation when `_context_query` provided |
| **extract_fields** | JSON configs | Preserves all top-level keys with nested structure + first values |
| **schema_pruning** | Large JSON arrays | Recursive pruning: first 2 + last 1 items sampled per array |
| **skeleton** | API docs | All headings + first content line per section |
| **progressive** | Large any-type content | Zero-loss: stores full content, delivers in chunks on demand via `stm_proxy_read_more` |
| **llm_summary** | High-value content | Calls external LLM (OpenAI/Anthropic/Ollama) to summarize |
| **none** | Passthrough | No compression (cache only) |

### Selective Compression (2-phase)

**Phase 1:** STM parses the response into sections and returns a compact TOC:

```json
{
  "type": "toc",
  "selection_key": "abc123def456",
  "format": "json",
  "total_chars": 50000,
  "entries": [
    {"key": "README", "type": "heading", "size": 200, "preview": "..."},
    {"key": "src/main.py", "type": "heading", "size": 5000, "preview": "..."}
  ],
  "hint": "Call stm_proxy_select_chunks(key='abc123def456', sections=[...]) to retrieve."
}
```

**Phase 2:** Agent calls `stm_proxy_select_chunks` to retrieve only the sections it needs.

Auto-detects format: JSON dicts (parsed by keys), JSON arrays (parsed by index), Markdown (parsed by headings), plain text (parsed by paragraphs).

Pending selections are stored for 5 minutes (max 100 concurrent), then auto-evicted.

### Hybrid Compression

Combines immediate access with selective retrieval:

```
┌─────────────────────────────────┐
│  HEAD (first 5000 chars)        │  ← Immediately available
├─────────────────────────────────┤
│  --- Remaining content (45K) ---│
│  Table of Contents:             │  ← Selective retrieval
│  • Section A (2K chars)         │
│  • Section B (8K chars)         │
│  ...                            │
└─────────────────────────────────┘
```

Configurable per server:

```json
{
  "hybrid": {
    "head_chars": 5000,
    "tail_mode": "toc",
    "head_ratio": 0.6,
    "min_toc_budget": 200
  }
}
```

### Progressive Delivery (cursor-based)

Inspired by how Claude Code reads files progressively (150 lines at a time), progressive delivery stores the full cleaned content and delivers it in chunks on demand — **zero information loss**.

```
Agent ← first 4000 chars + footer metadata
Agent → stm_proxy_read_more(key="abc123", offset=4000)
Agent ← next 4000 chars + footer metadata
Agent → stm_proxy_read_more(key="abc123", offset=8000)
Agent ← final chunk (has_more=false)
```

The first chunk includes a metadata footer with remaining headings/structure hints so the agent can decide whether to continue reading.

| Feature | Selective | Progressive |
|---------|-----------|-------------|
| Access pattern | By name (random) | By offset (sequential) |
| Requires structure | Yes (headings/JSON keys) | No (any content) |
| Information loss | None (section-level) | None (full content) |
| Use case | "Show me the Config section" | "Read through this file" |

Configure per server or per tool:

```json
{
  "compression": "progressive",
  "progressive": {
    "chunk_size": 4000,
    "max_stored": 200,
    "ttl_seconds": 600,
    "include_structure_hint": true
  }
}
```

Progressive is **opt-in only** — `auto` strategy never selects it because it changes the agent interaction pattern (requires calling `stm_proxy_read_more`).

### LLM Compression

Routes through an external LLM for intelligent summarization:

```json
{
  "llm": {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_key": "sk-...",
    "max_tokens": 500,
    "system_prompt": "Summarize concisely, preserving key information. Under {max_chars} chars."
  }
}
```

Providers: `openai`, `anthropic`, `ollama`. Falls back to truncation on API failure (circuit breaker protection).

Sensitive content (API keys, passwords, PII) is auto-detected and **never** sent to external LLMs — falls back to local truncation.

### Query-Aware Compression

When an agent provides `_context_query` in tool arguments, compression allocates budget proportionally to section relevance instead of fixed top-down order. This preserves more information from query-relevant sections.

```json
{
  "relevance_scorer": {
    "scorer": "bm25",
    "embedding_provider": "ollama",
    "embedding_model": "nomic-embed-text",
    "embedding_base_url": "http://localhost:11434",
    "embedding_timeout": 10.0
  }
}
```

| Scorer | Latency | Cross-language | Dependencies |
|--------|---------|----------------|--------------|
| `bm25` (default) | <1ms | No | None |
| `embedding` | 5-50ms | Yes | Ollama/OpenAI |

`RelevanceScorer` protocol (`proxy/relevance.py`) enables custom scorer implementations. `EmbeddingScorer` uses sync httpx to call embedding APIs with automatic BM25 fallback on error.

### Per-server and Per-tool Overrides

```json
{
  "upstream_servers": {
    "github": {
      "prefix": "gh",
      "compression": "hybrid",
      "max_result_chars": 16000,
      "tool_overrides": {
        "search_code": {
          "compression": "selective",
          "max_result_chars": 8000
        },
        "get_file_contents": {
          "compression": "none"
        }
      }
    }
  }
}
```

---

## Proactive Memory Surfacing

When your agent calls a proxied tool, STM automatically:

1. **Extracts context** from the tool name and arguments
2. **Checks relevance** (rate limit, cooldown, write-tool filter)
3. **Searches LTM** (memtomem) for related memories
4. **Injects relevant memories** at the top of the response

### How Context Extraction Works

STM extracts a search query in priority order:

1. **Per-tool template** — `"query_template": "file {arg.path}"` → `"file /src/main.py"`
2. **Agent-provided** — `_context_query` argument if present
3. **Path tokenization** — file paths like `/src/auth/jwt_handler.py` are auto-split on `/._-` separators → `"src auth jwt handler py"`
4. **Heuristic** — extracts string values from semantic keys (`query`, `path`, `file`, `url`, `topic`, `name`, `title`, `description`). Skips UUIDs, hex strings, booleans.
5. **Fallback** — tool name with underscores replaced (`search_repositories` → `"search repositories"`)

Queries shorter than `min_query_tokens` (default 3) are skipped.

### What the Agent Sees

When memories are found, they're injected before the response:

```
## Relevant Memories

- **auth_notes.md** [code-notes] (score=0.85): OAuth2 implementation uses PKCE flow...
- **api_design.md** (score=0.72): Rate limiting is handled by middleware in...

_Surfacing ID: abc123def456 — call `stm_surfacing_feedback` to rate_

---

(original tool response here)
```

The injection mode is configurable: `prepend` (default), `append`, or `section`.

### Surfacing Controls

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Global on/off switch |
| `min_score` | `0.02` | Minimum search score to include a result |
| `max_results` | `3` | Maximum memories surfaced per tool call |
| `max_injection_chars` | `2000` | Maximum total chars injected (truncated if exceeded) |
| `min_response_chars` | `5000` | Skip surfacing for small responses |
| `min_query_tokens` | `3` | Skip if extracted query has fewer tokens |
| `timeout_seconds` | `3.0` | Surfacing timeout (falls back to original response) |
| `cooldown_seconds` | `5.0` | Skip duplicate queries (Jaccard > 0.95) within this window |
| `max_surfacings_per_minute` | `15` | Global rate limit |
| `injection_mode` | `prepend` | Where to inject: `prepend`, `append`, `section` |
| `section_header` | `## Relevant Memories` | Header text for injected section |
| `default_namespace` | `null` | Restrict search to a specific namespace |
| `exclude_tools` | `[]` | fnmatch patterns to never surface (e.g. `["*debug*"]`) |
| `write_tool_patterns` | `*write*`, `*create*`, etc. | Auto-skip write/mutation operations |
| `include_session_context` | `true` | Include working memory (scratch) items |
| `session_dedup` | `true` | Same memory ID not shown twice in one session |

### Per-tool Templates

Fine-tune surfacing behavior per tool:

```json
{
  "surfacing": {
    "context_tools": {
      "read_file": {
        "enabled": true,
        "query_template": "file {arg.path}",
        "namespace": "code-notes",
        "min_score": 0.1,
        "max_results": 5
      },
      "search_issues": {
        "min_score": 0.5,
        "max_results": 2
      },
      "get_diff": {
        "enabled": false
      }
    }
  }
}
```

Template variables: `{tool_name}`, `{server}`, `{arg.ARGUMENT_NAME}`

### LTM Connection

STM connects to the LTM exclusively over the MCP protocol. The surfacing
engine spawns (or attaches to) a memtomem MCP server using these settings:

```bash
# Default — spawns `memtomem-server` as a child process
export MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=memtomem-server

# Pass extra arguments if needed (e.g. point at a custom config)
export MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS='["--config","/etc/memtomem.json"]'
```

This makes memtomem just another MCP upstream as far as STM is concerned —
the same compression / cache / surfacing pipeline applies, and a memtomem
crash never takes down STM's other upstream connections.

> **Note**: prior versions supported an in-process mode that imported
> memtomem directly. That path was removed so STM has a single LTM
> retrieval path and so core internals can evolve without breaking STM.

### Feedback & Auto-Tuning

Rate surfaced memories to improve future relevance:

```
stm_surfacing_feedback(surfacing_id="abc123", rating="helpful")
stm_surfacing_feedback(surfacing_id="def456", rating="not_relevant")
stm_surfacing_feedback(surfacing_id="ghi789", rating="already_known")
```

Valid ratings: `helpful`, `not_relevant`, `already_known`.

When auto-tuning is enabled (default), STM adjusts `min_score` per tool based on feedback:

| Feedback ratio | Action |
|----------------|--------|
| > 60% `not_relevant` | Raise `min_score` by +0.002 (surface fewer, more relevant) |
| < 20% `not_relevant` | Lower `min_score` by -0.002 (surface more) |

Requires `auto_tune_min_samples` (default 20) feedback entries before adjusting. Score is capped between 0.005 and 0.05. **Cold-start fallback**: new tools with insufficient samples use the global ratio across all tools instead of waiting for 20 per-tool samples.

**Search boost from feedback**: when you rate memories as "helpful", their `access_count` is incremented in the core search index (once per surfacing event, capped at `max_boost=1.5`). This creates a positive feedback loop where useful memories rank higher in future searches.

Check effectiveness with `stm_surfacing_stats`:

```
Surfacing Stats
===============
Total surfacings: 142
Total feedback:   38

By rating:
  helpful: 28
  not_relevant: 7
  already_known: 3

Helpfulness: 73.7%
```

---

## Response Caching

Proxied tool responses are cached in SQLite to avoid repeated upstream calls:

```json
{
  "cache": {
    "enabled": true,
    "db_path": "~/.memtomem/proxy_cache.db",
    "default_ttl_seconds": 3600,
    "max_entries": 10000
  }
}
```

Key details:
- Cache key = SHA-256 of `server:tool:args` (argument order independent)
- **Pre-surfacing content is cached** — surfacing is re-applied on cache hit, so memories stay fresh
- Expired entries are purged on startup; oldest entries evicted when `max_entries` exceeded
- Clear cache via MCP tool: `stm_proxy_cache_clear(server="gh", tool="search_code")`
- TTL can be overridden per-tool via `tool_overrides`

---

## Auto-Indexing

When enabled, large tool responses are automatically saved to memtomem LTM for future retrieval:

```json
{
  "auto_index": {
    "enabled": true,
    "min_chars": 2000,
    "memory_dir": "~/.memtomem/proxy_index",
    "namespace": "proxy-{server}"
  }
}
```

Each indexed response creates a markdown file with frontmatter:

```markdown
---
source: proxy/github/search_code
timestamp: 2026-04-05T12:00:00+00:00
compression: hybrid
original_chars: 50000
compressed_chars: 8000
---

# Proxy Response: github/search_code

- **Source**: `github/search_code(query="auth middleware")`
- **Original size**: 50000 chars

## Content

(compressed response content)
```

The namespace supports `{server}` and `{tool}` placeholders. Can be toggled per-server via `auto_index: true|false` in `UpstreamServerConfig`.

---

## Model-Aware Defaults

When `consumer_model` is set, STM automatically scales settings for the consuming model's context window. Set it once — compression budget, surfacing injection size, and result count all adjust.

```bash
export MEMTOMEM_STM_PROXY__CONSUMER_MODEL=claude-sonnet-4
```

### Recommended Settings by Model Size

| Setting | SLM (≤32K) | Medium (32K-200K) | LLM (>200K) |
|---------|------------|-------------------|--------------|
| `max_result_chars` | ~5,600 | ~16,000 | ~35,000 |
| `max_injection_chars` | 1,500 | 3,000 | 5,000 |
| `max_results` (surfacing) | 2 | 3 | 5 |
| `context_window` | 0-1 | 1-2 | 2-5 |
| Compression strategy | skeleton / truncate | auto (default) | auto / none |

### Model Examples

| Model | Context | Tier | Notes |
|-------|---------|------|-------|
| `o1-mini` | 128K | Medium | Default settings work well |
| `gpt-4o` | 128K | Medium | Default settings work well |
| `gpt-4.1` | 1M | LLM | Generous budget, more surfacing |
| `gpt-4.1-mini` | 1M | LLM | Generous budget, more surfacing |
| `claude-sonnet-4` | 200K | Medium | Default settings work well |
| `claude-opus-4` | 200K | Medium | Default settings work well |
| `o3` / `o4-mini` | 200K | Medium | Reasoning models, default settings |
| `gemini-2.5-pro` | 1M | LLM | Generous budget, more surfacing |
| `llama-4-scout` | 512K | LLM | Open-weight, generous budget |
| `deepseek-r1` | 131K | Medium | Default settings work well |
| `qwen-3` | 131K | Medium | Default settings work well |

All scaling is automatic when `consumer_model` is set. Override any value explicitly to disable auto-scaling for that setting.

---

## Configuration Reference

### Environment Variables

All settings use the `MEMTOMEM_STM_` prefix with `__` nesting:

```bash
# Proxy settings
export MEMTOMEM_STM_PROXY__ENABLED=true
export MEMTOMEM_STM_PROXY__DEFAULT_COMPRESSION=auto
export MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS=16000
export MEMTOMEM_STM_PROXY__MIN_RESULT_RETENTION=0.65
export MEMTOMEM_STM_PROXY__CONSUMER_MODEL=claude-sonnet-4
export MEMTOMEM_STM_PROXY__CONTEXT_BUDGET_RATIO=0.05
export MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS=200
export MEMTOMEM_STM_PROXY__STRIP_SCHEMA_DESCRIPTIONS=false
export MEMTOMEM_STM_PROXY__CACHE__ENABLED=true
export MEMTOMEM_STM_PROXY__CACHE__DEFAULT_TTL_SECONDS=3600
export MEMTOMEM_STM_PROXY__METRICS__ENABLED=true

# Surfacing settings
export MEMTOMEM_STM_SURFACING__ENABLED=true
export MEMTOMEM_STM_SURFACING__MIN_SCORE=0.02
export MEMTOMEM_STM_SURFACING__MAX_RESULTS=3
export MEMTOMEM_STM_SURFACING__MIN_RESPONSE_CHARS=5000
export MEMTOMEM_STM_SURFACING__FEEDBACK_ENABLED=true
export MEMTOMEM_STM_SURFACING__AUTO_TUNE_ENABLED=true

# Langfuse tracing (optional)
export MEMTOMEM_STM_LANGFUSE__ENABLED=true
export MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY=pk-...
export MEMTOMEM_STM_LANGFUSE__SECRET_KEY=sk-...
export MEMTOMEM_STM_LANGFUSE__HOST=https://cloud.langfuse.com
```

### Config File (`~/.memtomem/stm_proxy.json`)

Full example with all options:

```json
{
  "enabled": true,
  "default_max_result_chars": 16000,
  "min_result_retention": 0.65,
  "consumer_model": "",
  "context_budget_ratio": 0.05,
  "max_description_chars": 200,
  "strip_schema_descriptions": false,
  "upstream_servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
      "prefix": "fs",
      "transport": "stdio",
      "compression": "auto",
      "max_result_chars": 8000,
      "max_retries": 3,
      "reconnect_delay_seconds": 1.0,
      "max_reconnect_delay_seconds": 30.0,
      "max_description_chars": 200,
      "strip_schema_descriptions": false,
      "cleaning": {
        "strip_html": true,
        "deduplicate": true,
        "collapse_links": true
      },
      "selective": {
        "max_pending": 100,
        "pending_ttl_seconds": 300,
        "pending_store": "memory",
        "pending_store_path": "~/.memtomem/pending_selections.db"
      },
      "hybrid": {
        "head_chars": 5000,
        "tail_mode": "toc",
        "head_ratio": 0.6
      },
      "progressive": {
        "chunk_size": 4000,
        "max_stored": 200,
        "ttl_seconds": 600
      },
      "tool_overrides": {
        "read_file": {
          "compression": "progressive"
        },
        "internal_debug": {
          "hidden": true
        }
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "prefix": "gh",
      "env": { "GITHUB_TOKEN": "ghp_xxx" },
      "compression": "auto",
      "max_result_chars": 16000,
      "auto_index": true,
      "tool_overrides": {
        "search_code": {
          "compression": "selective",
          "max_result_chars": 8000
        }
      }
    }
  },
  "cache": {
    "enabled": true,
    "default_ttl_seconds": 3600,
    "max_entries": 10000
  },
  "auto_index": {
    "enabled": false,
    "min_chars": 2000,
    "namespace": "proxy-{server}"
  },
  "metrics": {
    "enabled": true,
    "max_history": 10000
  }
}
```

Config file is **hot-reloaded** — changes take effect on the next tool call without restarting.

### Transport Types

| Transport | Config fields | Description |
|-----------|---------------|-------------|
| `stdio` (default) | `command`, `args`, `env` | Standard subprocess MCP server |
| `sse` | `url`, `headers` | Server-Sent Events over HTTP |
| `streamable_http` | `url`, `headers` | HTTP streamable responses |

---

## CLI Commands

```bash
memtomem-stm-proxy status                  # Show config and server list
memtomem-stm-proxy list                    # List upstream servers (table format)
memtomem-stm-proxy add <name> \            # Add upstream server
  --command <cmd> \
  --args "<args>" \
  --prefix <pfx> \
  --transport stdio|sse|streamable_http \
  --compression none|truncate|selective|hybrid \
  --max-chars 8000 \
  --env KEY=VALUE
memtomem-stm-proxy remove <name> [-y]      # Remove upstream server
```

## MCP Tools (6 + proxied)

| Tool | Arguments | Description |
|------|-----------|-------------|
| `stm_proxy_stats` | — | Token savings, compression stats, cache hit/miss ratio |
| `stm_proxy_select_chunks` | `key`, `sections[]` | Retrieve sections from a selective/hybrid TOC response |
| `stm_proxy_read_more` | `key`, `offset`, `limit?` | Read next chunk from a progressive delivery response |
| `stm_proxy_cache_clear` | `server?`, `tool?` | Clear response cache (all, by server, or by server+tool) |
| `stm_surfacing_feedback` | `surfacing_id`, `rating`, `memory_id?` | Rate surfaced memories (`helpful` / `not_relevant` / `already_known`) |
| `stm_surfacing_stats` | `tool?` | Surfacing event counts, feedback breakdown, helpfulness % |

Plus all proxied tools named `{prefix}__{original_tool_name}` (e.g. `fs__read_file`, `gh__search_repositories`).

---

## Safety & Resilience

### Circuit Breaker

Unified 3-state circuit breaker protects against cascading failures:

```
closed ──(3 failures)──→ open ──(60s timeout)──→ half-open
  ↑                                                  │
  └──────────(success)──────────────────────────────←─┤
                                                      │
open ←──────(failure)─────────────────────────────────┘
```

- **Closed**: all calls pass through normally
- **Open**: all surfacing/LLM calls blocked (falls back to original response or truncation)
- **Half-open**: allows exactly one probe call after timeout; success closes, failure re-opens

Applied to both surfacing (LTM search) and LLM compression (external API calls).

### Connection Recovery

- **Retry with backoff**: transport errors retried up to `max_retries` (default 3) with exponential backoff (1s → 2s → 4s → max 30s)
- **Protocol error isolation**: JSON-RPC errors (-32600 to -32603) are not retried — connection is reset for the next call
- **Error type filtering**: only transport errors (`OSError`, `ConnectionError`, `TimeoutError`, `EOFError`) and MCP errors trigger retry. Programming errors (`TypeError`, `AttributeError`) propagate immediately.

### Other Protections

- **Timeout**: 3s surfacing timeout — falls back to original compressed response
- **Rate limiting**: Max 15 surfacings per minute (sliding window)
- **Write-tool skip**: Never surfaces for `*write*`, `*create*`, `*delete*`, `*push*`, `*send*`, `*remove*` tools
- **Query cooldown**: Deduplicates similar queries (Jaccard similarity > 0.95) within 5s window
- **Response size gate**: Skips surfacing for responses under `min_response_chars` (default 5000)
- **Session dedup**: Same memory ID not shown twice in one session
- **Cross-session dedup**: Recently surfaced memory IDs persisted to SQLite; not re-surfaced within `dedup_ttl_seconds` (default 7 days). Set to `0` to disable.
- **Injection size cap**: Memory block truncated if total exceeds `max_injection_chars` (default 3000)
- **Boost guard**: Each surfacing event can only boost `access_count` once (duplicate feedback ignored)
- **Fresh cache**: Proxy cache stores pre-surfacing content; surfacing is re-applied on cache hit so memories stay current

---

## Privacy

Sensitive content is auto-detected and never sent to external LLM compression:

| Pattern | Example |
|---------|---------|
| API keys/tokens | `api_key=...`, `sk-xxxx`, `ghp_xxxx`, `xoxb-...` |
| Passwords | `password=...`, `passwd: ...` |
| Email addresses | `user@example.com` |
| Private keys | `BEGIN RSA PRIVATE KEY` |

Detection scans the first 10K characters. When sensitive content is found, LLM compression falls back to local truncation.

---

## Horizontal Scaling

By default, `SelectiveCompressor` stores pending TOC selections in memory. For multi-instance deployments, switch to SQLite-backed storage so instances share state:

```json
{
  "upstream_servers": {
    "filesystem": {
      "selective": {
        "pending_store": "sqlite",
        "pending_store_path": "~/.memtomem/pending_selections.db"
      }
    }
  }
}
```

| Backend | Config value | Use case |
|---------|-------------|----------|
| `memory` (default) | In-process dict + deque | Single instance, zero overhead |
| `sqlite` | SQLite with WAL mode | Multiple instances sharing TOC state |

With `sqlite`, instance A can create a TOC and instance B can `stm_proxy_select_chunks` to retrieve sections from that TOC.

---

## Observability

### Metrics

Token savings, error rates, and latency tracked per server and tool:

```
STM Proxy Stats
===============
Total calls:     247       Current RPS: 2.5
Original chars:  1,234,567
Compressed:      345,678
Savings:         72.0%
Token savings:   68.3%
Cache hits:      89
Cache misses:    158
Errors:          3 (1.2%)
  transport: 2, timeout: 1

Latency (ms):
  clean:    p50=0.1  p95=0.5  p99=1.2
  compress: p50=0.3  p95=2.1  p99=8.5
  surface:  p50=15   p95=120  p99=450
  total:    p50=16   p95=125  p99=460

Upstream Health:
  filesystem: connected (12 tools)
  github:     connected (8 tools)

By server:
  filesystem: 142 calls, 800K → 200K chars (75.0% saved)
  github: 105 calls, 434K → 145K chars (66.6% saved)
```

**Error classification**: errors are categorized as `transport`, `timeout`, `protocol`, `upstream_error`, or `programming`. Each failed call records the error category and code for debugging.

**Trace IDs**: every proxy call generates a unique `trace_id` (16-char hex) for correlating logs and metrics.

Metrics persisted to SQLite (`~/.memtomem/proxy_metrics.db`, max 10K entries) with error category and trace_id columns.

### Langfuse Tracing (optional)

```bash
pip install "memtomem-stm[langfuse]"

export MEMTOMEM_STM_LANGFUSE__ENABLED=true
export MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY=pk-...
export MEMTOMEM_STM_LANGFUSE__SECRET_KEY=sk-...
export MEMTOMEM_STM_LANGFUSE__HOST=https://cloud.langfuse.com
```

Traces proxy calls for latency analysis and debugging.

---

## Data Storage

| File | Purpose | Managed by |
|------|---------|------------|
| `~/.memtomem/stm_proxy.json` | Upstream server config (hot-reloaded) | CLI / `mm stm init` |
| `~/.memtomem/proxy_cache.db` | Response cache (SQLite, WAL mode) | ProxyCache |
| `~/.memtomem/proxy_metrics.db` | Compression metrics history | MetricsStore |
| `~/.memtomem/stm_feedback.db` | Surfacing events & feedback ratings | FeedbackStore |
| `~/.memtomem/pending_selections.db` | Shared pending TOC state (horizontal scaling) | SQLitePendingStore |
| `~/.memtomem/proxy_index/*.md` | Auto-indexed responses | auto-index pipeline |

---

## Testing

```bash
# Run STM tests
uv run pytest packages/memtomem-stm/tests/ -v

# Run a specific test file
uv run pytest packages/memtomem-stm/tests/test_compression.py -v
```

735 tests covering:

| Test file | Coverage |
|-----------|----------|
| `test_circuit_breaker.py` | State machine transitions (closed/open/half-open) |
| `test_compression.py` | All 6 compression strategies + auto_select_strategy |
| `test_relevance_gate.py` | Exclusions, write-tool heuristic, rate limit, cooldown, Jaccard similarity |
| `test_context_extractor.py` | Query templates, heuristic extraction, path tokenization, identifier detection |
| `test_feedback.py` | FeedbackStore, FeedbackTracker, AutoTuner feedback loop, cold-start fallback |
| `test_proxy_cache.py` | TTL expiration, eviction, clear, key generation |
| `test_cleaning.py` | HTML stripping, script/style removal, deduplication, link flood collapse |
| `test_surfacing_cache.py` | In-memory TTL cache, eviction, empty list caching |
| `test_surfacing_engine.py` | Surfacing pipeline, session dedup, circuit breaker, cooldown, rate limit, feedback boost |
| `test_formatter.py` | Injection modes (prepend/append/section), size cap, source badges |
| `test_proxy_manager.py` | ToolConfig resolution, error handling, cleaning+compression pipeline |
| `test_config_persistence.py` | Hot reload, MetricsStore, FeedbackStore persistence, TokenTracker, privacy patterns |
| `test_stm_integration.py` | End-to-end pipeline (clean→compress→surface), selective 2-phase, auto-tuner loop |
| `test_effectiveness.py` | Decision quality, context extraction accuracy, compression ratio, feedback loop |
| `test_information_loss.py` | Content preservation across compression strategies, structural integrity |
| `test_proxy_error_paths.py` | Transport failure retry/reconnect, protocol error, exponential backoff, cache interaction |
| `test_latency_percentiles.py` | Percentile computation (p50/p95/p99), TokenTracker integration |
| `test_cross_session_dedup.py` | SQLite seen_memories persistence, TTL-based dedup, engine integration |
| `test_stress_concurrency.py` | 1-5MB payloads, concurrent calls, SelectiveCompressor lock |
| `test_auto_compression.py` | AUTO strategy selection, passthrough, per-content-type routing |
| `test_error_metrics.py` | ErrorCategory enum, record_error, MetricsStore migration, CircuitBreaker properties |
| `test_tool_metadata.py` | Description truncation, schema distillation, hidden tools, token savings |
| `test_context_window.py` | Model context window lookup, effective_max_result_chars, prefix matching |
| `test_observability.py` | RPSTracker, trace_id propagation, MetricsStore trace_id, upstream health |
| `test_pending_store.py` | InMemory/SQLite PendingStore, persistence, concurrency, multi-instance sharing |
| `test_extraction.py` | Auto fact extraction: JSON parsing, heuristic/LLM strategies, circuit breaker |
| `test_progressive.py` | Progressive delivery: chunker boundaries, store adapter, content integrity |
| `test_bench_pipeline.py` | 181 benchmark tests: quality scoring, statistical analysis, dataset coverage |

## License

Apache-2.0
