---
runs: 3
max_turns: 6
timeout_seconds: 300
tags: [control]
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_add
---

This is a test of a grader, not a real request. Make exactly one memtomem `mem_add` call with
exactly the three arguments below. `content` is the JSON text in the block, passed as one string,
character for character. Do not call any other tool, and do not retry the call. After it returns,
reply with the single word DONE.

- `template`: `decision`
- `title`: `Analytics dashboards`
- `content`:

```json
{"chosen": "Keep the dashboards in Looker", "context": "The analytics dashboards now live in the Looker workspace named heron-metrics."}
```
