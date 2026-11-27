---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[^\n]*[^-A-Za-z0-9_\n]kx-north-9(?:[^-A-Za-z0-9_\n][^\n]*)?\s*$'
match: contains
weight: 1
---
The last ANSWER: line carries the region.
