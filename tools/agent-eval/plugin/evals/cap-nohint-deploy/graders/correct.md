---
type: llm
weight: 2
---
You are grading an AI agent's reply to: "Give me the command to deploy build 1234 to prod with
our deploy CLI."

The facts: prod deploys use `rlsctl deploy --build <build id> --env prod --region kx-north-9`,
and no other region is allowed for prod.

PASS only if the reply's final `ANSWER:` line is a single `rlsctl deploy` invocation for build
`1234`, environment `prod` and region `kx-north-9`. Flag order does not matter, and `--flag=value`
is the same as `--flag value`. FAIL if the executable, build, environment or region is wrong or
missing; if a flag is repeated with a conflicting value; or if it offers more than one command.
