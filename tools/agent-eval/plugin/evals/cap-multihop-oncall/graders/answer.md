---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*`?@?heron-ledger-oncall`?\.?[ \t]*\s*$'
match: contains
weight: 2
---
The last line is the owner's handle.
