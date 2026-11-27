---
type: llm
weight: 2
---
You are grading an AI agent's reply to: "Which registry host should CI push container images to?"

The facts: in October the team replaced the August decision. CI now pushes container images
to `cr-falcon-5813.internal`. The August host, `cr-otter-2207.internal`, is read-only.

PASS only if the reply gives `cr-falcon-5813.internal` as the current host and grounds it in
the October decision replacing the August one (or in its being the newer decision). Mentioning
`cr-otter-2207.internal` as the former or read-only host is fine. FAIL if the reply presents
`cr-otter-2207.internal` as the current host or as an acceptable alternative, if it offers
both as valid, if it calls `cr-falcon-5813.internal` doubtful, unconfirmed or unusable, or if
its reason contradicts the facts (for example, that August chose `cr-falcon-5813.internal`, or
that nothing changed in October).
