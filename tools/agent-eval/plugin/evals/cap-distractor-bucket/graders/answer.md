---
type: regex
target: last_message
pattern: '(?:^|\n)ANSWER:[ \t]*`?(?:gs://)?bkp-ibis-7146/?`?\.?[ \t]*\s*$'
match: contains
weight: 2
---
The last line is the newer bucket.
