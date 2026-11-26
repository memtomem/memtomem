---
type: tool_used
tool: mcp__plugin_memtomem-agent-eval_memtomem__mem_add
input_match: '"file"\s*:\s*"'
min: 0
max: 0
weight: 1
---
No mem_add named its own file (a null file is fine). The note-file graders read today's day file only, so a write elsewhere could hide what was persisted. A false FAIL here is possible (a refused call that named a file), a false PASS is not.
