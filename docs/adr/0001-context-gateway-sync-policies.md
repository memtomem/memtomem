# ADR-0001: Context Gateway Sync Policies

**Status:** Accepted
**Date:** 2026-04-12
**Context:** Context gateway Phase 0–D implementation review

## Decision

### 1. Reverse sync runtime priority order

When importing runtime artifacts into canonical `.memtomem/` via
`extract_*_to_canonical()`, the first occurrence wins with a deterministic
traversal order:

| Artifact   | Priority (first wins)                |
|------------|--------------------------------------|
| Agents     | `.claude/agents` → `.gemini/agents`  |
| Skills     | `claude_skills` → `gemini_skills` → `codex_skills` (detector order) |
| Commands   | `.claude/commands` → `.gemini/commands` |

Codex agents fan out to project-scope `<project>/.codex/agents/` (symmetric
with Claude/Gemini); Codex prompts remain user-scope (`~/.codex/prompts/`,
no project-scope equivalent). Both are **never** imported — fan-out is
one-way (canonical → Codex) so the canonical entry stays the single source
of truth.

**Why this order:** Claude Code is the primary authoring surface in most
memtomem workflows.  Gemini CLI is experimental and Codex is
upstream-deprecated for custom prompts.  The order is explicit and
deterministic rather than timestamp-based, because mtime-based resolution
would be fragile across file systems and CI environments.

**Skip notification:** Skipped items are returned in `ExtractResult.skipped`
(list of `(name, reason)` tuples) and logged at `WARNING` level.  The CLI
displays them in yellow.  This ensures silent deduplication is never truly
silent.

### 2. `on_drop` severity levels for field conversion loss

When fanning out canonical agents/commands to runtimes, some fields are
dropped (e.g., Codex drops `tools`, `skills`, `isolation`, `kind`,
`temperature`).  The `--on-drop` option controls the severity:

| Level      | Behavior                                              |
|------------|-------------------------------------------------------|
| `ignore`   | Default. Dropped fields recorded in `result.dropped`. |
| `warn`     | Log a `WARNING` per dropped-field set.  Generation continues. |
| `error`    | Raise `StrictDropError` immediately.  No partial output. |

The legacy `--strict` flag is preserved as an alias for `--on-drop=error`.
When both are supplied, `--on-drop` takes precedence unless it is still the
default (`ignore`).

**Why three levels:** Binary strict/not-strict made `--strict` unusable with
Codex (5 of 9 fields dropped).  The `warn` level lets users see what is lost
in CI logs without blocking the pipeline.  `ignore` is the default because
most users care about the generated output, not the dropped metadata.

### 3. Phase independence

Phases 0 through D are fully independent:

- Phase 0 (`context.md` → `CLAUDE.md`, `GEMINI.md`, etc.) does not produce
  artifacts consumed by Phases 1–3 or D.
- Each `--include` kind (`skills`, `agents`, `commands`, `settings`) runs its
  own pipeline with no cross-phase data flow.
- Partial execution (e.g., `--include=skills` only) cannot cause
  inconsistency.

### 4. GUI expansion order

The web UI rolled out sync surfaces in this complexity order:

1. **Skills (Phase A)** — byte-identical copy, 3-state diff (simplest)
2. **Commands (Phase B)** — placeholder normalization in diff view
3. **Agents (Phase C)** — per-runtime dropped-field visualization,
   TOML vs MD diff (most complex; requires the priority policy from §1
   to be decided first)

Phases A–C shipped to prod in that order.  Phase D (Settings Hooks) —
the original dev-mode surface this ADR was authored against — remains
dev-only until it satisfies the readiness contract in §5.

### 5. Phase readiness criteria

A Context Gateway phase graduates from `_DEV_ONLY_ROUTERS` to
`_PROD_ROUTERS` (in `packages/memtomem/src/memtomem/web/app.py`) and
removes its `STATE.uiMode === 'dev'` UI gate(s) when **all four** hold:

1. **No P0/P1 issues open against the surface for ≥2 weeks.**  Verify
   via `gh issue list --label context-gateway,P0,P1` scoped to the
   phase.
2. **Round-trip integration test** in the Python suite — write
   canonical → diff via the read route → import back via
   `extract_*_to_canonical()`, asserting that canonical state survives
   the cycle.
3. **i18n key parity (en + ko)** verified by `tests/test_i18n.py`.
   The parity test is auto-discovery based, so adding the phase's keys
   to `en.json` + `ko.json` is sufficient — no test changes needed.
4. **Conflict path covered by a test fixture** — either HTTP 409
   (e.g., Skills' optimistic-locking via `mtime_ns`) **or** a documented
   soft-abort response (e.g., Settings' `200 + {"status": "aborted"}`
   on stale-mtime resolve).  Both shapes qualify; the requirement is
   that the conflict semantics are pinned by a test, not the specific
   status code.

**Why these four:** the round-trip test catches lossy serialization
(the most common context-gateway regression class); the i18n parity
test catches missing translations (the most common prod-only UX gap);
the conflict-path test pins optimistic-write behavior so a future
refactor cannot silently drop external-write detection.  The 2-week
dwell time mirrors ADR-0007's "prod-user feedback ≥2 reports / waiting
period" trigger and serves the same purpose.

**Procedure precedent:** ADR-0007 (Namespace CRUD prod exposure) used
trigger-criteria-then-flip with no env kill-switch; rollback was
`git revert` of the gate-removal commit.  Future phase promotions
follow the same pattern — no `*_DEV_ONLY` config knob.

**Retroactive scope:** §5 applies to *future* phase promotions.  It
does not retroactively fail Phases A–C — those shipped under earlier
review and gaps (e.g., missing 409 fixtures in B/C) are tracked as
hygiene follow-ups, not regressions.

## Consequences

- External callers of `extract_*_to_canonical()` must update to handle
  `ExtractResult` instead of `list[Path]`.
- CI pipelines using `--strict` continue to work unchanged.
- The `warn` level enables "fail-fast in local dev, log-only in CI" workflows
  via environment-driven `--on-drop` values.
- Future phase promotions (Phase D Settings Hooks → prod, and any
  subsequent phases) follow §5's four-point readiness contract.
