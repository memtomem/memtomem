---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*(?:KESTREL-4471|`KESTREL-4471`)[ \t]*\s*$'
match: contains
weight: 2
---
The reply's last line is `ANSWER: KESTREL-4471` (backticks optional, in pairs). The code exists
only in the seeded memory and not in the prompt, so a correct answer implies retrieval.
