---
name: recall
description: Inject relevant memories as structured context for the current task. Use when starting complex work that may benefit from past decisions or notes.
argument-hint: [topic or question]
allowed-tools: mcp__plugin_memtomem_memtomem__mem_search, mcp__memtomem__mem_search, mcp__plugin_memtomem_memtomem__mem_do, mcp__memtomem__mem_do
---

Search for relevant memories about: $ARGUMENTS

## Instructions

1. Use `mem_search` with the topic as query (top_k=5)
2. For each high-relevance result (score > 0.5), call `mem_do` with `action="related"` and `params={"chunk_id": <result's chunk_id>}` to find linked memories (`mem_related` is non-core; `mem_do` routes to it in the default `core` tool mode)
3. Present results grouped by namespace, showing:
   - Source file and heading
   - Tags for quick categorization
   - Full content for top 3, summary for the rest
4. If cross-references exist, show the relationship chain
5. Suggest which memories are most relevant to the current task
