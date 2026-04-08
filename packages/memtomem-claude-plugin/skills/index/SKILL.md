---
name: index
description: Index or re-index memory files for search. Use for initial setup or after bulk file changes.
argument-hint: [path]
allowed-tools: mcp__memtomem__mem_index, mcp__memtomem__mem_status
---

Index files at: $ARGUMENTS

If no path given, use `mem_index()` with default path.
Show results: total files, chunks indexed, duration.
If errors occur, explain and suggest fixes.
