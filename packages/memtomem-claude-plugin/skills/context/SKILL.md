---
name: context
description: Inject relevant memories as structured context for the current task. Use when starting complex work that may benefit from past decisions or notes.
argument-hint: [topic or question]
allowed-tools: mcp__memtomem__mem_search, mcp__memtomem__mem_related
---

Search for relevant memories about: $ARGUMENTS

## Instructions

1. Use `mem_search` with the topic as query (top_k=5)
2. For each high-relevance result (score > 0.5), check `mem_related` for linked memories
3. Present results grouped by namespace, showing:
   - Source file and heading
   - Tags for quick categorization
   - Full content for top 3, summary for the rest
4. If cross-references exist, show the relationship chain
5. Suggest which memories are most relevant to the current task
