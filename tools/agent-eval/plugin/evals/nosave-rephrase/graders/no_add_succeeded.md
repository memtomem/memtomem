---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,400}?Memory added to'
match: not_contains
weight: 1
---
No mem_add succeeded, wherever it would have written.
