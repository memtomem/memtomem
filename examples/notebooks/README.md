# memtomem — example notebooks

Runnable Jupyter notebooks that walk through memtomem's Python API. Each is
self-contained, runs against a throwaway temp directory, and never touches your
real `~/.memtomem/`. They use local **ONNX** embeddings — there is no embedding
server to start; the model downloads once on first use.

| # | Notebook | What it covers | Time |
|---|----------|----------------|------|
| 01 | [`01_hello_memory.ipynb`](./01_hello_memory.ipynb) | Initialise components, add a few memories, run your first hybrid search. The minimum viable tour. | ~5 min |
| 02 | [`02_index_and_filter.ipynb`](./02_index_and_filter.ipynb) | Bulk-index a directory; scope searches with `source` / `tag` / `namespace` filters; inspect BM25-vs-dense via `rrf_weights`; switch to the `kiwipiepy` tokenizer for Korean. | ~15 min |
| 03 | [`03_agent_memory_patterns.ipynb`](./03_agent_memory_patterns.ipynb) | Agent-style memory: sessions, events, scratch (working memory), and time-based recall. | ~10 min |
| 04 | [`04_multi_agent_mcp_memory.ipynb`](./04_multi_agent_mcp_memory.ipynb) | Multi-agent memory coordination: registration, session management, private-vs-shared namespaces, and LangGraph flow orchestration. | ~15 min |

## Setup

```bash
# ONNX is enough for all three; add ',korean' for notebook 02's Korean section.
uv pip install "memtomem[onnx]" jupyter ipykernel
uv run jupyter lab examples/notebooks/
```

Each notebook checks that its embedding backend is importable in the first cell
and stops early with a clear message if a required extra is missing.

More scenario notebooks (search tuning, LangGraph integration, lifecycle,
embedding-provider comparison, LLM features) live in the private
`memtomem/memtomem-docs` repo — the public set is kept small and beginner-first.
