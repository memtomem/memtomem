---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:rook\-payments\-oncall|`rook\-payments\-oncall`|@rook\-payments\-oncall|`@rook\-payments\-oncall`)[ \t]*\s*$'
match: contains
weight: 2
---
The reply's last line is the expected `ANSWER:` line.
