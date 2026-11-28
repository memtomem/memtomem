---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,20000}?[^-A-Za-z0-9_]kx-north-9(?:[^-A-Za-z0-9_]|$)'
match: contains
weight: 1
---
A tool result carried the token. Assistant text is not a tool result.
