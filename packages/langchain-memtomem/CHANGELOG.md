# Changelog

All notable changes to `langchain-memtomem` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- BM25 no longer matches every item for a query whose words match no value but name a word of
  the store's path: its root (`plane-store` for `plane`), or `projections` and `md`, which every
  store's paths contain (#3205). memtomem's BM25 leg falls back to matching a row's path when no
  content matches, and every projection path shares those words, so without an embedder (or in
  `bm25` mode) every item came back, scored as a real match at its rank would be. memtomem 0.7.9
  narrows that fallback to the file's name (#3208), which for the store is the item's encoded
  key. A key can encode to a word (`V` to `sky`), so the store also turns the fallback off, and
  such a query returns `[]` there. Dense retrieval is unchanged: in `hybrid` or `dense` mode with
  an embedder, any query still returns the nearest items. Needs
  memtomem 0.7.9, which adds the `create_components` keyword the store passes; the dependency
  floor rises with that release.
- With bounded chunking (on for an E5 embedding model), the words `md`, `file` and `fragment` and
  an item's encoded key no longer match items through text the store did not write (#3209).
  memtomem's chunker wrote `File: <encoded key>.md` and `Fragment <i>/<n>` lines into each row's
  embedded and BM25-indexed text, so in BM25 the first three matched every item and the encoded
  key matched its item. The store now opens its components with `chunk_source_labels=False`,
  which leaves both lines out. Rows indexed before the upgrade keep them until their item is
  written again. Needs memtomem 0.7.9 as well.

## [0.2.0] — 2026-11-10

### Added

- `MemtomemPipelineStore(retrieval={"mode": "hybrid" | "bm25" | "dense", "weights": [bm25, dense],
  "rrf_k": 60})` chooses which retrievers a query search runs, with the keys and refusals of
  memtomem's `MemtomemHybridStore`. `"dense"` without an embedder is refused at open, and a
  `"dense"` query whose dense leg cannot run raises the new `DenseSearchUnavailable` instead of
  returning nothing. Without `retrieval`, a store searches as 0.1.0 did.

## [0.1.0] — 2026-11-04

### Added

- `MemtomemPipelineStore`, a LangGraph `BaseStore` whose `search(query=...)` runs the memtomem
  pipeline over one Markdown projection per item, in an index the store owns under its root
  (ADR-0042).
