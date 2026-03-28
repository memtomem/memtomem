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

---

## Compression Strategies

| Strategy | Best for | Description |
|----------|----------|-------------|
| **hybrid** (default) | General use | Preserves first ~5K chars + TOC for remainder |
| **selective** | Large structured data | 2-phase: returns TOC only, then retrieve selected sections on demand |
| **truncate** | Simple limiting | Sentence-boundary-aware character limit with structural metadata |
| **extract_fields** | JSON responses | Preserves key structure, truncates long values, shows array previews |
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
3. **Heuristic** — extracts string values from semantic keys (`query`, `path`, `file`, `url`, `topic`, `name`, `title`, `description`). Skips UUIDs, hex strings, booleans.
4. **Fallback** — tool name with underscores replaced (`search_repositories` → `"search repositories"`)

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

### LTM Connection Modes

| Mode | Config | Description |
|------|--------|-------------|
| **in_process** (default) | `ltm_mode: "in_process"` | Imports memtomem directly (faster, requires `memtomem` installed) |
| **mcp_client** | `ltm_mode: "mcp_client"` | Connects to remote memtomem server via MCP (isolated, works without local memtomem) |

For MCP client mode:

```bash
export MEMTOMEM_STM_SURFACING__LTM_MODE=mcp_client
export MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=memtomem-server
```

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

Requires `auto_tune_min_samples` (default 20) feedback entries before adjusting. Score is capped between 0.005 and 0.05.

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

## Configuration Reference

### Environment Variables

All settings use the `MEMTOMEM_STM_` prefix with `__` nesting:

```bash
# Proxy settings
export MEMTOMEM_STM_PROXY__ENABLED=true
export MEMTOMEM_STM_PROXY__DEFAULT_COMPRESSION=hybrid
export MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS=16000
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
  "upstream_servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"],
      "prefix": "fs",
      "transport": "stdio",
      "compression": "hybrid",
      "max_result_chars": 8000,
      "max_retries": 3,
      "reconnect_delay_seconds": 1.0,
      "max_reconnect_delay_seconds": 30.0,
      "cleaning": {
        "strip_html": true,
        "deduplicate": true,
        "collapse_links": true
      },
      "hybrid": {
        "head_chars": 5000,
        "tail_mode": "toc",
        "head_ratio": 0.6
      },
      "tool_overrides": {
        "read_file": {
          "compression": "none"
        }
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "prefix": "gh",
      "env": { "GITHUB_TOKEN": "ghp_xxx" },
      "compression": "selective",
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

## MCP Tools (5 + proxied)

| Tool | Arguments | Description |
|------|-----------|-------------|
| `stm_proxy_stats` | — | Token savings, compression stats, cache hit/miss ratio |
| `stm_proxy_select_chunks` | `key`, `sections[]` | Retrieve sections from a selective/hybrid TOC response |
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

## Observability

### Metrics

Token savings and compression efficiency tracked per server and tool:

```
STM Proxy Stats
===============
Total calls:     247
Original chars:  1,234,567
Compressed:      345,678
Savings:         72.0%
Cache hits:      89
Cache misses:    158

By server:
  filesystem: 142 calls, 800K → 200K chars (75.0% saved)
  github: 105 calls, 434K → 145K chars (66.6% saved)
```

Metrics persisted to SQLite (`~/.memtomem/proxy_metrics.db`, max 10K entries).

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
| `~/.memtomem/proxy_index/*.md` | Auto-indexed responses | auto-index pipeline |

---

## Testing

```bash
# Run STM tests
uv run pytest packages/memtomem-stm/tests/ -v

# Run a specific test file
uv run pytest packages/memtomem-stm/tests/test_compression.py -v
```

122 unit tests covering:

| Test file | Coverage |
|-----------|----------|
| `test_circuit_breaker.py` | State machine transitions (closed/open/half-open) |
| `test_compression.py` | All 5 compression strategies (noop/truncate/selective/hybrid/field-extract) |
| `test_relevance_gate.py` | Exclusions, write-tool heuristic, rate limit, cooldown, Jaccard similarity |
| `test_context_extractor.py` | Query templates, heuristic extraction, identifier detection |
| `test_feedback.py` | FeedbackStore, FeedbackTracker, AutoTuner feedback loop |
| `test_proxy_cache.py` | TTL expiration, eviction, clear, key generation |
| `test_cleaning.py` | HTML stripping, deduplication, link flood collapse |
| `test_surfacing_cache.py` | In-memory TTL cache, eviction, empty list caching |

## License

Apache-2.0
