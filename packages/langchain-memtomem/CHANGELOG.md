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

## [0.1.0] — 2026-11-04

### Added

- `MemtomemPipelineStore`, a LangGraph `BaseStore` whose `search(query=...)` runs the memtomem
  pipeline over one Markdown projection per item, in an index the store owns under its root
  (ADR-0042).
