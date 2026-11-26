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

파이썬에서 리스트의 길이를 구하는 내장 함수 이름이 뭐야? 답변 마지막 줄은 `ANSWER: <함수>` 형식으로 써 줘.
