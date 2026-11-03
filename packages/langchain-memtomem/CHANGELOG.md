# Changelog

All notable changes to `langchain-memtomem` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] — 2026-11-04

### Added

- `MemtomemPipelineStore`, a LangGraph `BaseStore` whose `search(query=...)` runs the memtomem
  pipeline over one Markdown projection per item, in an index the store owns under its root
  (ADR-0042).
