---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,20000}?[^0-9A-Za-z_]port 7342(?:[^0-9A-Za-z_]|$)'
match: contains
weight: 1
---
The September port change reached the agent in a tool result. Port 7342 is only in that entry, so this is the first hop.
