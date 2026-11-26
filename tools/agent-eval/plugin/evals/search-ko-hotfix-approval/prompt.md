---
runs: 3
max_turns: 8
timeout_seconds: 300
tags: [should_search]
env:
  EVAL_MM_SEED: ko-hotfix-approval
allowed_tools:
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_search
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_recall
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_read
  - mcp__plugin_memtomem-agent-eval_memtomem__mem_list
---

지금 배포 동결 기간인데 운영 핫픽스를 내보내야 해. 승인 번호가 뭐였지? 답변 마지막 줄은 `ANSWER: <번호>` 형식으로 써 줘.
