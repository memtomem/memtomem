---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,20000}?[^0-9A-Za-z_]port 6519(?:[^0-9A-Za-z_]|$)'
match: contains
weight: 1
---
A tool result carried the token. Assistant text is not a tool result.
