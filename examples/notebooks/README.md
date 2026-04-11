# memtomem Interactive Notebooks

Scenario-based Jupyter notebooks that walk through memtomem's Python API. Each
notebook is self-contained, runs against a throwaway temp directory, and never
touches your real `~/.memtomem/` setup.

These notebooks complement the MCP-client-focused
[`docs/guides/hands-on-tutorial.md`](../../docs/guides/hands-on-tutorial.md).
Use the tutorial if you want to drive memtomem from Claude Code / Cursor /
Windsurf; use these notebooks if you want to use memtomem as a Python library
in a data-science, research, or agent-framework workflow.

## Prerequisites

1. **Python 3.12+**
2. **Ollama** running locally with the default embedding model:
   ```bash
   ollama serve
   ollama pull nomic-embed-text
   ```
3. **memtomem + jupyter**:
   ```bash
   # From PyPI
   uv pip install "memtomem[ollama]" jupyter ipykernel

   # Or from a source checkout
   uv pip install -e "packages/memtomem[all]" jupyter ipykernel
   ```
4. **(Notebook 02, Korean section only)** the `kiwipiepy` tokenizer:
   ```bash
   uv pip install "memtomem[korean]"
   ```
5. **(Notebook 05 only)** LangGraph:
   ```bash
   uv pip install langgraph
   ```

## Running the notebooks

```bash
uv run jupyter lab examples/notebooks/
```

Each notebook checks that Ollama is reachable in its first cell and stops
early with a clear error message if it is not.

## Notebook index

| #  | Notebook | Scenario | Time |
|----|----------|----------|------|
| 01 | [`01_hello_memory.ipynb`](01_hello_memory.ipynb) | Initialise components, add a handful of memories, run your first hybrid search. The minimum viable tour. | ~5 min |
| 02 | [`02_index_and_filter.ipynb`](02_index_and_filter.ipynb) | Index a directory of markdown notes, filter by source / tag / namespace, inspect BM25 vs dense scores, and switch to the `kiwipiepy` tokenizer for Korean content. | ~15 min |
| 03 | [`03_agent_memory_patterns.ipynb`](03_agent_memory_patterns.ipynb) | Build episodic + working memory for an agent: sessions, events, scratchpad, and time-based recall. | ~10 min |
| 04 | [`04_search_tuning.ipynb`](04_search_tuning.ipynb) | Compare the same query under different search configurations — BM25-only, dense-only, balanced, with and without the context window. | ~15 min |
| 05 | [`05_langgraph_integration.ipynb`](05_langgraph_integration.ipynb) | Wire `MemtomemStore` into a minimal two-node LangGraph agent that searches memtomem and writes findings back. | ~20 min |
| 06 | [`06_lifecycle.ipynb`](06_lifecycle.ipynb) | The full memory lifecycle: hash-based incremental re-index on edit, surgical chunk delete, orphan cleanup after a file is removed, and `force=True` full re-embed. | ~10 min |

## How memories are stored

The notebooks deliberately exercise two different storage paths, and it
helps to know which is which when you are reading the cells:

**1. File-backed memories (notebooks 01, 02, 04, 05, 06 — most "document"
style memory).** The notebook writes a real `.md` file into the temp
`notes/` directory and then calls `index_engine.index_file(path)` (or
`index_path(dir)`). The chunker reads the file, splits it into chunks,
and the embedder stores those chunks in SQLite. This is exactly the same
path the MCP server takes when a file lands in a configured memory
directory. `MemtomemStore.add()` in notebook 05 also goes through this
path — it appends to a dated markdown file under the hood and then calls
`index_file`.

**2. DB-only memories (notebook 03 — sessions, events, scratch).** These
are agent working-memory primitives that do not correspond to any file.
The notebook calls `storage.create_session()`, `add_session_event()`,
`scratch_set()`, etc., and the data lands directly in dedicated SQLite
tables (`sessions`, `session_events`, `scratch`). There is no markdown
file to read or edit afterwards — the DB row *is* the memory.

Either way, everything lives inside a single temp directory:

```
/var/folders/.../tmpXXXXXXXX/
├── memory.db        ← chunks + embeddings + sessions + events + scratch
└── notes/           ← the real markdown files (file-backed memories only)
    ├── deployment.md
    └── retrieval.md
```

Deletes work the same way, split across the two paths. Notebook 06 walks
through the file-backed side end-to-end: edit a file → watch the
hash-diff re-index (`indexed` / `skipped` / `deleted` counts),
`storage.delete_chunks([uuid])` for a single surgical removal,
`storage.get_all_source_files()` → filesystem diff → `delete_by_source()`
to clean up orphans after a file is removed from disk, and `force=True`
for a full rebuild after (for example) swapping embedding models. In a
running MCP server the `FileWatcher` automates the edit and delete
paths; the notebook calls them explicitly so every step is deterministic.

## How these notebooks stay safe

Every notebook follows the same pattern from
[`packages/memtomem/tests/conftest.py`](../../packages/memtomem/tests/conftest.py):

1. Create a `tempfile.TemporaryDirectory()` for both the SQLite database and
   the memory directory.
2. Override `MEMTOMEM_STORAGE__SQLITE_PATH` and `MEMTOMEM_INDEXING__MEMORY_DIRS`
   via environment variables.
3. Monkey-patch `memtomem.config.load_config_overrides` to a no-op so the
   user's real `~/.memtomem/config.json` cannot leak into the notebook's
   configuration.
4. Close components and clean up the temp directory in the final cell.

This means you can run the notebooks as many times as you like without any
impact on an existing memtomem installation on the same machine.

## Next steps

- The [User Guide](../../docs/guides/user-guide.md) covers the full MCP tool
  surface.
- The [Agent Memory Guide](../../docs/guides/agent-memory-guide.md) digs
  deeper into episodic / working / procedural memory patterns.
- [`docs/guides/integrations/langgraph.md`](../../docs/guides/integrations/langgraph.md)
  is the prose companion to notebook 05.
