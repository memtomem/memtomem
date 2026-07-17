# Embedding Providers

memtomem supports three embedding providers: **ONNX** (local, no server), **Ollama** (local server), and **OpenAI** (cloud). Embedding is optional — memtomem works with **BM25-only mode** (provider `none`, the default) for keyword search without any embedding setup. Embeddings add **dense** (semantic) search — finding notes by *meaning* rather than exact keywords — which memtomem fuses with BM25 keyword results into hybrid search. The provider, model, and vector dimension must always be set together — dimension is **not auto-detected**, and a mismatch will cause indexing errors.

## Which provider should I choose?

Embeddings are **opt-in** — out of the box memtomem runs keyword-only search with no model and no server. Pick a provider by what you need:

- **Stay on the default (`none`)** — BM25 keyword search, no model download, no server. Works immediately; `mm init` recommends it for a first run.
- **Want semantic ("by meaning") search?** → **ONNX** with `all-MiniLM-L6-v2`. Runs locally in-process, ~90 MB downloaded automatically on first use — the simplest on-ramp to dense search, and what `mm init` suggests once you opt into embeddings.
- **Already run an Ollama server?** → **Ollama** with `nomic-embed-text`, to reuse that daemon instead of embedding in-process.
- **Want cloud accuracy with no local compute?** → **OpenAI** with `text-embedding-3-small` (needs an API key; text is sent to OpenAI).
- **Multilingual (Korean / Chinese / Japanese)?** → **`bge-m3`** on ONNX or Ollama (1024-dim; ~2.3 GB on ONNX).

Switch any time with `mm init` (interactive) or `mm embedding-reset` (handles the dimension migration safely). Whichever you pick, set the provider, model, and dimension together — choose a row from the table below.

## Supported Models

| Model | Provider | Dimension | Best for |
|-------|----------|-----------|----------|
| `all-MiniLM-L6-v2` | ONNX | 384 | Quick local dense search, tiny (~90 MB) |
| `bge-small-en-v1.5` | ONNX | 384 | Better English accuracy (~67 MB) |
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

The model is downloaded automatically on first use (~90 MB for all-MiniLM-L6-v2) and cached in `~/.memtomem/cache/fastembed/` (override with `MEMTOMEM_FASTEMBED_CACHE` or `FASTEMBED_CACHE_PATH`).

For multilingual content (Korean, Chinese, Japanese):

```bash
export MEMTOMEM_EMBEDDING__MODEL=bge-m3
export MEMTOMEM_EMBEDDING__DIMENSION=1024
```

> **Note:** `bge-m3` is ~2.3 GB on disk — a substantial download, and much larger than the Ollama models below. For lightweight English-only search, use `all-MiniLM-L6-v2` or `bge-small-en-v1.5`.

Local ONNX inference uses memory-safe batching, sequence, and allocator defaults:

```bash
export MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE=8
export MEMTOMEM_EMBEDDING__MAX_SEQUENCE_TOKENS=1024
export MEMTOMEM_EMBEDDING__ONNX_CPU_MEM_ARENA=false
```

The sequence cap is enforced by the model's actual tokenizer. When an input is
longer, the dense vector represents its prefix while stored content and BM25
still cover the complete chunk. Set the cap to `0` to restore the model's own
limit. Changing the cap requires a restart and a force-reindex of existing ONNX
content to keep vector generation consistent.

The CPU memory arena is disabled so ONNX Runtime returns peak allocations after
indexing instead of retaining them in the process RSS for reuse. This allocator
setting does not change vectors or require re-indexing, but it is fixed when the
model session loads, so changing it requires a restart. Set it to `true` only as
an explicit compatibility or throughput escape hatch.

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

If you switch the embedding model after indexing, run `mm embedding-reset` to detect and resolve the dimension mismatch. **Stop any running `mm web` / MCP server / `mm index` first** — running `embedding-reset` against a live DB can leave a mix of old- and new-model vectors silently coexisting (issue #707). See [`configuration.md#reset-flow`](configuration.md#reset-flow) for the full two-mode flow (`apply-current` vs `revert-to-stored`), the `mem_status` warning schema, and the equivalent `mem_embedding_reset` MCP tool.

## Troubleshooting

- **"Request URL is missing an 'http://' or 'https://' protocol"** — your config has `embedding.provider` set to `ollama` but `embedding.base_url` is empty. Upgrade to the latest version (which defaults to `http://localhost:11434`) or add `"base_url": "http://localhost:11434"` to the `embedding` section of `~/.memtomem/config.json`.
- **"Cannot connect to Ollama"** — verify `ollama serve` is running.
- **"Model not found"** — run `ollama pull <model>` to download it.
- **Embedding model or ONNX sequence-policy mismatch** — dense search safely
  falls back to BM25 and indexing is blocked. Use
  `mm embedding-reset --mode apply-current`, then
  `mm index --force <memory_dir>`.

## Tuning Throughput

See [`configuration.md#embedding`](configuration.md#embedding) for the provider-specific controls. `MEMTOMEM_EMBEDDING__BATCH_SIZE` and `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` tune remote providers; `MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE` and `MEMTOMEM_EMBEDDING__MAX_SEQUENCE_TOKENS` bound local FastEmbed activation memory, while `MEMTOMEM_EMBEDDING__ONNX_CPU_MEM_ARENA=false` prevents peak allocations from remaining cached in process RSS.
