# langchain-memtomem

A [LangGraph](https://github.com/langchain-ai/langgraph) `BaseStore` whose
`search(query=...)` runs the [memtomem](https://github.com/memtomem/memtomem)
search pipeline — query expansion, BM25 and dense fusion, optional
cross-encoder rerank, time decay, MMR, entity boost — over one Markdown
projection per item, in an index the store owns.

- **Each item is a file a person can open and diff.** A JSON record is the
  source of truth; an indexed item also has a Markdown projection beside it.
- **The index is the store's.** It lives under the root you choose, built
  from a configuration the store assembles itself. Your own memtomem
  (`~/.memtomem`, `config.json`, `config.d`, `MEMTOMEM_*`) is never read for
  it and never written to.
- **The store is the application's memory**, not the developer's: its
  namespaces are your users and threads. If you want to read it from your
  own memtomem, add `<root>/projections` to `indexing.read_only_memory_dirs`
  in your personal configuration.

## Install

> **Not on PyPI yet.** The first release waits for the memtomem release that
> carries the names this package imports. Until then, install from a checkout
> of the memtomem repository: `uv pip install -e packages/memtomem -e
> packages/langchain-memtomem`.

```bash
pip install langchain-memtomem
```

Requires Python 3.12+, `langgraph>=1.0` and the memtomem release that carries
the ADR-0042 public names (the package refuses to import against an older
memtomem). Embedding is off by default (`embedding.provider = "none"`,
BM25 only, no model download); pass an `embedding` section to turn it on.

## Use

```python
from langchain_memtomem import MemtomemPipelineStore

store = await MemtomemPipelineStore.open("/var/app/memory")

await store.aput(("users", "alice"), "pref-1", {"text": "prefers window seats", "kind": "pref"})
await store.aput(("users", "alice"), "pref-2", {"text": "allergic to peanuts"}, index=False)

hits = await store.asearch(("users",), query="seat preference", filter={"kind": "pref"}, limit=5)
for item in hits:
    print(item.namespace, item.key, item.score, item.value)

await store.adelete(("users", "alice"), "pref-1")
await store.aclose()
```

`get`, `list_namespaces` and a query-less `search` read the records only.
`search(query=...)` ranks projections through the pipeline and returns whole
items, paginated over items. `index=False` stores an item without a
projection; it is listed and filtered, never ranked. `async with
MemtomemPipelineStore(root) as store:` opens on entry and closes on exit. A synchronous
`store.put(...)` / `store.search(...)` works outside a running event loop.

With an embedder and a reranker:

```python
store = await MemtomemPipelineStore.open(
    "/var/app/memory",
    config={
        "embedding": {"provider": "onnx", "model": "multilingual-e5-small"},
        "rerank": {"enabled": True},
        "search": {"cache_ttl": 5.0},
    },
    max_chunks_per_search=400,
)
```

`config` takes the sections of memtomem's `Mem2MemConfig` you want to set
(`embedding`, `search`, `rerank`, `chunking`, …). `storage` and `indexing`
belong to the store and are refused.

## What to know

- **Recall is bounded.** A query search reads at most `max_chunks_per_search`
  pipeline chunks (default 200), widening its window until it has
  `offset + limit` items or hits that bound; `rerank.max_pool` does not lower it.
- **Read-after-write.** The instance that wrote sees its write at once.
  Another store instance over the same root sees it after its
  `search.cache_ttl` (30 s by default), or at once if a cached hit disagrees
  with the files.
- **Two instances over one root** — two processes, or two stores in one —
  serialise per item on memtomem's own file lock. An interrupted write is
  repaired when the store is next opened, or when a query hits the item.
- **Project tiers.** If the root is inside a directory registered in your
  memtomem as a `project_shared` tier (git-tracked), opening it requires
  `confirm_project_shared=True`, and `force_unsafe` is refused there. The
  project registry is the one thing the store reads from your personal
  memtomem configuration.
- **Secrets.** Every write runs memtomem's redaction guard over the record
  and over the projection text before anything is written. A hit refuses the
  write; `force_unsafe=True` on the store lets it through, except in a
  `project_shared` tier.
- **Names.** Each namespace segment and the key encode to at most 200
  characters on disk (up to 124 bytes of UTF-8 each), and a whole namespace
  to at most 256; a longer one is refused at `put`. The encoding is
  single-case, so names that differ only in case stay apart on macOS and
  Windows too.
- **TTLs** are not supported in this version: an explicit `ttl=` raises
  `NotImplementedError` (LangGraph's own check, since `supports_ttl` is
  `False`).

The full protocol — write order, repair table, interruption matrix, search
widening, measurements — is in [`docs/protocol.md`](docs/protocol.md).

## License

Apache-2.0, the same as memtomem.
