# MemtomemHybridStore

`MemtomemHybridStore` is an optional LangGraph `BaseStore` implementation for
local agent development and retrieval evaluation. It persists JSON values and
search indexes in one explicitly selected SQLite database. It supports BM25,
exact dense retrieval, weighted reciprocal rank fusion, TTL, and search diagnostics.

This is a separate corpus. It does not search Core Markdown memories, open Core's
SQLite database, or migrate the older JSON-backed `MemtomemBaseStore` automatically.
Use `MemtomemStore` for the existing Core Markdown search pipeline.

## Quick start

Install `memtomem[langgraph]`. No embedding service is needed for BM25:

```python
from memtomem.integrations import MemtomemHybridStore

with MemtomemHybridStore("./agent-memory.sqlite") as store:
    store.put(("users", "alice"), "preferences", {
        "text": "Alice prefers Python examples",
        "category": "preference",
    })
    result = store.search_with_diagnostics(
        ("users", "alice"),
        query="Python",
        filter={"category": "preference"},
    )
    assert result["items"][0].key == "preferences"
    assert result["diagnostics"]["effective_mode"] == "bm25"
```

Standard `get`, `put`, `delete`, `search`, `list_namespaces`, and `batch` methods
have their LangGraph async counterparts. Use `async with` or `await store.aclose()`
in asynchronous applications. Pass the instance as `graph.compile(store=store)`.
Both `memtomem.integrations` and `memtomem.integrations.langgraph` expose the class
lazily; package star imports remain safe without LangGraph installed.

## Hybrid search

The `index` argument follows LangGraph's `IndexConfig`: `embed`, `dims`, and
`fields`. `embed` accepts a LangChain Embeddings instance, a sync/async function
from a list of strings to vectors, or a supported provider string. A model service
and its dependencies are the caller's responsibility. This runnable toy embedder
illustrates wiring, not meaningful semantic quality:

```python
from memtomem.integrations import MemtomemHybridStore

async def embed(texts):
    return [[1.0, 0.0] if "Python" in text else [0.0, 1.0] for text in texts]

async def example():
    async with MemtomemHybridStore(
        "./hybrid-memory.sqlite",
        index={"embed": embed, "dims": 2, "fields": ["text"]},
        index_id="toy-python-v1",
        retrieval={"mode": "hybrid", "weights": [1.0, 1.0], "rrf_k": 60},
    ) as store:
        await store.aput(("examples",), "one", {"text": "Python example"})
        return await store.asearch_with_diagnostics(("examples",), query="Python")
```

`index_id` is mandatory when embeddings are configured. It identifies the model
and preprocessing contract; change it when either changes. Reopening checks this
ID, dimensions, fields, database kind, and schema version. It cannot detect a
caller supplying a different model under the same ID. Incompatible settings require
export/import into a fresh database. Existing foreign databases are refused.

Each selected field value gets a vector; the best cosine score represents the
item. BM25 uses the concatenated field text and SQLite FTS5 `unicode61` with Core's
literal, prefix-query escaping and OR term matching. Dense vectors are normalized
and stored as float32. Dense scoring uses `sqlite_vec.vec_distance_cosine`, not an
approximate nearest-neighbor index.

Namespace prefix, JSON filters, and expiry are applied before each leg's candidate
limit. Each leg retains `max(100, offset + limit)` distinct items. Hybrid combines
item ranks as `sum(weight / (rrf_k + rank))`; ties use namespace/key. This bounded
fusion is not an exhaustive RRF ranking over every matching item, and changing the
requested page depth can change the candidate pool. Single-leg modes return BM25
or cosine scores; hybrid scores are RRF scores, not probabilities. Nonnegative,
finite weights with at least one positive leg are required. Zero-weight legs do
not add candidates.

Queryless search browses by descending update time, then namespace/key, with
`score=None`. `index=False` keeps an item accessible to `get` and queryless browsing,
but excludes it from query searches. Per-item `index=["text", "other.path"]`
overrides the store fields. Nested partial objects, exact arrays, dotted paths,
and `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$nin` are supported;
unknown operators raise even for an empty corpus. Namespace labels are case
sensitive. `list_namespaces` supports prefix/suffix single-segment wildcards,
maximum depth, and pagination.

`search_with_diagnostics` and `asearch_with_diagnostics` return:

- `items`: normal LangGraph `SearchItem` objects containing original JSON values.
- `diagnostics`: `effective_mode`, `score_scale`, per-leg retained
  `candidate_counts`, and `fallback_reason`.

Without embeddings, default hybrid resolves to BM25 with
`fallback_reason="embeddings_not_configured"`. If query embedding fails, hybrid
emits a `RuntimeWarning` and returns BM25 with `query_embedding_failed`; dense-only
mode raises. BM25 fallback remains available even when its configured hybrid
weight was zero. Failed embedding during **put** always raises without changing
the old record.

## Persistence and lifecycle

JSON, FTS text, vectors, timestamps, revision, and TTL commit together. Embedding
is prepared before the write transaction; an embedding or SQL failure leaves the
old value and indexes intact. Deletion and expiry sweeping remove all
representations atomically. SQLite WAL and a 30-second busy timeout support
multiple instances/processes; the last committed write wins. A revision counter
is retained for live keys, not a historical revision log.

Candidate scoring and value hydration use the same SQLite snapshot. Operations
within an instance serialize on a dedicated event-loop thread, including embedding
calls. Sync methods can therefore be used from a host with an event loop, although
async methods avoid blocking that host. Embedders must work on the store's loop;
they must not call back into the same store. The reentrant call raises
`RuntimeError` rather than deadlocking, whether it asks for a store operation or
`close()` / `aclose()`, and the store stays usable. What the *outer* call then
reports is the embedder's own business: a hybrid search treats that error like
any other embedding failure and falls back to BM25 with a `RuntimeWarning`, so
plan to see the refusal where the callback made it. The refusal reads a
contextvar, so it covers a callback that keeps the store's context: a plain sync
embedder, which LangChain dispatches through the default executor, and an async
one on the store's loop. An embedder that hands its work to an executor or pool of
its own without copying the context is invisible to that check, and reentering
from there still deadlocks. A second rule outlives the callback: a synchronous
method called on the store's own loop, as a task an embedder spawned would do,
is refused as well, since it would wait on the loop it is running on. Ordinary
async methods stay open to that task, because awaiting suspends rather than
blocks. `aclose()` is the exception and is refused there too, since it hands
`close()` to the loop's own executor. Close the store from the code that owns
it. The store installs its own default executor and identifies its workers.
Handing `close()` to one of those workers, including with
`asyncio.to_thread(store.close)` after the embedding callback has returned,
raises `RuntimeError` before shutdown begins. Calling `aclose()` from such a
worker is also refused before it can offload the close. While the callback is
still running, the embedding-callback refusal takes precedence. This executor
rule applies only to closing; ordinary synchronous operations keep their
existing callback and loop checks. External callers can still use `close()`
or `aclose()`. The store's loop drains its executor during shutdown.
Caller-owned embedding clients are not automatically closed. Always close the
store. Close drains accepted operations; cancelling an async caller does not
retract an accepted write.

`batch` commits each operation independently in input order. A later failure does
not undo earlier successful operations. Do not treat it as an all-or-nothing batch.
SQLite read operations that refresh TTL take a write transaction, which can contend
with other writers; use `refresh_ttl=False` for evaluation runs.

## TTL and transfer

TTL values are **minutes**. For example:

```python
with MemtomemHybridStore(
    "./expiring-memory.sqlite",
    ttl={"default_ttl": 60, "refresh_on_read": True, "sweep_interval_minutes": 5},
) as store:
    store.put(("sessions",), "one", {"text": "temporary"})
    store.put(("preferences",), "permanent", {"text": "persistent"}, ttl=None)
    store.get(("sessions",), "one", refresh_ttl=False)
    envelope = store.export_json()

with MemtomemHybridStore("./imported-memory.sqlite") as target:
    target.import_json(envelope)
```

Expired records are logically invisible on every read. Physical cleanup happens
only through `sweep_ttl()` or a configured sweeper. Reads refresh expiry without
changing the content's update timestamp. Export returns a JSON-serializable
`memtomem-hybrid-json-v1` envelope of live records, preserving identity, values,
timestamps, revision, per-item indexing, and absolute expiry. Async transfer APIs
are `aexport_json`/`aimport_json`. Exports contain plaintext values and no vectors;
the caller chooses whether and where to serialize the envelope.

Import validates and prepares every record, regenerates vectors with the target
configuration, and commits the entire import atomically. Duplicate identities or
existing keys reject the import. Absolute expiry is preserved, so time spent in
transit does not extend TTL. No implicit migration or in-place reindex is performed.

Configured project directories are consulted only for scope guards, not for an
implicit database or embedding model. Project-shared destinations require
`confirm_project_shared=True`. Before opening the database, the constructor
checks the persisted index configuration, including raw `fields` and `index_id`,
with the privacy write guard. This applies to reopening too; a refused
configuration creates no directory or database and leaves an existing file intact.

Put and import scan the value JSON, every namespace label, the key, and per-item
`index` selectors before embedding or storage, even with `index=False` or without
an embedder. Identifiers are also scanned as raw strings so JSON escaping cannot
hide a quoted credential. Mutable values and selectors are copied before scanning;
embedding callbacks cannot change what was approved for storage.

A privacy refusal raises `ValueError` without echoing the sensitive input. It
rejects the operation rather than redacting or renaming an identity. Remove
credentials from namespace/key/selector/model-ID strings before retrying; changing
only the value will not resolve an identifier refusal. Ordinary field names such
as `password` or `api_key` alone are permitted. The existing `force_unsafe=True`
valve remains available for `user` and `project_local`; it cannot bypass a
`project_shared` block, including configuration checks.

There is no retroactive scan or cleanup of existing rows. Reads and deletes can
still address legacy sensitive identifiers under a valid configuration. Existing
sensitive constructor settings are refused on reopen unless the private-tier
bypass applies. Database schema and export format are unchanged. Namespaces
organize records and are not authentication or tenant-isolation boundaries.

## Validation and scope

`test_langgraph_hybrid_store.py` covers ranking, filter selectivity, atomic failure,
TTL, concurrency, read snapshots, crash recovery, transfer, scope, and a real
LangGraph graph. `test_langgraph_hybrid_consumers.py` exercises actual Deep Agents
StoreBackend and LangMem tool calls without a live LLM. Optional consumer versions
are pinned in `tools/hybrid-store-consumers.txt`; install them in a separate test
environment, with this package and pytest/pytest-asyncio available.

Run `python tools/benchmark_hybrid_store.py --sizes 1000 10000 100000` for synthetic
32-dimensional BM25/dense/hybrid measurements. The checked-in benchmark artifact
in `docs/benchmarks/` records the measured environment, warmup, trial count, latency,
process peak RSS, and database size. Exact dense search and Python JSON-filter
predicates scale with the eligible/scanned corpus; this is not a production SLA or
evidence of superiority over other LangGraph stores. No relevance-quality benchmark
or live embedding-provider acceptance is claimed.

Measured on macOS arm64 / Python 3.12.11, with one warmup and ten trials per
query variant (2026-09-07). Unfiltered median latency:

| Records | BM25 | Dense | Hybrid | Process peak RSS |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 1.13 ms | 4.34 ms | 5.41 ms | 64.03 MiB |
| 10,000 | 5.06 ms | 36.80 ms | 43.55 ms | 65.98 MiB |
| 100,000 | 47.20 ms | 369.14 ms | 415.69 ms | 66.19 MiB |

The [raw measurements](../benchmarks/langgraph-hybrid-store-local.json) also
include 1%-selective filters, ingestion time, database size, and maximum latency.
These vectors have 32 synthetic dimensions; real model dimensions and embedding
latency can substantially increase cost.

This release supplies persistent hybrid retrieval and observable fallback behavior.
Snapshots/forks, revision history, query replay, reranking, Core corpus bridging,
and comparative retrieval evaluations remain future work.
