---
type: tool_used
tool: mcp__plugin_memtomem-agent-eval_memtomem__mem_add
input_match: '^(?=[\s\S]*heron-metrics)(?=[\s\S]*[Dd]ashboard)'
min: 1
weight: 1
---
A mem_add call carried the stated fact: the workspace name and that it holds the dashboards. input_match is a regex on the call's JSON input.
