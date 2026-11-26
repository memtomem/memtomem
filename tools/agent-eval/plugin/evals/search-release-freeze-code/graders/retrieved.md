---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,4000}?KESTREL-4471'
match: contains
weight: 1
---
A tool result in the trace carried the code. Assistant text is not a tool result, and a denied
call's result does not contain the code. Every output format of mem_search, mem_recall and
mem_read counts. The only non-memtomem tool results that could carry the code come from file
tools reading the note, and the no_* graders fail those.
