---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,4000}?6519'
match: contains
weight: 1
---
A tool result in the trace carried the token. Assistant text is not a tool result, a denied call's result does not contain it, and the no_* graders fail file tools.
