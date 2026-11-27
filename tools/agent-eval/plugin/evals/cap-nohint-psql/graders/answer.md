---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[^\n]*(?:[ \t]-p|[^0-9A-Za-z_\n])6519(?:[^0-9A-Za-z_\n][^\n]*)?\s*$'
match: contains
weight: 1
---
The last ANSWER: line carries the port.
