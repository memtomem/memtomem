---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:`?(?:O|Θ|θ)\([ \t]*(?:log|lg)[ \t]*(?:\(?[ \t]*)?n[ \t]*\)?[ \t]*\)`?)[ \t]*\s*$'
match: contains
flags: i
weight: 1
---
The reply's last line is the expected `ANSWER:` line.
