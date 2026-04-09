# Embedding Providers

memtomem supports two embedding providers out of the box: **Ollama** (local, default) and **OpenAI** (cloud). The provider, model, and vector dimension must always be set together — dimension is **not auto-detected**, and a mismatch will cause indexing errors.

## Supported Models

| Model | Provider | Dimension | Best for |
|-------|----------|-----------|----------|
| `nomic-embed-text` (default) | Ollama | 768 | General English, lightweight, no GPU |
| `bge-m3` | Ollama | 1024 | Multilingual (KR/EN/JP/CN), higher accuracy |
| `text-embedding-3-small` | OpenAI | 1536 | Cloud-based, no GPU needed |
| `text-embedding-3-large` | OpenAI | 3072 | Best accuracy |

You can switch models via `mm init` (interactive wizard) or `mm embedding-reset` (handles the dimension migration safely).

## Ollama (default, local)

```bash
# Pull the default model (one-time, ~270MB)
ollama pull nomic-embed-text

# These are the defaults — no env vars needed
MEMTOMEM_EMBEDDING__PROVIDER=ollama
MEMTOMEM_EMBEDDING__MODEL=nomic-embed-text
MEMTOMEM_EMBEDDING__DIMENSION=768
MEMTOMEM_EMBEDDING__BASE_URL=http://localhost:11434
```

For multilingual content, switch to `bge-m3`:

```bash
ollama pull bge-m3

export MEMTOMEM_EMBEDDING__MODEL=bge-m3
export MEMTOMEM_EMBEDDING__DIMENSION=1024
```

## OpenAI (cloud)

```bash
export MEMTOMEM_EMBEDDING__PROVIDER=openai
export MEMTOMEM_EMBEDDING__MODEL=text-embedding-3-small
export MEMTOMEM_EMBEDDING__DIMENSION=1536
export MEMTOMEM_EMBEDDING__API_KEY=sk-...
```

For higher accuracy:

```bash
export MEMTOMEM_EMBEDDING__MODEL=text-embedding-3-large
export MEMTOMEM_EMBEDDING__DIMENSION=3072
```

## Switching Models on an Existing Index

If you switch the embedding model after indexing files, the existing vectors won't match the new model's vector space. Use `mm embedding-reset` to detect and resolve the mismatch:

```bash
mm embedding-reset                  # Show current vs configured model
mm embedding-reset --mode apply-current   # Drop old vectors, prepare for re-index
mm index ~/notes                    # Re-embed with the new model
```

Or non-destructively, point the runtime back at the model that was used to build the index:

```bash
mm embedding-reset --mode revert-to-stored
```

The same operation is available as the `mem_embedding_reset` MCP tool.

## Tuning Throughput

| Variable | Default | When to change |
|----------|---------|----------------|
| `MEMTOMEM_EMBEDDING__BATCH_SIZE` | `64` | Lower for memory-constrained Ollama setups; higher for cloud APIs |
| `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` | `4` | Lower if you're hitting rate limits; higher to saturate fast endpoints |
