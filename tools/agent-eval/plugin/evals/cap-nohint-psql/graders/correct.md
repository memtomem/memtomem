---
type: llm
weight: 2
---
You are grading an AI agent's reply to: "Give me the psql command to connect to the staging
database as user `deploy`, database `app`."

The facts: staging Postgres is at host `staging-db.internal`, port `6519`. Port 5432 is closed.

PASS only if the reply's final `ANSWER:` line is a single `psql` invocation that would connect
to host `staging-db.internal` on port 6519 as user `deploy` to database `app`. Any equivalent
form passes: flags (`-h`, `-p`, `-U`, `-d` or their long forms), a `key=value` connection
string, or a `postgresql://` URI. Environment variables set on the same line count. FAIL if the
host, port, user or database is wrong, missing or given twice with conflicting values; if the
line is not a psql invocation (for example `echo ...`); or if it offers more than one command.
A password prompt or a placeholder for the password is fine.
