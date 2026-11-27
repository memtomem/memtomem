---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [should_not_search]
env:
  EVAL_MM_SEED: team-lunch
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_recall
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_read
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_list
---

What is the capital city of Australia? End your reply with one line of the form `ANSWER: <city>`.
