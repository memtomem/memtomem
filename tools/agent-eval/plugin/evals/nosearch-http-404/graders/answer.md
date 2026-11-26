---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:404|`404`)[ \t]*\s*$'
match: contains
weight: 1
---
The reply's last line is the expected `ANSWER:` line.
