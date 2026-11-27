---
type: llm
weight: 2
---
You are grading an AI agent's reply to: "Which registry host should CI push container images to
now, and which host did it push to before?"

The facts: in October the team replaced the August decision. CI now pushes container images
to `cr-falcon-5813.internal`. The August host, `cr-otter-2207.internal`, is read-only.

PASS only if the reply gives `cr-falcon-5813.internal` as the current host, names
`cr-otter-2207.internal` as the host CI pushed to before, and grounds the current one in the
October decision replacing the August one (or in its being the newer decision). FAIL if the
reply does not name `cr-otter-2207.internal` as the previous host (saying the previous host is
unknown or not recorded also fails), if it presents `cr-otter-2207.internal` as the current
host or as an acceptable alternative, if it calls `cr-falcon-5813.internal` doubtful,
unconfirmed or unusable, or if its reason contradicts the facts (for example, that August chose
`cr-falcon-5813.internal`, or that nothing changed in October).
