---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:6519|`6519`)[ \t]*\s*$'
match: contains
weight: 2
---
The reply's last line is the expected `ANSWER:` line.
