# Embedding Providers

memtomem supports three embedding providers: **ONNX** (local, no server), **Ollama** (local server), and **OpenAI** (cloud). Embedding is optional — memtomem works with **BM25-only mode** (provider `none`, the default) for keyword search without any embedding setup. The provider, model, and vector dimension must always be set together — dimension is **not auto-detected**, and a mismatch will cause indexing errors.

## Supported Models

| Model | Provider | Dimension | Best for |
|-------|----------|-----------|----------|
| `all-MiniLM-L6-v2` | ONNX | 384 | Quick local dense search, tiny (~22 MB) |
| `bge-small-en-v1.5` | ONNX | 384 | Better English accuracy (~33 MB) |
| `bge-m3` | ONNX / Ollama | 1024 | Multilingual (KR/EN/JP/CN), highest accuracy |
| `nomic-embed-text` | Ollama | 768 | General English, lightweight, no GPU |
| `text-embedding-3-small` | OpenAI | 1536 | Cloud-based, no GPU needed |
| `text-embedding-3-large` | OpenAI | 3072 | Best accuracy |

You can switch models via `mm init` (interactive wizard) or `mm embedding-reset` (handles the dimension migration safely).

## ONNX (local, no server)

Install the optional dependency:

```bash
pip install memtomem[onnx]
# or with uv:
uv pip install memtomem[onnx]
```

Configure via environment variables:

```bash
export MEMTOMEM_EMBEDDING__PROVIDER=onnx
export MEMTOMEM_EMBEDDING__MODEL=all-MiniLM-L6-v2
export MEMTOMEM_EMBEDDING__DIMENSION=384
```

Or run `mm init` and select "Local ONNX".

The model is downloaded automatically on first use (~22 MB for all-MiniLM-L6-v2) and cached in `~/.cache/fastembed/`.

For multilingual content (Korean, Chinese, Japanese):

```bash
export MEMTOMEM_EMBEDDING__MODEL=bge-m3
export MEMTOMEM_EMBEDDING__DIMENSION=1024
```

> **Note:** `bge-m3` is ~1.2 GB — similar in size to Ollama models. For lightweight English-only search, use `all-MiniLM-L6-v2` or `bge-small-en-v1.5`.

## Ollama (local server)

```bash
# Pull the default model (one-time, ~270MB)
ollama pull nomic-embed-text

# Minimal config — base_url defaults to http://localhost:11434
MEMTOMEM_EMBEDDING__PROVIDER=ollama
MEMTOMEM_EMBEDDING__MODEL=nomic-embed-text
MEMTOMEM_EMBEDDING__DIMENSION=768
```

> **`base_url` is optional.** When provider is `ollama` and `base_url` is empty or unset, it defaults to `http://localhost:11434`. Override only if Ollama runs on a different host or port.

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

## Troubleshooting

- **"Request URL is missing an 'http://' or 'https://' protocol"** — your config has `embedding.provider` set to `ollama` but `embedding.base_url` is empty. Upgrade to the latest version (which defaults to `http://localhost:11434`) or add `"base_url": "http://localhost:11434"` to the `embedding` section of `~/.memtomem/config.json`.
- **"Cannot connect to Ollama"** — verify `ollama serve` is running.
- **"Model not found"** — run `ollama pull <model>` to download it.
- **Dimension mismatch after model switch** — use `mm embedding-reset --mode apply-current` then re-index.

## Tuning Throughput

| Variable | Default | When to change |
|----------|---------|----------------|
| `MEMTOMEM_EMBEDDING__BATCH_SIZE` | `64` | Lower for memory-constrained Ollama setups; higher for cloud APIs |
| `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` | `4` | Lower if you're hitting rate limits; higher to saturate fast endpoints |
