# Changelog

All notable changes to `langchain-memtomem` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] — 2026-11-10

### Added

- `MemtomemPipelineStore(retrieval={"mode": "hybrid" | "bm25" | "dense", "weights": [bm25, dense],
  "rrf_k": 60})` chooses which retrievers a query search runs, with the keys and refusals of
  memtomem's `MemtomemHybridStore`. `"dense"` without an embedder is refused at open, and a
  `"dense"` query whose dense leg cannot run raises the new `DenseSearchUnavailable` instead of
  returning nothing. Without `retrieval`, a store searches as 0.1.0 did.

### Fixed

- BM25 no longer matches every item for a query whose words match no value but name a word of
  the store's path: its root (`plane-store` for `plane`), or `projections` and `md`, which every
  store's paths contain (#3205). memtomem's BM25 leg falls back to matching a row's path when no
  content matches, and every projection path shares those words, so without an embedder (or in
  `bm25` mode) every item came back, scored as a real match at its rank would be. The store now
  turns that fallback off, so such a query returns `[]` there. Dense retrieval is unchanged: in
  `hybrid` or `dense` mode with an embedder, any query still returns the nearest items. With
  bounded chunking (on for an E5 embedding model) `md`, `file`, `fragment` and an item's encoded
  key still match, through the chunker's `File:` and `Fragment` lines (#3209). Needs
  memtomem 0.7.9, which adds the `create_components` keyword the store passes; the dependency
  floor rises with that release.

## [0.1.0] — 2026-11-04

### Added

- `MemtomemPipelineStore`, a LangGraph `BaseStore` whose `search(query=...)` runs the memtomem
  pipeline over one Markdown projection per item, in an index the store owns under its root
  (ADR-0042).
