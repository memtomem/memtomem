---
name: memtomem-search
description: Search indexed memtomem memories by topic. Use for past decisions, notes, findings, or project context that may exist in the memory index.
---

# Search memories

Derive the search query from the current user request.
If it does not provide a usable search query, ask before calling a tool.
Call `mem_search` with the requested topic and use the compact output unless machine-readable details are necessary.

Present the strongest matches concisely with their source path, heading, and relevance score. Explain that memtomem uses BM25 by default and adds dense retrieval only when embeddings are configured. If nothing matches, suggest a broader query or the status workflow; do not write or index anything automatically.
