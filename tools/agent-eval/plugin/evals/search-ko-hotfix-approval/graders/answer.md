---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:ORCA\-2290|`ORCA\-2290`)[ \t]*\s*$'
match: contains
weight: 2
---
The reply's last line is the expected `ANSWER:` line.
