# LangGraph / LangChain Integration

Use memtomem as a memory store in LangGraph agents without MCP.

## Installation

```bash
pip install memtomem
# or
uv pip install memtomem
```

Ensure an embedding provider is running:

```bash
ollama pull nomic-embed-text
```

---

## Quick Start

```python
from memtomem.integrations.langgraph import MemtomemStore

async with MemtomemStore() as store:
    # Add a memory
    result = await store.add("Deploy uses blue-green strategy", tags=["ops"])
    print(result)  # {"file": "...", "indexed_chunks": 1}

    # Search
    results = await store.search("deployment strategy")
    for r in results:
        print(f"{r['score']:.3f} {r['content'][:80]}")
```

---

## API Reference

### MemtomemStore

```python
class MemtomemStore:
    def __init__(self, config_overrides: dict | None = None): ...
```

**Config overrides** let you customize without environment variables:

```python
store = MemtomemStore(config_overrides={
    "storage": {"sqlite_path": "/tmp/agent.db"},
    "embedding": {"model": "bge-m3", "dimension": 1024},
})
```

### Search

```python
results = await store.search(
    query="deployment",
    top_k=10,
    namespace="ops",
    source_filter="*.md",
    tag_filter="important",
    bm25_weight=1.5,      # boost keyword matches
    dense_weight=1.0,      # semantic weight
)
# Returns: [{"id", "content", "score", "source", "tags", "namespace", "rank"}]
```

### CRUD

```python
# Add
await store.add(content, title="...", tags=["a"], namespace="ops", template="adr")

# Get by ID
chunk = await store.get(chunk_id)

# Delete
await store.delete(chunk_id)
```

### Sessions (Episodic Memory)

```python
session_id = await store.start_session(agent_id="researcher", namespace="project-x")

# Log events during the session
await store.log_event("query", "searched for deployment docs")
await store.log_event("add", "saved new finding")

# End session with summary
stats = await store.end_session(summary="Researched deployment patterns")
# {"session_id": "...", "events": 2, "event_counts": {"query": 1, "add": 1}}
```

### Working Memory (Scratch)

```python
# Store temporary data
await store.scratch_set("current_task", "analyze logs", ttl_minutes=60)

# Retrieve
value = await store.scratch_get("current_task")  # "analyze logs"

# List all
entries = await store.scratch_list()
```

### Index

```python
stats = await store.index(path="~/notes", recursive=True, namespace="notes")
# {"total_files": 12, "indexed_chunks": 45, "duration_ms": 1200}
```

---

## LangGraph StateGraph Example

```python
from langgraph.graph import StateGraph, END
from memtomem.integrations.langgraph import MemtomemStore

store = MemtomemStore()

async def research_node(state):
    results = await store.search(state["query"], top_k=5)
    return {"context": results, "query": state["query"]}

async def save_node(state):
    if state.get("findings"):
        await store.add(state["findings"], tags=["research"])
    return state

graph = StateGraph(dict)
graph.add_node("research", research_node)
graph.add_node("save", save_node)
graph.add_edge("research", "save")
graph.add_edge("save", END)
graph.set_entry_point("research")

app = graph.compile()
result = await app.ainvoke({"query": "deployment best practices"})
```

---

## Multi-Agent Pattern

Each agent gets its own namespace for memory isolation:

```python
async def agent_a_node(state):
    async with MemtomemStore(config_overrides={
        "indexing": {"memory_dirs": ["~/.memtomem/memories"]},
    }) as store:
        await store.start_session(agent_id="agent-a", namespace="agent/a")
        results = await store.search(state["query"], namespace="agent/a")
        # Also search shared knowledge
        shared = await store.search(state["query"], namespace="shared")
        return {"context": results + shared}
```

---

## Configuration

The adapter respects the same environment variables as the MCP server:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_EMBEDDING__PROVIDER` | `ollama` | Embedding provider |
| `MEMTOMEM_EMBEDDING__MODEL` | `nomic-embed-text` | Model name |
| `MEMTOMEM_STORAGE__SQLITE_PATH` | `~/.memtomem/memtomem.db` | Database path |
| `MEMTOMEM_INDEXING__MEMORY_DIRS` | `~/.memtomem/memories` | Memory directories |

Or pass `config_overrides` to the constructor for programmatic control.

---

## Next Steps

- [Agent Memory Guide](../agent-memory-guide.md) — Episodic, working, procedural memory concepts
- [User Guide](../user-guide.md) — Complete MCP tool reference
