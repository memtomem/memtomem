# Deferred ADR Tracker

Index of ADRs that carry an open question deferred to a later trigger or
date. A row here is **not** a summary of the ADR — it is a single-line
pointer so a maintainer can scan due dates without opening each ADR.

## Authoring rules

- Add a row when you author or merge an ADR with
  `Status: Proposed (deferred pending trigger)` (or an equivalent status
  with an explicit Open question that defers a decision).
- Keep one row per deferred question. If a single ADR defers multiple
  independent questions (e.g. Shape A vs. Shape B in ADR-0012), add one
  row per shape.
- The "Trigger / deadline" column is a one-line summary plus a pointer
  to the ADR section that carries the formal criteria. **Do not duplicate
  ADR body content here** — readers should be sent to the ADR for detail.
- When the tracking issue closes (decision recorded in a new ADR), strike
  the row out or remove it; the closing ADR's References section is the
  permanent record.

## Open deferred decisions

| ADR | Deferred question | Trigger / deadline | Tracking issue | Next review |
|-----|-------------------|--------------------|----------------|-------------|
| 0016 §"Open questions" §2 | `target_scope` → `target_tier` identifier rename | "the field name confused me when reading X" non-author signal ≥1 / **2026-08-11** (3-month window from ADR merge) | [#922](https://github.com/memtomem/memtomem/issues/922) | 2026-08-11 |
| 0012 §"Shape A" | Cross-DB memory migration — team onboarding export | Onboarding flow blocked on `scope`/`project_root` serialization OR gate plumbing on import (full criteria in ADR §"Shape A — Trigger criteria") | [#911](https://github.com/memtomem/memtomem/issues/911) | (event-driven) |
| 0012 §"Shape B" | Cross-DB memory migration — project archive | User reports `~/.memtomem/memtomem.db` size pain that existing compaction / orphan-GC remedies do not solve (full criteria in ADR §"Shape B — Trigger criteria") | [#911](https://github.com/memtomem/memtomem/issues/911) | (event-driven) |
| 0007 §"Trigger criteria" | PR-C: Namespace rename / bulk delete prod exposure (PR-A/B already shipped) | "≥ 2 prod user reports" along the rename/bulk-delete axis OR namespace rules surfacing in the onboarding flow | (none — tracked in ADR) | (event-driven) |
| 0020 §"Open questions" | Tier 3 index-file curation (`budget` trim / `index_orphan` add / `outside_root`) beyond the subtractive `missing_target`-only `--fix` | A user report (or repeated doctor findings) that the `missing_target`-only fix demonstrably cannot resolve (criteria in ADR §"Open questions") | (none — tracked in ADR) | (event-driven) |
| 0021 §"Open questions" §1 | Docs Drift Doctor — impl-path vs upstream-doc-path divergence surface | Upstream path drift causes a support issue OR a runtime-detection false-negative the mcp-clients.md-SoT conformance test does not catch (criteria in ADR §"Open questions") | (none — tracked in ADR) | (event-driven) |
| 0022 §"Open questions" §1 | Skills versioning — directory-tree snapshot model (`versions/v1/<SKILL.md + assets>`) with the same label layer (v1 covers agents + commands only) | A concrete need to freeze/rollback a skill OR user-reported parity-gap pain vs agents/commands versioning (criteria in ADR §"Open questions") | (none — tracked in ADR) | (event-driven) |
| 0027 §"Provisional decisions" D-A | in-browser wiki editor save→commit model | first dev-tier dogfooding of the now-shipped commit affordance; criteria in ADR-0027 §"Provisional decisions" | (none — tracked in ADR) | (event-driven) |
| 0027 §"Provisional decisions" D-E | wiki editor privacy posture (soft-warn vs hard-gate) | wiki gains a configured push remote; criteria in ADR-0027 §D-E | (none — tracked in ADR) | (event-driven) |
| 0026 §"Provisional decisions" D-B / D-C | P2 "Bold" re-frame — directional verb rename (D-B: Sync→Push↑ / Import→Pull↓) and status-merge to ahead/behind/in-sync (D-C) | The §Validation first-run user test clears the P2 gate: probe 5 overwrite-prediction ≥5/6 AND the status-merge keeps the create-vs-overwrite distinction (criteria in ADR-0026 §Validation) | [#1353](https://github.com/memtomem/memtomem/issues/1353) | (event-driven) |
| 0028 §"Consequences" | Widen `_DEFAULT_SYSTEM_PREFIXES` to hide `shared:<project>` buckets from default `mem_search` (as `agent-runtime:` already is) | ≥ 2 reports of `shared:<project>` buckets leaking into default (`namespace=None`) search results; gated by the default-change onboarding-doc fan-out rule | [#1477](https://github.com/memtomem/memtomem/issues/1477) | (event-driven) |
| 0028 §"Consequences" | `project=` sugar on `mem_session_start` / `mem_agent_register` (derives both the `agent_id` prefix and the `shared:<project>` bucket; optional session-bound scope) | Per-call `shared_namespace=` / explicit-bucket repetition reported as friction by a non-author, OR real usage of the flat convention accrues | [#1478](https://github.com/memtomem/memtomem/issues/1478) | (event-driven) |
| 0029 §"Revisit triggers" | First-party network MCP transport auth — adopt full OAuth 2.1 resource-server support (RFC 9728 PRM + RFC 8707), never a static token | A concrete remote / multi-tenant requirement OR MCP-client OAuth 2.1 becoming table-stakes among documented editors (criteria in ADR-0029 §"Revisit triggers") | (none — tracked in ADR) | (event-driven) |

## Adding a row

1. In the same PR that merges the deferred ADR, append a row here.
2. The Trigger column's "/ deadline" half is required if the ADR sets
   a calendar date; otherwise write "(event-driven)" in the Next review
   column.
3. If a tracking issue exists, link it. If not, the ADR section pointer
   in the Deferred question column is sufficient — but consider opening
   a tracking issue if the decision needs aggregated contributor signal
   (rather than a single trigger event).

## Signal collection

When a deferred question expects qualitative contributor signal (vs. a
crisp event trigger), maintainers can:

- Comment on the tracking issue with a quote + source link + date.
- Apply the `adr-feedback` label to any **non-tracker** PR / issue /
  discussion where the confusion surfaces, then copy the relevant quote
  to the tracking issue so signals don't get lost in closed-PR review
  threads. The tracker issue itself is the aggregation point, not a
  signal source — do not label it.

The tracking issue's body should enumerate which signal sources count
and what the adjudication rule is (see #922 for the canonical example).
