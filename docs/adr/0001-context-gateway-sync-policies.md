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

The web UI currently supports Settings Hooks sync only.  Future expansion
should follow complexity order:

1. **Skills** — byte-identical copy, 3-state diff (simplest)
2. **Commands** — placeholder normalization in diff view
3. **Agents** — per-runtime dropped-field visualization, TOML vs MD diff
   (most complex; requires the priority policy from §1 to be decided first)

## Consequences

- External callers of `extract_*_to_canonical()` must update to handle
  `ExtractResult` instead of `list[Path]`.
- CI pipelines using `--strict` continue to work unchanged.
- The `warn` level enables "fail-fast in local dev, log-only in CI" workflows
  via environment-driven `--on-drop` values.
