---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,20000}?[^-A-Za-z0-9_]bkp-wren-3390(?:[^-A-Za-z0-9_]|$)'
match: contains
weight: 1
---
The older bucket reached the agent in a tool result. Only the June note names it, so a pass here means both notes were seen.
