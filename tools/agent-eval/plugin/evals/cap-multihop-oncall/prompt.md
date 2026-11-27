---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [capability]
env:
  EVAL_MM_SEED: service-ownership
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_recall
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_read
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_list
---

Which on-call handle owns the service whose port changed in September? Name the service too. End your reply with one line of the form `ANSWER: <handle>`.
