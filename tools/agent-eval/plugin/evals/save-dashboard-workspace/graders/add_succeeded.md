---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,400}?Memory added to'
match: contains
weight: 1
---
A mem_add call succeeded: the server reported the write. Together with add_with_fact this grades what the server stored, wherever the agent put the note (`file=` included).
