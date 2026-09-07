# memtomem — example notebooks

Runnable Jupyter notebooks that walk through memtomem's Python API. Each is
self-contained and keeps its database, configuration, and sample memories in a
throwaway temp directory. Notebooks 01–04 use local **ONNX** embeddings — there is no
embedding server to start; model snapshots download once and are shared through
`~/.memtomem/cache/fastembed` by default. Set `MEMTOMEM_FASTEMBED_CACHE` to an
explicit temporary path as well if you need strict home-directory isolation.

| # | Notebook | What it covers | Time |
|---|----------|----------------|------|
| 01 | [`01_hello_memory.ipynb`](./01_hello_memory.ipynb) | Initialise components, add a few memories, run your first hybrid search. The minimum viable tour. | ~5 min |
| 02 | [`02_index_and_filter.ipynb`](./02_index_and_filter.ipynb) | Bulk-index a directory; scope searches with `source` / `tag` / `namespace` filters; inspect BM25-vs-dense via `rrf_weights`; switch to the `kiwipiepy` tokenizer for Korean. | ~15 min |
| 03 | [`03_agent_memory_patterns.ipynb`](./03_agent_memory_patterns.ipynb) | Agent-style memory: sessions, events, scratch (working memory), and time-based recall. | ~10 min |
| 04 | [`04_multi_agent_mcp_memory.ipynb`](./04_multi_agent_mcp_memory.ipynb) | Multi-agent memory coordination: registration, session management, private-vs-shared namespaces, and LangGraph flow orchestration. | ~15 min |
| 05 | [`05_langgraph_memory_basics.ipynb`](./05_langgraph_memory_basics.ipynb) | Korean beginner lab: thread state vs JSON-backed cross-thread memory, user namespaces, update and reopen. No model or API key. | ~15 min |
| 06 | [`06_langgraph_retrieval_memory.ipynb`](./06_langgraph_retrieval_memory.ipynb) | Korean beginner lab: Core BM25 retrieval → draft → explicit approval → Markdown save. Optional paid Responses API preview is off by default. | ~20 min |

## Start without a model (05–06)

Download either notebook; each is self-contained and includes setup, expected
outputs, exercises, hints, and cleanup. No private repository is required.

```bash
uv venv .venv
uv pip install --python .venv/bin/python "memtomem[langgraph]==0.5.0" jupyterlab ipykernel
uv run --python .venv/bin/python --no-project jupyter lab
```

Both labs use temporary homes and disable embeddings. 05 uses the LangGraph
BaseStore adapter's JSON records, not Core's SQLite index. 06 uses Core's
Markdown/SQLite path through `MemtomemStore`; namespaces are not authorization.
For a coding-agent demonstration instead, use the [retry-policy sample](../onboarding/retry-policy/).

Maintainers can install `nbclient ipykernel` and run
`python tools/check_beginner_notebooks.py --json` from the repository root.
This runs new kernels with Python socket connections blocked, without modifying
the notebooks' saved outputs. It does not test an actual AI client or paid API.

## Setup

```bash
# Base setup for notebooks 01-03; add ',korean' for notebook 02's Korean section.
uv pip install "memtomem[onnx]" jupyter ipykernel

# Notebook 04 additionally uses LangChain, LangGraph, and Jupyter's nested loop.
uv pip install langchain langgraph nest-asyncio

uv run jupyter lab examples/notebooks/
```

Notebooks 01–04 check that their embedding backend is importable in the first cell
and stops early with a clear message if a required extra is missing. Notebook 04
also checks its orchestration dependencies there.

More scenario notebooks (search tuning, LangGraph integration, lifecycle,
embedding-provider comparison, LLM features) live in the private
`memtomem/memtomem-docs` repo — the public set is kept small and beginner-first.
