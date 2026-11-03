# langchain-memtomem — the store protocol

What `MemtomemPipelineStore` writes, in what order, what repair does with
whatever an interruption left behind, and how a query search widens its
window. ADR-0042 in the memtomem repository records the decisions; this
document is the contract the code and the tests in `tests/` implement. Every
row of the interruption matrix is one test case in `tests/test_lcmm_protocol.py`.
`tests/test_lcmm_model.py` checks the same contract on generated sequences:
two instances over one root, puts (heading-only edits and `index=False`
included), deletes, crashes at every step boundary followed by a restart, and
loss of `index.db` with an interrupted first repair. After every step no
query search may serve a deleted item, an old value or a word the value no
longer holds; at intervals a freshly opened instance must find every item by
every word. Seeds are fixed (20 by default, `LANGCHAIN_MEMTOMEM_MODEL_SEEDS`
for more; 300 passed when this was written).

Source references are to memtomem at `bb0e63a9` (2026-11-03).

## Layout

One caller-chosen `root`, normalised once at construction with memtomem's
`norm_path` (resolved, and NFC-folded where the filesystem folds Unicode
forms), because that is the spelling the index stores source paths under and
both hit decoding and the orphan sweep compare paths against the root. An
NFD-spelled root on macOS would otherwise match no hit.

```
<root>/
  index.db                      the store's SQLite (+ -wal, -shm)
  records/<seg>/…/<key>.json    one JSON record per item — the source of truth
  records/<seg>/…/<key>.json.deleting
                                deletion marker: the generation a delete started on
  records/<seg>/…/<key>.json.last
                                tombstone: the generation a deleted item reached (kept)
  projections/<seg>/…/<key>.md  one Markdown projection per indexed item
  projections/<seg>/…/<key>.md.indexed
                                last-indexed marker: generation, text digest, row stamps
  projections/<seg>/…/.<key>.md.lock
                                memtomem's sidecar lock for the item (see Locks)
```

`<seg>` and `<key>` are `_protocol.encode_segment` of each namespace segment
and of the key: the letter `s`, then the lowercase RFC 4648 base32 of the
UTF-8, without padding. The record tree and the projection tree mirror each
other. `records/` is outside the index's `memory_dirs`, so its `.json` files
are never indexed; `projections/` is the index's only memory dir.

**Why base32, not memtomem's base64url segment codec.** The paths have to be
distinct on a filesystem that folds case — NTFS, and APFS as macOS formats it
by default. base64url is case-sensitive: `aaa` and `aaG` encode to `YWFh`
and `YWFH`, which name one directory there. Measured on macOS (APFS, case
folding): with `MemtomemBaseStore`'s layout, two keys of one namespace whose
encodings differ only in case cannot both be written (the second `put` raises
`StoreRecordError`), and of two such namespaces the second drops out of
`list_namespaces` and `search`. An earlier version of this store, which reused
that layout, lost the second namespace's projection on reopen on macOS and,
on Windows, returned one namespace's item from the other's query search. The
lowercase base32 alphabet (`a-z2-7`) has one case, so no fold can join two
tokens. The marker `s` keeps a raw segment from equalling an encoded one and
keeps a token off every Windows reserved device name (`CON`, `COM2`, `LPT3`,
…). A token is at most 200 characters (a segment or key of up to 124
bytes of UTF-8, measured). The longest name the store writes is the temporary
file of an atomic marker write, `.<token>.json.deleting.<8 random>.tmp`, the
token plus 28 characters, which then fits a 255-byte file name.

The records themselves are `_records.py`'s, not `MemtomemBaseStore`'s: the
same JSON shape (`schema_version` 1, `namespace`, `key`, `value`, `index`,
`generation`, `created_at`, `updated_at`), the same record guard (the
serialized value, every namespace segment, the key and the selectors are
scanned before the first write), memtomem's atomic write and memtomem's
`matches_filter` for `search(filter=…)`. A record must sit at the path its
own `namespace` and `key` encode to, or it is treated as torn.

A projection file is one empty line, then `render_projection(value, index)`:
the whole value for `index=None`, the selected fields for a list of JSON-path
selectors. Nothing of the store's own bookkeeping is in it, so none of it is
searchable and no value can imitate it. The empty line keeps memtomem's
chunker from reading a value that opens with `---` as frontmatter (measured:
such a value setting `valid_to` in the past was otherwise hidden from search).
A record whose selectors render to empty text has no projection, the same as
`index=False`.

## Namespaces

A LangGraph tuple maps to one memtomem namespace: every segment encoded with
the same codec as the paths, joined with `:`
(`_protocol.to_memtomem_namespace`). The joined form must pass
`memtomem.constants.validate_namespace`; a tuple over 256 characters is
refused at `put` with the cap and the encoded length. Every `index_file` call
passes `namespace=` explicitly (an explicit namespace wins over stored rows and
rules, `indexing/engine.py:1580`).

## Generation agreement

Each record carries a generation: `put` writes `max(existing record, .last
tombstone, .deleting marker) + 1`. The `.indexed` marker, written after
`index_file` returned clean, holds the generation it was indexed from, the
SHA-256 of the projection text, and a **stamp** for every row the index holds
for the file afterwards: the memtomem chunk id and the SHA-256 of the text the
row is ranked by (`Chunk.retrieval_content`, the heading path or retrieval
context and then the body). Every index step unlinks the marker first, so an
interruption before the new marker is written leaves none.

The generation is monotonic for a key across deletion: a delete writes the
`.last` tombstone with the generation the item reached and never removes it.
Without it a re-created key would start again at 1 and a deletion marker left
by an interrupted delete would outrank the new record, so repair would delete
it.

An item is **whole** when the record exists and has something to rank, has no
deletion marker, the projection file holds exactly the text the record renders
to, the marker names the record's generation and that text's digest, and the
index holds at least one row for the projection path (`ItemState.agrees`).
Only a whole item is served by a query search, and only from hits whose
stamps are all in the marker's set. Repair also compares the rows the index
holds now with the marker's stamps, so rows the marker does not describe (an
older `index.db` restored under current files) are rebuilt.

**Why stamps.** A row left by an interrupted `put`, a row another instance
cached before this one rewrote the item, or a row from before a delete is
either ranked by the same text as the current rows (its score is still right)
or carries a stamp outside the current set (it is not served). The chunk id
alone does not establish that: memtomem keeps an id across a re-index when the
body hash matches (`indexing/engine.py:1334-1339`, ADR-0005), and the body
hash leaves out the heading path, so a value whose heading alone changed kept
its ids and a cached hit for the old heading was served with the new value.
An earlier version carried the generation as a frontmatter tag inside the
projection; a value quoting `tags: [lcmm-gen-999]` then hid its own item, and
a superscript digit made any search that reached the item raise.

## Locks

Every mutation and every repair of an item runs under that item's memtomem
sidecar lock: `async_file_lock(memory_lock_path(<projection path>),
timeout=…)` from `memtomem.runtime.files`, the lock memtomem's own writers
take for a memory file. The projection's directory is created before the
acquire because `memory_lock_path` resolves its argument. Two store instances
over one root, in one process or two, serialise on it. `index_file` is called
with `lock_held=True`, so the engine does not re-acquire the sidecar (which
would self-deadlock within one process) and goes straight to its own
in-process index lock. The order sidecar → engine lock → storage follows the
level rule in `memtomem/context/_atomic.py` (low to high). The store takes no
other lock: a record is written with an atomic replace, so a read without the
lock sees either the previous record or the new one.

Cancellation: the store's own file work under the lock — reading and
writing the record, the projection and the markers — runs synchronously on
the event loop, not in a worker thread. A task cancelled while awaiting a
thread releases the lock while the thread keeps working, and a late unlink
could remove a record another instance wrote in between. The awaited
memtomem calls (`index_file`, `delete_by_source`) protect their own work; a
cancellation there leaves the item non-whole and repairable.

Timeouts: `put`, `delete` and open-time repair wait
`CRUD_SIDECAR_LOCK_BUDGET_S` (5 s, memtomem's own CRUD budget). A query search
that finds a disagreeing item waits `SEARCH_REPAIR_LOCK_S` (0.25 s) and skips
the item when the lock is still held, because a held lock means a writer is
mid-mutation and the item will disagree until it finishes.

## Put

`put(namespace, key, value, index)`, under the item's lock:

| step | writes |
|---|---|
| 0 | render the projection text; read the existing record; `generation = max(record, .last, .deleting) + 1` |
| 1 | **guard the projection text** (`enforce_write_guard`, surface `langchain_memtomem_put`, the store's tier, `force_unsafe` as configured); `blocked` or `blocked_project_shared` raises `ValueError` and nothing has been written |
| 1b | **guard the record** (`RecordStore.prepare`: the serialized value, namespace, key and selectors); a block raises and nothing has been written |
| 2 | write the record atomically with the new generation |
| 3 | unlink `.deleting` if present — the record just written outranks the marker |
| 4a | `index=False`: unlink the projection and `.indexed`, `storage.delete_by_source(projection)`; done |
| 4b | `atomic_write_text(projection)` |
| 5 | `index_file(projection, namespace=, lock_held=True, ingress_guarded=True, valve_text=<file text>, force_unsafe=<step 1 said bypassed>)`; judge the stats |
| 6 | write `.indexed` = generation |
| after the lock | `search_pipeline.invalidate_cache()` |

Step 4a's two unlinks have a boundary of their own (`put.between_unlinks`).
Both guards run before the first write (ADR B8): steps 1 and 1b precede every
write. A blocked record leaves the projection guard's audit line and nothing
else.

Judging step 5 (`_judge`): `total_files == 0` (the engine excluded or skipped
the path), a non-empty `errors`, or `blocked_files > 0` is a failed `put` and
raises `StoreIndexError` — the record is written, the item is not whole, and
the next open or the next `put` repairs it. `indexed_chunks == 0` alone is
not failure: unchanged chunks are reported as skipped. A `PrivacyRejection`
from the engine is re-raised as `ValueError`.

`PutOp(value=None)` (LangGraph's delete-by-put) runs Delete. `abatch` runs
its ops in order; each `PutOp` is one indexed `put` (see Measurements for why
there is no cross-item batching yet).

## Delete

`delete(namespace, key)`, under the item's lock. A missing record with no
projection, marker or deletion marker is a no-op.

| step | writes |
|---|---|
| 1 | write `.deleting` and `.last` = `max(record generation, .last, .deleting)` (`0` with none) |
| 2 | unlink the projection and `.indexed` |
| 3 | `storage.delete_by_source(projection)` |
| 4 | unlink the record |
| 5 | unlink `.deleting` |
| after the lock | `invalidate_cache()` |

## Repair

Repair runs at open, over every item named by a record, a `.deleting`
marker, a projection or an `.indexed` marker, each under its lock, and before
the store serves any operation. A query search runs the same single-item
repair on a disagreeing hit. Repair is idempotent: a second pass finds every
item whole or absent.

The state is read under the lock (`_read_state`) and classified by one
ordered decision (`_protocol.classify`); the first matching row wins.

| # | state | action |
|---|---|---|
| 1 | `.deleting` present and (no record, or record gen ≤ marker gen) | **finish delete**: write `.last`, unlink projection and `.indexed`, `delete_by_source`, unlink record, unlink `.deleting` |
| 2 | `.deleting` present and record gen > marker gen | the marker is superseded: unlink it, continue below |
| 3 | no readable record, or record has `index=False` | **purge projection** if a projection, an `.indexed` marker or rows exist (unlink both files, `delete_by_source`); else nothing |
| 4 | projection missing, or not exactly the text the record renders to | **render and index**: write the projection from the record, guard it (surface `langchain_memtomem_repair`), `index_file`, write `.indexed` |
| 5 | projection current, but the marker names another generation or digest, no rows, or rows whose stamps differ from the marker's | **index**: unlink `.indexed`, `index_file` the projection, write `.indexed` |
| 6 | whole | nothing |

A guard block or a failed index in rows 4–5 is logged and the item stays
unserved (no agreeing marker); the record is intact and the next open or
`put` retries. After the per-item pass: a projection file whose path does not
decode to an item is unlinked and its rows deleted; every source path the
index holds that is not in the snapshot of present projection files is
re-judged **under its item's lock** — a put on another instance can land
between the snapshot and the row listing — and its rows are deleted only
when the projection is still absent (a path that decodes to no item is
deleted outright). Paths are compared in memtomem's stored spelling
(`norm_path`: realpath, NFC on macOS).

### Interruption matrix

Each row: the step boundary a crash (or the test's `Steps.at`) interrupts,
what repair observes, which row above classifies it, and the outcome. "A" is
the item's previous generation, "B" the one being written.

Put, over an existing indexed item (gen 1 = A):

Columns: the record, the text in the projection file, the marker's
generation, whose text the rows hold.

| interrupted after | record | projection | marker | rows | row | outcome |
|---|---|---|---|---|---|---|
| step 1 (`put.before_record`) | A gen 1 | A | 1 | A | 6 | A, whole |
| step 2 (`put.after_record`) | B gen 2 | A | 1 | A | 4 | B rendered and indexed |
| step 4b (`put.after_projection`) — **the stale-rows case** | B gen 2 | B | 1 | A | 5 | rows rebuilt from B |
| step 5 (`put.after_index`) | B gen 2 | B | 1 | B | 5 | marker written |
| step 6 (`put.after_marker`) | B gen 2 | B | 2 | B | 6 | whole (only the cache invalidation was skipped; a fresh open has a fresh cache) |

Put with `index=False` over an indexed item:

| interrupted after | record | projection | marker | rows | row | outcome |
|---|---|---|---|---|---|---|
| step 2 (`put.after_record`) | B, `index=False` | A | 1 | A | 3 | projection and rows purged |
| step 4a, between the two unlinks (`put.between_unlinks`) | B, `index=False` | — | 1 | A | 3 | marker and rows purged |

First put of a new item interrupted after step 2 (`put.after_record`): record
gen 1, nothing derived → row 4 → rendered and indexed.

A repair interrupted between its index and its marker (`repair.after_index`)
leaves new rows and no marker → row 5 → indexed again, marker written.

Delete, over a whole item (gen 1):

| interrupted after | record | `.deleting` | projection | marker | rows | row | outcome |
|---|---|---|---|---|---|---|---|
| step 1 | gen 1 | 1 | yes | 1 | yes | 1 | finished |
| step 2 | gen 1 | 1 | — | — | yes | 1 | finished |
| step 3 | gen 1 | 1 | — | — | — | 1 | finished |
| step 4 | — | 1 | — | — | — | 1 | finished (marker removed) |

Delete interrupted after step 4, then a put of the same key interrupted
after its record write: the new record is generation 2 (from the `.last`
tombstone), the marker says 1, so row 2 drops the marker and row 4 finishes
the put. Without the tombstone the record would be generation 1 and row 1
would delete it.

Delete marker superseded: delete interrupted after step 2, then a `put`
completes (record gen 2 > marker gen 1): the put unlinks the marker at its
step 3; had it crashed before that, row 2 drops the marker and row 6 (or
4–5) finishes the put.

### Two instances, `delete` racing `put`

Both serialise on the item's sidecar lock, so the outcome is one of the two
serial orders: `put` then `delete` (item absent, only the `.last` tombstone)
or `delete` then `put` (record, projection, marker and rows at generation 2,
whole). Each
instance's next search sees the outcome: the writing instance invalidated its
cache; the other sees it after its `search.cache_ttl`, or at once if its hit
disagrees with the files.

## Search

`get`, `list_namespaces` and a `search` without `query` read the JSON records
only, through `_records.RecordStore` (an `index=False` item is listed and
filtered like any other; a query-less search returns newest first, unscored). `search(query=…)`:

1. **Narrow by namespace.** For a non-empty `namespace_prefix`, the pipeline
   is called with the pattern `<encoded prefix>*`; for the empty prefix, with
   no namespace filter (the store's `system_namespace_prefixes` is `[]`, so
   nothing is hidden). The pattern over-matches on purpose: it is a SQL
   `LIKE`, so the encoding of a shorter segment can prefix a longer one's
   (`abc` → `smfrgg`, `abcdef` → `smfrggzdfmy`). LIKE also folds ASCII
   case, which a single-case codec makes moot. The hit's source path is decoded back to its tuple
   and kept only when `tuple[:len(prefix)] == prefix` — the prefix itself
   included. The store's own tier is passed as `scope=`; the pipeline's
   default SQL is `scope = 'user'`, which would return nothing for a root in a
   registered project tier.
2. **Group chunks into items.** Hits are grouped by `(namespace, key)` in
   order of first appearance; the item's score is the best final pipeline
   score among its chunks (after rerank and the later modifiers), and the
   stamps of its hits go with it. A hit counts for an item only when the
   row's own namespace is the one the path encodes.
3. **Judge each item** against its files: served only when the item is whole
   and every hit's stamp is in the marker's set, and the record's value
   passes `filter` (memtomem's `matches_filter`, the predicate
   `MemtomemBaseStore` applies too, used on both paths here: dotted keys, literal
   equality, `$eq $ne $gt $gte $lt $lte $in $nin`; a nested mapping is
   compared for equality, where LangGraph's `InMemoryStore` matches a
   subset and refuses `$in`/`$nin`). A disagreeing item is repaired under its lock
   if the lock is free within 0.25 s, and the window is then **retrieved
   again** from the fresh rows with `record=False` (which bypasses the result
   cache); on that second pass nothing is repaired and a still-disagreeing
   item is skipped. The hit in hand is never served after a repair: its score
   was computed over rows of other text.
4. **Paginate over items.** `offset + limit` eligible items are collected
   before slicing, so a later page never underfills because an earlier item
   had many chunks.
5. **Widen.** The first window asks the pipeline for `top_k = min(budget,
   max(2 · (offset + limit), 20))` chunks, where `budget` is the store's
   `max_chunks_per_search` (default 200, at least 1). While fewer than
   `offset + limit` items are eligible and `top_k < budget`, `top_k =
   min(budget, 2 · top_k)` and the window is retrieved again. Each window
   stands alone: its ranking is rebuilt from that window's hits (a window is
   never merged with the previous one, so a reranker that fell back to fused
   scores mid-widening cannot mix two scales in one list). A window that adds
   no eligible item does not by itself end the search: the pipeline truncates
   to `top_k` before its validity filter, so two windows of filtered-out
   chunks can precede an eligible item at the next rank. Termination is
   `offset + limit` items or `top_k == budget`.

Bounds the docs state: recall is bounded by `max_chunks_per_search`; with
reranking on, the candidate pool is capped at `clamp(oversample · top_k,
[min_pool, max_pool])` (`search/pipeline.py:1981`), so the effective bound is
`min(max_chunks_per_search, rerank.max_pool)`. The store never passes
`rerank=False` per call. An item whose new generation is not yet indexed (an
interrupted `put`) is found by a query on its **old** text, which repairs it,
and by every query after that; a query on its new text alone finds nothing to
repair until the next open.

## Read-after-write

The writing instance sees its own write at once (its cache is invalidated).
Another instance over the same root sees it after its `search.cache_ttl`
(30 s by default, settable through `config={"search": {"cache_ttl": …}}`),
or at once when one of its hits disagrees with the files (step 3 above).

## Isolation and the ambient inputs that remain

The components are built from `Mem2MemConfig.without_environment(...)` with
`storage.sqlite_path = <root>/index.db`, `indexing.memory_dirs =
[<root>/projections]`, `search.system_namespace_prefixes = []`, the caller's
`config` sections (`embedding`, `search`, `rerank`, `chunking`, …; `storage`
and `indexing` are refused) and `create_components(config,
load_ambient_config=False)`. No watcher runs. Measured on a scratch `HOME`
with `config.json`, a `config.d` fragment and `MEMTOMEM_STORAGE__SQLITE_PATH`
all pointing elsewhere: the store's index is under root and none of the
other paths is created.

What the store still reads or writes outside root, by design:

- **The project registry** (`indexing.project_memory_dirs`), read once at
  open from the developer's `config.json`, `config.d` and `MEMTOMEM_*`, the
  way `MemtomemBaseStore` reads it (ADR B7): it decides the store's tier, is
  written into the store's own configuration so the indexer's scope guard
  agrees, and gates `project_shared` (`confirm_project_shared=True`
  required; `force_unsafe` hard-refused there). Because this read is the
  personal configuration, a malformed unrelated `MEMTOMEM_*` variable fails
  `open()` the way it fails `MemtomemBaseStore` today.
- **Embedding model caches** under `~/.memtomem/cache` when an ONNX profile
  is configured.
- **Entity backfill** (`create_components(entity_backfill=True)`) writes to
  the store's own database once, on first open of an index built by an older
  memtomem.
- **The schema peer check** (`storage/schema_peers.py`), on a schema
  migration of an existing `index.db`, enumerates memtomem's runtime instance
  registry; a fresh `index.db` takes no migration.

## Measurements

On memtomem `bb0e63a9`, macOS, Apple Silicon, 50 single-paragraph items:

| profile | `put` mean | notes |
|---|---|---|
| `embedding.provider = "none"` (default, BM25 only) | 3.2 ms | `tests/test_lcmm_latency.py` |
| E5 (`onnx` / `multilingual-e5-small`) | 39.3 ms | model already cached; `LANGCHAIN_MEMTOMEM_E5=1` runs the test |

A five-hit query search under E5 took 7 ms.

Open with a fresh root: 16 ms (`provider="none"`); reopen with 23 items: 20 ms.

Because a `put` costs a few milliseconds without an embedder, `abatch` indexes
each `PutOp` separately; batching several items into one index call is left
for a measurement that shows it is needed.

`max_chunks_per_search` default 200: a single-paragraph value is one chunk,
`long_text` in the tests (six headed sections) is six; 200 chunks therefore
cover between 33 and 200 items per query. The default is a placeholder until
a corpus-based chunk-per-item distribution is measured.
