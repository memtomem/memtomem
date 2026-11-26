---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:Canberra|`Canberra`)[ \t]*\s*$'
match: contains
flags: i
weight: 1
---
The reply's last line is the expected `ANSWER:` line.
