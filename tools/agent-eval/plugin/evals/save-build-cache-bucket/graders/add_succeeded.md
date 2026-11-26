---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,400}?Memory added to'
match: contains
weight: 1
---
A mem_add call succeeded: the server reported the write. A mem_add succeeded, wherever the agent put the note (`file=` included). On its own it proves only that some write happened. The `fact` grader judges what that write recorded.
