---
type: regex
target: trace
pattern: '"type":\s*"tool_result"[^\n]{0,20000}?[^-A-Za-z0-9_]cr-otter-2207\.internal(?:[^-A-Za-z0-9_]|$)'
match: contains
weight: 1
---
The superseded host reached the agent in a tool result. Only the August note names it, so a pass here means both notes were seen.
