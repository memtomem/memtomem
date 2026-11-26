---
type: tool_used
tool: mcp__plugin_memtomem-agent-eval_memtomem__mem_add
input_match: '^(?=[\s\S]*(?:[Rr]eview(?:(?!English)[^,;.]){0,60}?Korean|Korean(?:(?!English)[^,;.]){0,60}?[Rr]eview))(?=[\s\S]*(?:[Cc]ode comment(?:(?!Korean)[^,;.]){0,60}?English|English(?:(?!Korean)[^,;.]){0,60}?[Cc]ode comment))'
min: 1
weight: 1
---
A mem_add call carried the stated fact: both halves in the right direction: reviews in Korean, code comments in English (a swapped pair fails). input_match is a regex on the call's JSON input.
