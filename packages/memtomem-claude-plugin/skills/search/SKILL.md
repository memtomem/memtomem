---
name: search
description: Search indexed memtomem memories by topic. Use for past decisions, notes, findings, or project context that may exist in the memory index.
argument-hint: [query]
allowed-tools: mcp__plugin_memtomem_memtomem__mem_search, mcp__memtomem__mem_search
---

# Search memories

Use `$ARGUMENTS` as the search query.
If the request does not clearly specify the search query, ask before calling a tool.
Call `mem_search` with the requested topic and use the compact output unless machine-readable details are necessary.

Present the strongest matches concisely with their source path, heading, and relevance score. Explain that memtomem uses BM25 by default and adds dense retrieval only when embeddings are configured. If nothing matches, suggest a broader query or the status workflow; do not write or index anything automatically.
