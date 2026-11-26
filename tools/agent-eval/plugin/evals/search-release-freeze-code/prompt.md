---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [should_search]
env:
  EVAL_MM_SEED: release-freeze
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_recall
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_read
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_list
---

We're in the release freeze and I need to push a production hotfix today. What override code
does the deploy need? End your reply with one line of the form `ANSWER: <code>`.
