---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*`?cr-falcon-5813\.internal`?\.?[ \t]*\s*$'
match: contains
weight: 2
---
The last line is the current host.
