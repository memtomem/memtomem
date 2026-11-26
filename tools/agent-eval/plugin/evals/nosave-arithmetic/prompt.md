---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [should_not_save]
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_add
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
---

Quick one: what is 17 times 23? End your reply with one line of the form `ANSWER: <number>`.
