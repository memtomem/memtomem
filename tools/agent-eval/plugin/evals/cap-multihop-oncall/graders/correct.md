---
type: llm
weight: 2
---
You are grading an AI agent's reply to: "Which on-call handle owns the service whose port
changed in September? Name the service too."

The facts: on 2026-09-17 ledger-api moved from port 8080 to port 7342 (the September change).
On 2026-10-08 quota-svc moved from port 9000 to port 6618 (October, not September). Pages for
ledger-api go to `heron-ledger-oncall`; quota-svc to `marten-quota-oncall`; atlas-gw to
`vole-atlas-oncall`.

PASS only if the reply attributes the September port change to ledger-api AND gives
`heron-ledger-oncall` as its handle. FAIL if it names another service as the one whose port
changed in September, even when the handle given is `heron-ledger-oncall`; if it gives another
handle; if it offers more than one handle as the answer; or if it does not name the service.
