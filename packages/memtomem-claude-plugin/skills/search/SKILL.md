---
name: search
description: Search memories using semantic search. Use when user asks about past decisions, notes, or context.
argument-hint: [query]
allowed-tools: mcp__memtomem__mem_search
---

Search memories for: $ARGUMENTS

Use `mem_search` with the query. Show results concisely with source file and relevance score.
If no results, suggest broadening the query or checking `mem_status` for indexing state.
