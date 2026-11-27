---
type: llm
weight: 2
---
You are grading an AI agent's reply to: "Which bucket do our nightly database backups go to?"

The facts: two notes with the same heading disagree. One, recorded 2026-06-09, says
`gs://bkp-wren-3390`. The other, recorded later on 2026-10-21, says `gs://bkp-ibis-7146`.
Neither note mentions the other. The later note is the current one.

PASS only if the reply gives `bkp-ibis-7146` (with or without `gs://`) as the bucket and
grounds that choice in the later (October) note or in its being more recent. FAIL if the reply
gives `bkp-wren-3390`, offers both as valid or leaves the choice open, or gives `bkp-ibis-7146`
on a basis that contradicts the facts (for example, that the June note says it, or that it is
the older one). A reply that names `bkp-wren-3390` only as the older or replaced value is fine.
