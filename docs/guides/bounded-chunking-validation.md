# Bounded chunking validation — 2026-09-08

The common file-indexing path now applies index-only masking, exact token limits,
and structural descriptions. The installed CLI and primary Core were verified
with the existing configuration.

## Issues addressed

| Issue | Resolution |
|---|---|
| Duplicate JSON keys could lose earlier values | Fall back to lossless source splitting |
| Line references could drift after whitespace packing | Generate positions and fragment numbers after packing |
| Repeated tokenization of the entire remaining body | Bound candidate windows and validate final token counts |
| Illustrative passwords and empty environment assignments were blocked | Mask identifiable values in the indexed copy; preserve explicit empty assignments |
| Editing masked chunks could overwrite original source | Reject MCP/web chunk edits and direct users to edit the source |
| Description caches retained older source generations | Replace each source generation, cap it at 256 entries, and clear it on deletion |
| Partial hot reload could mix chunk policies | Validate restart-required settings before publishing configuration |
| Reprocessing depended on temporary scripts | Add a generic migration tool with hash checks and atomic completion receipts |
| Set ordering caused inconsistent configuration hashes | Canonicalize sets and test across process hash seeds |

Existing scanner patterns, explicit exemption auditing, and shared-scope guards
remain in place. Ambiguous values and specific credential patterns still reach
the guard. Optional LLM enrichment remains disabled. Only the indexed copy is
masked; source files are preserved. This does not anonymize arbitrary PII.

## Scanner and chunk examples

Two illustrative values in comments in `nbrun.py` were handled consistently:

```text
password: required         -> password: [REDACTED]
"password": "secret"       -> "password": "[REDACTED]"
OPENAI_API_KEY=             -> unchanged empty assignment
```

The actual chunk containing the masked examples has a 1,373-token body and a
28-token description:

```text
Index masking: 2 source values masked.
nbrun
File: nbrun.py
Lines: 1-67; fragment 1/2
```

Fragment numbers are relative to the same symbol hierarchy. `redaction_count`
is the source-level count repeated on each chunk, not a per-chunk count.

## Local validation snapshot

- 1,936 targeted tests passed; Ruff lint and mypy over 361 source files passed.
- 148 sources were reprocessed into 2,018 chunks with no migration errors.
- Whole database: 9,828 chunks; maximum body/context/composed input lengths were 4,096/409/4,295 tokens.
- Zero budget violations, missing vectors, or missing FTS rows; SQLite check returned `ok`.
- Source hashes and namespaces, scopes, tags, validity, and provenance were preserved for all 148 sources.
- Installed CLI canary: five chunks, maximum body 4,096 tokens, masking and source preservation confirmed; test files and rows were removed.
- All 361 installed Python source files matched the implementation at the time of deployment.

These measurements precede the CI follow-up changes. Initial CI additionally
identified formatting differences, a missing tokenizer extras probe, and two
SQL construction warnings. The follow-up formats the affected files, adds the
probe, and uses static SQL without weakening the security baseline.

## Deployment and restart scope

The validated wheel was installed into an immutable release directory and the
existing console launchers were switched atomically. Existing processes kept
their package files. Dependencies, the uv installation receipt, and Core/STM
configuration files were preserved. Launcher and database backups were retained.

Deployed wheel SHA-256: `53e0b6d5cbc2fe4ece89639aac646292e3e6ff8bdd427e08df098ce3fff61db6`.

The primary Core reconnected as PID 90022 and served a live search. Its STM parent,
PID 84046, was preserved. Primary Core RSS was 13.30 GiB before restart and
2.22 GiB after the final search check. These are snapshots under different
workloads, not evidence of long-term leak resolution. The final process snapshot
contained no zombies.

Twelve other pre-existing Core processes were preserved to avoid disrupting
active sessions. Their clients must reconnect to load the new installation.
`uv run mm` in another checkout uses that checkout's code independently.
The local deployment snapshot does not establish remote CI success.

## Deferred Base64 handling

Automatic Base64 detection/filtering, image extraction, and summarization remain
deferred. The previously reviewed Base64 chunk and sources removed by exclusion
rules were confirmed absent from the database. The Base64 source was omitted
from this migration and its original file preserved. A future source change can
make it eligible for indexing again; a persistent Base64 ingestion policy has
not been implemented.
