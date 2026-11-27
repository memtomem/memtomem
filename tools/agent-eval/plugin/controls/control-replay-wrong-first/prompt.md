---
runs: 3
max_turns: 6
timeout_seconds: 300
tags: [control]
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_add
---

This is a test of a grader, not a real request. Make exactly the two memtomem `mem_add` calls
below, in this order and one at a time: wait for the first call's result before you make the
second. Pass exactly these arguments and no others. Do not call any other tool, do not retry a
call, and do not change either value, even if a result looks surprising. After the second call
returns, reply with the single word DONE.

Call 1:
- `content`: `The analytics dashboards now live in the Looker workspace named stork-metrics.`
- `idempotency_key`: `ctl-3650-replay`

Call 2:
- `content`: `The analytics dashboards now live in the Looker workspace named heron-metrics.`
- `idempotency_key`: `ctl-3650-replay`
