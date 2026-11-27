---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [capability]
env:
  EVAL_MM_SEED: staging-db
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_recall
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_read
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_list
---

Give me the psql command to connect to the staging database as user `deploy`, database `app`. End your reply with one line of the form `ANSWER: <command>`.
