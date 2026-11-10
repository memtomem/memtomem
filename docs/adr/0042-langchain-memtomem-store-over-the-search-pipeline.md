# ADR-0042: A `langchain-memtomem` store whose query search runs the memtomem pipeline

**Status:** Proposed
**Date:** 2026-11-03
**Tracking:** #3015

## Context

#3015 proposes a new distribution, `langchain-memtomem`, holding a LangGraph
`BaseStore` whose `search(query=...)` goes through the memtomem search
pipeline over Markdown. `memtomem` itself does not change: the three classes
in `packages/memtomem/src/memtomem/integrations/`, the `memtomem[langgraph]`
extra, and the LangChain listing of `MemtomemHybridStore` stay as they are.

Today no class is both a `BaseStore` and backed by Markdown that the memtomem
pipeline ranks:

- `MemtomemBaseStore` (`integrations/langgraph_store.py:141`) is a `BaseStore`
  over JSON record files. Its query path (`_search`, `langgraph_store.py:461`)
  averages a lexical-overlap score with a dense score from the configured
  embedder. It does not use the search pipeline.
- `MemtomemHybridStore` (`integrations/langgraph_hybrid_store.py:89`) is a
  `BaseStore` over its own SQLite file with BM25, dense and weighted RRF. It
  stores one row per item, with no chunking, reranking or MMR, and its
  records are not files a person can open.
- `MemtomemStore` (`integrations/langgraph.py:222`) wraps the real components
  but is not a `BaseStore`, and its `add` appends to a day file
  (`append_entry`, `langgraph.py:662`). It cannot replace or remove one item.

The LangGraph contract the new store has to meet
(`src/oss/langgraph/stores.mdx` in langchain-ai/docs @ `e404560b613e`): all
five async methods are required (line 578), each `Item` carries a `score` when
`query` is passed (line 628), and there is no conformance suite, so
`InMemoryStore` is the reference (line 634).

### Whose memory the store holds

A LangGraph `BaseStore` is the application's memory. Its namespaces are the
application's end users and threads, and in deployment it runs on a server.
The memory directories and the index under `~/.memtomem` belong to the
developer who runs memtomem on their own machine. The first draft of this
design put the application's records into that personal index and then hid
them from the developer's default `mem_search`. Three review rounds on that
draft all landed on the machinery the hiding needed. Once the records are
hidden, nothing is gained by placing them in the personal index, so this ADR
gives the store its own index.

### What this ADR decides, and what it leaves to a spec

Four review rounds on the drafts kept finding gaps in the same place: the
step-by-step mutation and repair protocol, and the search budget algorithm.
Those are closed by code and tests, not by prose. This ADR records the
decisions that constrain them and names the document that will carry the
protocol: `packages/langchain-memtomem/docs/protocol.md`, written and
reviewed with the implementation PR, with an interruption matrix (every step
boundary of `put` and `delete` against the repair that restores agreement).

## Decision

### A. The distribution

1. **Package and version.** A new workspace member,
   `packages/langchain-memtomem`, published as `langchain-memtomem` (not
   registered on PyPI, checked 2026-11-03). It has its own version line,
   starting at `0.1.0`. It depends on `langgraph` and on
   `memtomem>=X,<Y`, where `X` is the first memtomem release carrying the
   public names in A2 and `Y` is the next minor.
2. **Supported memtomem surface and the prerequisite PR.** The package
   imports only public names. The ones that exist today:
   - `memtomem.runtime`: `create_components`, `close_components`, `Components`.
   - `memtomem.config.Mem2MemConfig` and its sub-models.
   - From `Components`: `index_engine.index_file`, `storage.delete_by_source`,
     `storage.get_all_source_files`, `storage.count_chunks_by_sources`,
     `search_pipeline.search` and `search_pipeline.invalidate_cache`.
   - `memtomem.privacy.enforce_write_guard`.
   - `memtomem.constants.validate_namespace`.
   - `memtomem.runtime.files.norm_path`, the spelling the index stores a
     source path in. Added with the release PR: the store compares hit paths
     and its sweep against its root, which an NFD spelling on macOS defeated.

   The ones a prerequisite memtomem PR adds, each with a test:
   - Public re-exports of `atomic_write_text`, `async_file_lock` and
     `memory_lock_path` (today in `memtomem.context._atomic`) and of the
     sidecar lock budget the CRUD paths use (`_CRUD_SIDECAR_LOCK_BUDGET_S`,
     `langgraph.py:610`).
   - Public names for `_encode_segment` (`langgraph_store.py:53`) and
     `_resolve_target_scope` (`langgraph.py:174`), with the resolver's
     refusal exceptions in the contract. Copies would fork a path encoding
     and a safety gate.
   - A record contract on `MemtomemBaseStore`: read and write of a record
     with its persisted `index` choice, its rendered projection text and a
     caller-owned `generation` field. Today those live behind
     `_read_record`, `_projection` and the JSON schema
     (`langgraph_store.py:79, 263, 398`).
   - A configuration constructor that reads no environment. `Mem2MemConfig`
     is a `BaseSettings` with `env_prefix="MEMTOMEM_"` (`config.py:1271-1273`),
     and `load_ambient_config=False` skips the files, not the environment.
   - Instance-local FTS tokenizer selection. `create_components` calls
     `set_tokenizer` (`runtime/components.py:206`) on a process-global
     (`storage/fts_tokenizer.py:28`) that the SQLite query path reads
     (`storage/sqlite_search_ops.py:199`). A refusal at open would not
     protect the store's lifetime: a stack created later in the same process
     with another tokenizer changes the global under it. So the storage
     backend carries its tokenizer as an instance attribute and the global
     goes. The package is not released before this lands.
3. **Release and CI.** The root workspace lists one member
   (`pyproject.toml:9`). `release.yml` builds only `packages/memtomem` (lines
   128 and 168) and triggers on `v*` and `test-v*` tags. The new project gets
   its own tag prefix, its own trusted publisher, build and artifact
   validation, and a CI job, with one `uv.lock` covering both members. This
   ADR records the decision. The mechanics ship in their own PR.
4. **Reuse by composition, over the record layer only.** The new store wraps
   a `MemtomemBaseStore` for JSON records, through the record contract in
   A2. That gives it `get`, `list_namespaces`, value `filter`, `index=False`
   and selectors, offset/limit and the record path layout
   (`langgraph_store.py:261, 329`). The wrapper owns every mutation and the
   query path. It does not route through `MemtomemStore.add`, because append
   cannot replace an item.

   > **Amended 2026-11-03** with the implementation PR. The store owns its
   > records instead of wrapping `MemtomemBaseStore`. That store's path
   > layout is base64url, which is case-sensitive, and on a filesystem that
   > folds case (NTFS; APFS as macOS formats it) two segments whose encodings
   > differ only in case share one path (`aaa` → `YWFh`, `aaG` → `YWFH`).
   > Measured: `MemtomemBaseStore` refuses the second of two such keys and
   > drops the second of two such namespaces from `list_namespaces` and
   > `search`; the implementation that wrapped it lost a projection on
   > reopen on macOS and, on Windows CI, served one namespace's item to
   > another namespace's query. The package keeps the same record shape and
   > the same record guard under a single-case layout (`s` + lowercase
   > base32), and still imports memtomem's guard, atomic write and filter
   > predicate (`matches_filter`, made public for this). The record contract
   > A2 added to `MemtomemBaseStore` stays public and unused by the package.
   > `packages/langchain-memtomem/docs/protocol.md` §Layout has the detail.

   > **Amended 2026-11-03** after #3034. `MemtomemBaseStore` now lays its
   > records out in the same single-case alphabet (`s` + lowercase base32,
   > under `records.v2/`), with a hashed name for a value too long for a
   > file, so the collision above no longer applies to it. The package
   > still owns its records and its codec: it decodes identity back from its
   > paths, which a hashed name would not allow.

### B. The store over its own pipeline

1. **Own components, own configuration.** The store builds a `Mem2MemConfig`
   through the environment-free constructor (A2) and calls
   `create_components(config, load_ambient_config=False)`
   (`runtime/components.py:162-166`). The developer's `config.json`,
   `config.d` and `MEMTOMEM_*` variables never reach it. The configuration
   sets `storage.sqlite_path` to `<root>/index.db` (the default is the
   personal database, `config.py:281`), `indexing.memory_dirs` to
   `[<root>/projections]` alone, `search.system_namespace_prefixes` to `[]`
   (the defaults `archive:` and `agent-runtime:`, `constants.py:356-359`,
   would hide an application namespace starting with one of them), the
   embedder to the caller's choice with `provider="none"` as the default
   (`config.py:75`, BM25 only, no model download), and no namespace rules.
   The engine, pipeline and storage are the server's classes. No watcher
   runs: `create_components` starts none, and the store is the only writer
   of its projection directory. Ambient inputs that remain, each named in
   the docs: the project registry read for the scope gate (B7), ONNX model
   caches under `~/.memtomem/cache`, and the schema peer check in
   `storage/schema_peers.py`.
2. **Layout.** `<root>/records/` holds the JSON records and
   `<root>/projections/` holds one Markdown file per indexed item, both
   under one caller-chosen root. The record tree is outside
   `indexing.memory_dirs`, so its `.json` files are never indexed (`.json`
   is indexable inside a memory dir, `indexing/engine.py:1006`). Each
   projection carries a header with the encoded `(namespace, key)` and the
   record's generation. An item stored with `index=False` has a record and
   no projection.
3. **Namespaces.** A LangGraph namespace tuple maps to one memtomem
   namespace by encoding every segment, always, and joining with `:`. Each
   encoded segment is base64url (the promoted `_encode_segment`, output in
   `[A-Za-z0-9_-]`) behind a fixed marker letter, so no segment starts with
   `-`, no raw segment can collide with an encoded one, and the mapping is
   injective. The joined form must pass `validate_namespace`
   (`constants.py:155-175`): segments in `[A-Za-z0-9._-]+`, no leading `-`,
   256 characters in total. A tuple that exceeds the limit is refused at
   `put` with an error that names the limit and the encoded length. Every
   `index_file` call passes `namespace=` explicitly, and an explicit
   namespace wins over stored rows and rules (`indexing/engine.py:1552`,
   `1580`).

   > **Amended 2026-11-03.** The segments are the package's own codec,
   > `s` + lowercase base32 (`[a-z2-7]`), not base64url; see the A4
   > amendments.
4. **Search.** `get`, `list_namespaces` and a query-less `search` read only
   the JSON records. `search(query=...)`:
   - Narrows the pipeline by namespace and then checks the tuple. The
     invariant: an item is returned for `namespace_prefix` exactly when its
     original tuple has that prefix, the prefix itself included. The public
     selector offers a list of exact names or one glob pattern
     (`NamespaceFilter`, `models.py:212-227`; the glob is a `LIKE` in
     `storage/sqlite_helpers.py:471`, as `archive:session:*` at
     `search/pipeline.py:1035`), never both, and `LIKE` folds ASCII case
     (`models.py:112`), so no single selector expresses the invariant. The
     protocol chooses the retrieval strategy; the store decodes each hit's
     header and keeps only items whose tuple satisfies the invariant.
   - Serves a hit only when the record, the projection and the last-indexed
     generation of its item agree, and the record passes the value `filter`.
     A disagreeing item is not served from stale rows: an instance that
     wrote generation 2's record and projection and died before indexing
     leaves rows of generation 1 that another open instance would otherwise
     keep returning. The store repairs the item under its lock before
     serving it, or skips it; the protocol chooses.
   - De-duplicates chunks per item. The item's `score` is the best final
     pipeline score among its chunks, after the rerank
     (`search/pipeline.py:2065`) and the later modifiers.
   - Paginates over items, not chunks: `offset + limit` distinct eligible
     items are collected before slicing. The widening and termination
     mechanics live in the protocol document, under these constraints:
     recall is bounded by a store setting `max_chunks_per_search`; the
     pipeline truncates before its validity filter
     (`search/pipeline.py:2117`), so a window that adds no new eligible item
     does not by itself establish exhaustion (two windows of only expired
     chunks can precede an eligible item at the next rank); with a reranker
     on, the candidate pool is capped at `clamp(oversample * top_k,
     [min_pool, max_pool])` (`search/pipeline.py:1981`), so the effective
     budget in that mode is `min(max_chunks_per_search, max_pool)`; and the
     reranker is not switched off per call to widen further, because that
     would mix post-rerank and fused scores in one list. The docs state the
     bound.
     > **Amended 2026-11-03** after #3043. The reranker's candidate pool is
     > no longer smaller than the request: it is `max(top_k, clamp(...))`,
     > because a pool below `top_k` returned fewer results than asked for
     > with nothing saying so. `max_pool` now bounds only the oversampling,
     > and the budget is `max_chunks_per_search` with or without a reranker,
     > on a memtomem that carries this fix.
     > **Amended 2026-11-10.** The store takes `retrieval={"mode": "hybrid" |
     > "bm25" | "dense", "weights", "rrf_k"}`, the keys and refusals of
     > `MemtomemHybridStore`, so a caller can choose the legs without spelling
     > pipeline settings. It is translated into the `search` settings the
     > pipeline already honours (`enable_bm25`, `enable_dense`,
     > `rrf_weights`, `rrf_k`), and giving both forms is refused. Without it,
     > the search is the one above. `dense` is refused at open without an
     > embedder, and raises when a pass's dense leg did not run; `hybrid`
     > ranks by BM25 alone in that state. The mode is set per store because
     > LangGraph's `SearchOp` carries no per-call field. Mechanics:
     > `packages/langchain-memtomem/docs/protocol.md`, "Retrieval legs".
5. **Mutation and repair: the constraints the protocol must meet.**
   - The JSON record is the source of truth. The projection and the index
     rows are derived from it.
   - Every mutation of an item runs under that item's projection sidecar
     lock, `async_file_lock(memory_lock_path(projection), timeout=<budget>)`,
     as `MemtomemStore.add` does (`langgraph.py:627-628`). Two store
     instances over one root, in one or two processes, serialise on it. The
     ordering invariant in `context/_atomic.py:319-322` applies: levels low
     to high, sorted keys within a level, no FIFO promise.
   - Record first, then projection, then `index_file(projection,
     namespace=<B3>, lock_held=True, ...)`, then `invalidate_cache()`. The
     record carries a generation the store increments on every write; the
     projection header carries the generation it was rendered from; the
     store persists the generation it last indexed. Repair compares all
     three, so an interruption after the projection write and before the
     index (same record and projection generation, stale rows) is detected
     as well as a stale projection.
     > **Amended 2026-11-03** with the implementation PR. The projection
     > carries no header: it is the rendered value alone, behind one empty
     > line, so none of the store's bookkeeping is searchable and no value
     > can imitate it (measured: a value quoting the store's generation tag
     > hid its own item; a superscript digit in that tag made any search
     > that reached the item raise). Projection agreement is byte equality
     > with the text the record renders to. The persisted last-indexed state
     > holds that text's digest and, for every row indexed from it, the
     > memtomem chunk id with a digest of the text the row is ranked by. A
     > hit is served only when that pair is in the set. The id alone would
     > not do: memtomem keeps an id when the body hash matches (ADR-0005),
     > and the body hash leaves out the heading path retrieval ranks by.
     > B2's header and B4's "decodes each hit's header" read accordingly:
     > the hit's item is named by its source path and checked against the
     > row's namespace.
     > **Amended 2026-11-10** after #3205. The projection's path is
     > bookkeeping too, and it was searchable: memtomem's BM25 leg matches a
     > query against `source_file` once no row's content matches (#2224), and
     > every projection path shares the root's words and `projections`, so
     > such a query returned every item, scored as real matches at those
     > ranks would be. The store
     > now opens its components with
     > `create_components(..., bm25_path_fallback=False)`, a public keyword
     > added for it, so BM25 matches content only. Nothing a caller types is
     > lost: keys and namespaces appear in the path only in encoded form
     > (measured: with the fallback on, querying an item's key found
     > nothing). The package's memtomem floor rises to the first release
     > carrying the keyword. This closes the path, not all bookkeeping:
     > under bounded chunking (on for E5) the chunker writes
     > `File: <encoded key>.md` into each row's retrieval text, which BM25
     > indexes as content (#3209).
   - A deletion marker carries the generation it was written for. A later
     `put` under the lock settles or supersedes the marker before it writes.
     Repair gives a marker precedence only over a record of the same or an
     older generation.
   - `index=False` writes the record, removes any projection and its rows
     (`storage.delete_by_source`, `storage/base.py:223`), and skips indexing.
   - Deletion is `delete_by_source`. `_delete_missing_source`
     (`indexing/engine.py:1899`) is not a deletion path: it holds rows and
     returns `deleted=0`.
   - Success of an index step is judged by agreement, not by counts.
     `IndexingStats.errors` (`models.py:521`) non-empty, or an excluded or
     skipped path, is a failed `put` and the store raises. `indexed_chunks ==
     0` alone is not failure: unchanged and metadata-only chunks are
     reported as skipped (`indexing/engine.py:2856`).
   - Repair on open is idempotent, runs under the same locks before the
     store serves operations, and finds rows with no projection through the
     existing `get_all_source_files` and `count_chunks_by_sources(...,
     as_stored=True)` (`storage/base.py:311, 340`). Re-indexing keeps the UUID of chunks
     whose content hash matches and deletes the rest
     (`indexing/engine.py:1334-1339`, ADR-0005). The store keys on
     `(namespace, key)`, never on chunk ids.

   The ordered steps, the interruption matrix and the repair cases live in
   the protocol document and are reviewed with the code.
6. **Read-after-write.** A `search` on the instance that wrote sees the
   change at once, because the mutation invalidates its cache. Another
   instance over the same root sees it after its own `search.cache_ttl`,
   30 s by default (`config.py:308`). The cross-process
   `source_visibility_epoch` (`search/pipeline.py:1615`) is bumped by holds
   and orphan GC (`storage/sqlite_source_holds_ops.py:95`), not by content
   writes, so it does not shorten that window. The store sets `cache_ttl` in
   its own configuration, and the docs state the window.
7. **Scope.** The one ambient input the store reads on purpose is the
   developer's project registry, `indexing.project_memory_dirs` from the
   personal configuration, the way `MemtomemBaseStore` reads it today
   (`_registered_project_dirs`, `langgraph_store.py:178`). The store
   resolves its root's tier once, through the promoted scope resolver fed
   with that registry, and keeps the result. The same registry is written
   into the store's own configuration, so the indexer's guard
   (`indexing/engine.py:2266`) derives the same tier and every guard
   agrees. A root inside a registered `project_shared` directory opens only
   with `confirm_project_shared=True`, and the hard refusal of `force_unsafe`
   for that tier applies as it does in `MemtomemBaseStore`. A developer who wants to read an application's
   memory from their own memtomem adds `<root>/projections` to
   `indexing.read_only_memory_dirs` (`config.py:402`) in their personal
   configuration. That is their opt-in, outside this package.
8. **Privacy.** Two guards run before the first write: the full-record guard
   `MemtomemBaseStore` runs today, over the serialized value, namespace, key
   and selectors (`langgraph_store.py:376-386`), and the rendered projection
   text when a projection is written. The indexer runs its own guard
   (`indexing/engine.py:2266`), so the store passes its decision through
   `index_file`'s `ingress_guarded` and `valve_text` parameters, and
   `force_unsafe` only when the caller set it. `force_unsafe` defaults to
   `False`.

### C. LangChain docs

The existing listing (`MemtomemHybridStore`, package `memtomem`) stays. The
new store gets its own entry with package `langchain-memtomem`, filed through
the integration-submission issue form. Manual listing PRs are closed by
`external-integration-pr-comment.yml` (langchain-ai/docs @ `a28c75411093`);
the workflow's featured and 50,000-downloads exception applies to new
integration pages, not to a listing-only change. A dedicated page under
`src/oss/python/integrations/stores/` is optional. If written, it follows the
headings of `openviking.mdx` (@ `fddd948a41c5`): Overview, Setup,
Instantiation, Usage and API reference.

## Consequences

- **Three PRs, in order.** The prerequisite memtomem PR (A2). The
  implementation PR with `packages/langchain-memtomem`, the protocol
  document and its tests. The release and CI PR (A3).
- **The implementation PR's acceptance criteria**, carried in the protocol
  document: a `put` latency measurement with `provider="none"` and with the
  E5 profile; an interruption test at every step boundary of `put` and
  `delete`, including the stale-rows case; a two-instance race of `delete`
  against `put` over one root; a behaviour test against `InMemoryStore` for
  `get`, `search` with and without `query`, `namespace_prefix` including the
  prefix itself and a case-variant sibling, `list_namespaces`, `filter`,
  `offset`, `limit` and `index=False`; a test that a `config.d` fragment and
  a `MEMTOMEM_*` variable changing `sqlite_path` have no effect.
- **What the store adds over `MemtomemHybridStore`:** the record is a
  Markdown file a person can open and diff; items are chunked and ranked by
  the full pipeline (query expansion, BM25 and dense fusion, rerank, time
  decay, MMR, entity boost); namespaces and the redaction guard are the
  server's own. What it gives up: `MemtomemHybridStore`'s TTL sweep, which
  the first version does not implement.
- **Each item is stored twice.** The JSON record is read; the projection is
  the only thing indexed.
- **Recall is bounded** by `max_chunks_per_search` (B4), stated in the docs.
- **No connection to the developer's memory unless they make one** (B7).

## Alternatives considered

- **Index into the developer's personal memtomem** (the first draft). Needs
  a hidden namespace prefix (one of 10 capped entries, `config.py:340`), a
  namespace rule for the projection directory so a watcher does not stamp
  rows with the default namespace (`indexing/engine.py:1554`), a repair for
  rows stamped wrongly (a plain re-index preserves a unanimous stored
  namespace; only `force=True` with an explicit namespace rewrites it), and
  a refusal to open when the developer's config lacks either entry. All of
  it exists to hide the records from the person whose index they sit in.
- **Route writes through `MemtomemStore.add`.** It appends to a day file, so
  an update leaves the old text searchable, and there is no single-item
  delete.
- **Index the JSON records directly.** The chunks would be serialized JSON,
  not readable Markdown, and the item boundary a chunker accident.
- **Extend `MemtomemHybridStore` with the pipeline.** Its rows are items, not
  chunks, and its schema is its own. Making it run the pipeline means
  replacing its storage with the server's, which is the new store.
- **Keep raw segments when they look safe.** `("a b",)` and `("YSBi",)` would
  share a namespace. Encoding every segment is the cost of injectivity.
- **Re-implement the private helpers in the package.** Forks a path encoding
  and a safety gate that must stay identical to memtomem's.
- **Move the existing classes into the new package.** The first draft of
  #3015, dropped there. It breaks current importers and the existing
  LangChain listing for no gain to the new store.

## Open questions

- Whether `put` batches several items into one index call when LangGraph's
  `abatch` passes many `PutOp`s. Decided by the latency measurement.
- The default of `max_chunks_per_search`, decided against a measured
  chunk-per-item distribution.
- Whether a TTL sweep belongs in the first version.

## References

- #3015: the proposal this ADR answers, with the review comments that moved
  B to a dedicated index.
- ADR-0005: force re-index metadata contract (chunk identity on re-index).
- ADR-0011: canonical artifact scope hierarchy (`project_shared` gates).
- langchain-ai/docs#7309 and langchain-ai/docs#7318: the existing
  `MemtomemHybridStore` listing.
