# Embedding Providers

memtomem supports three embedding providers: **ONNX** (local, no server), **Ollama** (local server), and **OpenAI** (cloud). Embedding is optional — memtomem works with **BM25-only mode** (provider `none`, the default) for keyword search without any embedding setup. Embeddings add **dense** (semantic) search — finding notes by *meaning* rather than exact keywords — which memtomem fuses with BM25 keyword results into hybrid search. The provider, model, and vector dimension must be set together — dimension is **not auto-detected**, and a mismatch will cause indexing errors. The one exception is the ONNX default: `provider=onnx` with no model selects `multilingual-e5-small` and fills in its 384 dimensions.

## Which provider should I choose?

Embeddings are **opt-in** — out of the box memtomem runs keyword-only search with no model and no server. Pick a provider by what you need:

- **Stay on the default (`none`)** — BM25 keyword search, no model download, no server. Works immediately, and is the fastest no-download first proof — the `mm init` **Minimal** preset selects it.
- **Want semantic ("by meaning") search?** → **ONNX** with `multilingual-e5-small`. Runs locally in-process on CPU, ~490 MB downloaded automatically on first use, and covers English plus Korean / Chinese / Japanese. It is the ONNX default, the model both the English and Korean-optimized `mm init` presets use, and the wizard's default choice.
- **Want the smallest English-only download?** → **ONNX** with `all-MiniLM-L6-v2` (~90 MB) or `bge-small-en-v1.5` (~67 MB).
- **Already run an Ollama server?** → **Ollama** with `nomic-embed-text`, to reuse that daemon instead of embedding in-process.
- **Want cloud accuracy with no local compute?** → **OpenAI** with `text-embedding-3-small` (needs an API key; text is sent to OpenAI).
- **Multilingual with a larger model?** → **`bge-m3`** on ONNX or Ollama (1024-dim; ~2.3 GB on ONNX).

Switch any time with `mm init` (interactive) or `mm embedding-reset` (handles the dimension migration safely). Whichever you pick, set the provider, model, and dimension together — choose a row from the table below.

## Supported Models

| Model | Provider | Dimension | Best for |
|-------|----------|-----------|----------|
| `multilingual-e5-small` | ONNX | 384 | ONNX default; multilingual (KR/EN/JP/CN) on CPU (~490 MB) |
| `all-MiniLM-L6-v2` | ONNX | 384 | Quick local English dense search, tiny (~90 MB) |
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
export MEMTOMEM_EMBEDDING__MODEL=multilingual-e5-small
export MEMTOMEM_EMBEDDING__DIMENSION=384
```

Or run `mm init` and pick the English or Korean-optimized preset, or select "Local ONNX" in the Advanced wizard.

The model is downloaded automatically on first use (~490 MB for multilingual-e5-small) and cached in `~/.memtomem/cache/fastembed/` (override with `MEMTOMEM_FASTEMBED_CACHE` or `FASTEMBED_CACHE_PATH`).

The download goes through `huggingface_hub`. memtomem runs those E5 calls with Hub telemetry
off, even if `HF_HUB_DISABLE_TELEMETRY`, `DISABLE_TELEMETRY` or `DO_NOT_TRACK` is set to a false
value. Unless another component in the same process changes that setting mid-download, they send
no `agent/<id>` user-agent tag and skip the agent-registry fetch that writes
`HF_HOME/.agent_harnesses.json`. `MEMTOMEM_FASTEMBED_CACHE` moves the model files only:
`huggingface_hub` still keeps its own files, such as its xet logs, under `HF_HOME`
(`$XDG_CACHE_HOME/huggingface/`, else `~/.cache/huggingface/`, by default) or `HF_XET_CACHE`.
Set those before starting memtomem to put them elsewhere.

`multilingual-e5-small` fixes two values: the dimension must be `384` and
`MAX_SEQUENCE_TOKENS` must be `512` (so `0` is rejected). It also fills in
two inference threads, an ONNX batch of four, and a smaller chunk budget
when you leave those unset; explicit values for threads and batch size are
kept. See [Configuration → CPU ONNX profiles](configuration.md#cpu-onnx-profiles)
for the full profile.

For the larger multilingual model:

```bash
export MEMTOMEM_EMBEDDING__MODEL=bge-m3
export MEMTOMEM_EMBEDDING__DIMENSION=1024
```

> **Note:** `bge-m3` is ~2.3 GB on disk — a substantial download, and much larger than the Ollama models below. For lightweight English-only search, use `all-MiniLM-L6-v2` or `bge-small-en-v1.5`.

For models other than `multilingual-e5-small`, local ONNX inference uses these
memory-safe batching, sequence, and allocator defaults:

```bash
export MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE=8
export MEMTOMEM_EMBEDDING__MAX_SEQUENCE_TOKENS=1024
export MEMTOMEM_EMBEDDING__ONNX_CPU_MEM_ARENA=false
```

The sequence cap is enforced by the model's actual tokenizer. For these
models, when an input is longer, the dense vector represents its prefix while
stored content and BM25 still cover the complete chunk. Set the cap to `0` to
restore the model's own limit. Changing the cap requires a restart and a
force-reindex of existing ONNX content to keep vector generation consistent.

`multilingual-e5-small` does not truncate passages: its chunker keeps
indexed chunks inside 512 tokens, and embedding a passage that still exceeds
the limit fails instead of producing a vector for its prefix. Query-side
embeddings, such as search queries, are truncated with a warning.

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

If you switch the embedding model after indexing, run `mm embedding-reset` to detect and resolve the dimension mismatch. **Stop any running `mm web` / MCP server / `mm index` first** — running `embedding-reset` against a live DB can leave a mix of old- and new-model vectors silently coexisting (issue #707). See [`configuration.md#reset-flow`](configuration.md#reset-flow) for the reset and recovery instructions and the `mem_status` warning schema. CLI `revert-to-stored` only shows stored settings and recovery instructions; it does not repair the mismatch or change a running server. The MCP `mem_embedding_reset(mode="revert_to_stored")` call changes the runtime of the server handling it without persisting settings. CLI reporting modes do not write `config.json`, but still initialize storage and may create or initialize the database.

## Troubleshooting

- **"Request URL is missing an 'http://' or 'https://' protocol"** — your config has `embedding.provider` set to `ollama` but `embedding.base_url` is empty. Upgrade to the latest version (which defaults to `http://localhost:11434`) or add `"base_url": "http://localhost:11434"` to the `embedding` section of `~/.memtomem/config.json`.
- **"Cannot connect to Ollama"** — verify `ollama serve` is running.
- **"Model not found"** — run `ollama pull <model>` to download it.
- **Embedding model or ONNX sequence-policy mismatch** — dense search safely
  falls back to BM25 and indexing is blocked. Use
  `mm embedding-reset --mode apply-current`, then
  `mm index --force <memory_dir>`. `--force` re-embeds only — each file keeps
  the namespace its chunks are stored under, agent session namespaces
  included. `mem_index(force=true)` is equally safe, including inside an agent
  session: the session namespace binds only files the index has never seen
  (see [`configuration.md#reset-flow`](configuration.md#reset-flow)). A
  namespace you pass explicitly still wins.

## Tuning Throughput

See [`configuration.md#embedding`](configuration.md#embedding) for the provider-specific controls. `MEMTOMEM_EMBEDDING__BATCH_SIZE` and `MEMTOMEM_EMBEDDING__MAX_CONCURRENT_BATCHES` tune remote providers; `MEMTOMEM_EMBEDDING__ONNX_BATCH_SIZE` and `MEMTOMEM_EMBEDDING__MAX_SEQUENCE_TOKENS` bound local FastEmbed activation memory, while `MEMTOMEM_EMBEDDING__ONNX_CPU_MEM_ARENA=false` prevents peak allocations from remaining cached in process RSS.
