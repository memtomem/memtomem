# LLM Providers

memtomem can optionally use an LLM to enhance several features. LLM is **disabled by default** — all core functionality (search, indexing, tagging) works without it.

## Quick Start

The simplest path — local Ollama:

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=ollama
# model defaults to gemma4:e2b, base_url to localhost:11434
```

## Supported Providers

### Ollama (local, default)

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=ollama
export MEMTOMEM_LLM__MODEL=gemma4:e2b      # default when empty
export MEMTOMEM_LLM__BASE_URL=http://localhost:11434
```

### OpenAI (cloud)

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=openai
export MEMTOMEM_LLM__MODEL=gpt-4.1-mini    # default when empty
export MEMTOMEM_LLM__API_KEY=sk-...
```

### Anthropic (cloud)

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=anthropic
export MEMTOMEM_LLM__MODEL=claude-haiku-4-5-20251001
export MEMTOMEM_LLM__API_KEY=sk-ant-...
```

## OpenAI-Compatible Endpoints

The `openai` provider works with **any server** that implements `/v1/chat/completions` — not just OpenAI's cloud API. Set `PROVIDER=openai` and point `BASE_URL` at your server.

### LM Studio

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=openai
export MEMTOMEM_LLM__BASE_URL=http://localhost:1234
export MEMTOMEM_LLM__MODEL=<loaded-model-name>
# No API key needed for local LM Studio
```

### vLLM

```bash
# Start vLLM: vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000

export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=openai
export MEMTOMEM_LLM__BASE_URL=http://localhost:8000
export MEMTOMEM_LLM__MODEL=meta-llama/Llama-3.1-8B-Instruct
```

### OpenRouter

```bash
export MEMTOMEM_LLM__ENABLED=true
export MEMTOMEM_LLM__PROVIDER=openai
export MEMTOMEM_LLM__BASE_URL=https://openrouter.ai/api
export MEMTOMEM_LLM__API_KEY=sk-or-v1-...
export MEMTOMEM_LLM__MODEL=meta-llama/llama-3.1-8b-instruct
```

### Other Compatible Servers

Any server implementing `/v1/chat/completions` works:

- **text-generation-webui**: enable `--api`, `BASE_URL=http://localhost:5000`
- **LocalAI**: `BASE_URL=http://localhost:8080`

### MCP Config Example

```json
{
  "mcpServers": {
    "memtomem": {
      "command": "uvx",
      "args": ["--from", "memtomem", "memtomem-server"],
      "env": {
        "MEMTOMEM_LLM__ENABLED": "true",
        "MEMTOMEM_LLM__PROVIDER": "openai",
        "MEMTOMEM_LLM__BASE_URL": "http://localhost:1234",
        "MEMTOMEM_LLM__MODEL": "loaded-model-name"
      }
    }
  }
}
```

## What LLM Powers

| Feature | LLM enabled | LLM disabled (default) |
|---------|-------------|------------------------|
| Query expansion (`strategy="llm"`) | LLM synonym generation | Disabled — original query used |
| Entity extraction (`mem_entity_scan`) | LLM structured extraction | Regex + pattern matching |
| Auto-tagging (`mem_auto_tag`) | LLM semantic tagging | Keyword frequency heuristic |
| Consolidation (`auto_consolidate`) | LLM summary | Bullet-point extraction |

All features gracefully degrade: if LLM is enabled but a call fails, the heuristic fallback runs automatically.

## Configuration Reference

See [`configuration.md#llm`](configuration.md#llm) for the complete `MEMTOMEM_LLM__*` environment variable reference. Provider defaults when `MODEL` is empty: ollama → `gemma4:e2b`, openai → `gpt-4.1-mini`, anthropic → `claude-haiku-4-5-20251001`.

## Troubleshooting

- **"Cannot connect to …"** — check `BASE_URL` and that the server is running
- **"Authentication failed"** — verify `API_KEY`
- **"Model not found" (Ollama)** — run `ollama pull <model>`
- **Consolidation falls back to heuristic** — LLM error is logged; check provider health
- **Query expansion adds latency** — expansion has a 3-second hard timeout; consider switching to `strategy="tags"` if LLM is consistently slow
