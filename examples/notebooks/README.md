# memtomem — example notebooks

One quick-start notebook is kept here as a runnable demo of the Python API:

- [`01_hello_memory.ipynb`](./01_hello_memory.ipynb) — install memtomem,
  index a folder, run a hybrid search, add a memory. ~5 minutes end-to-end.

Run it with:

```bash
uv pip install "memtomem[ollama]" jupyter ipykernel
uv run jupyter lab examples/notebooks/01_hello_memory.ipynb
```

The other scenario notebooks (indexing & filters, agent memory patterns,
search tuning, LangGraph integration, lifecycle, embedding providers, LLM
features) live in the private `memtomem/memtomem-docs` repo under
`memtomem/examples/notebooks/`. They were moved out of the public repo to
keep the beginner surface small while still providing a working quick-start.
