---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [capability]
env:
  EVAL_MM_SEED: deploy-cli
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_recall
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_read
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_list
---

Give me the command to deploy build 1234 to prod with our deploy CLI. End your reply with one line of the form `ANSWER: <command>`.
