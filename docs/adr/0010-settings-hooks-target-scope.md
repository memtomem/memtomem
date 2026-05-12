# ADR-0010: Settings hooks target scope (user / project shared / project local)

**Status:** Accepted
**Date:** 2026-05-09
**Context:** PR #866 surfaced that settings hooks alone target user-scope
while every other context-gateway artifact targets project-scope. This
ADR records whether the asymmetry is deliberate, decides whether the
policy should change, and (if it does) defines the staged migration.

## Terminology

The `target_scope` config field this ADR proposes takes one of three
values, mapping to the three settings tiers Claude Code 2.x recognises:

| `target_scope` value | Resolved path                              | Tracked by git?       |
|----------------------|--------------------------------------------|-----------------------|
| `user`               | `~/.claude/settings.json`                  | n/a (user home)       |
| `project_shared`     | `<project>/.claude/settings.json`          | yes                   |
| `project_local`      | `<project>/.claude/settings.local.json`    | no (gitignored)       |

## Background

Every memtomem fan-out artifact except settings hooks targets
**project-scope**:

| Artifact          | Canonical                                      | Runtime target                      | Scope    |
|-------------------|------------------------------------------------|-------------------------------------|----------|
| Agents            | `<proj>/.memtomem/agents/<name>.md`            | `<proj>/.claude/agents/<name>.md`   | project  |
| Skills            | `<proj>/.memtomem/skills/<name>/SKILL.md`      | `<proj>/.claude/skills/<name>/…`    | project  |
| Commands          | `<proj>/.memtomem/commands/<name>.md`          | `<proj>/.claude/commands/<name>.md` | project  |
| **Settings hooks**| `<proj>/.memtomem/settings.json`               | **`~/.claude/settings.json`**       | **user** |

ADR-0001 §1 fixes canonical = project-scope as a deliberate principle
("Codex agents fan out to project-scope `<project>/.codex/agents/`
(symmetric with Claude/Gemini); Codex prompts remain user-scope
(`~/.codex/prompts/`, no project-scope equivalent). Both are **never**
imported — fan-out is one-way (canonical → Codex) so the canonical
entry stays the single source of truth." —
`docs/adr/0001-context-gateway-sync-policies.md:21-25`) but never
addresses the settings tile, which silently exempts itself by hardcoding
`Path.home()` in two places:

- `packages/memtomem/src/memtomem/web/routes/settings_sync.py:28-29` —
  `_claude_target()` returns `Path.home() / ".claude" / "settings.json"`.
- `packages/memtomem/src/memtomem/context/settings.py:122-123` —
  `ClaudeSettingsGenerator.target_file()` returns the same path,
  ignoring the `project_root` argument it accepts.

PR #866 cleaned the visual symptom (the hooks panel layout); this ADR
addresses the underlying policy.

The inferred (undocumented) reason the asymmetry exists today: the
bundled hook commands shown in
`docs/guides/integrations/claude-code.md:153-201` (`mm session start`,
`mm search`, `mm index`, `mm session end`) operate on `~/.memtomem/`
regardless of project, so user-scope is the natural fit for them — one
install, hooks fire across all projects. That convenience is real but
forecloses the "team adopts memtomem together, hooks committed to the
repo" workflow.

**Tier-merge assumption — sourced.** Claude Code 2.x merges hook
entries from all three settings tiers additively (user + project shared
+ project local), so a hook entry duplicated across tiers fires once
per tier. §3, §4, and Considered-rejected #2 below all rest on this
behaviour. Source: Anthropic Claude Code settings reference at
https://docs.claude.com/en/docs/claude-code/settings. If a future
revision of that doc retracts the additive merge, the decisions in this
ADR weaken to "if Claude Code merges across tiers, double-fire is
possible; otherwise the user-tier copy is dead config" — the tradeoffs
shift but the staged-flip recommendation does not.

## Decision

### 1. Add `hooks.target_scope` to the config schema

A new config field `hooks.target_scope` takes one of three string
values: `user`, `project_shared`, `project_local` (per Terminology).
**v1 default: `user`** — see §2 for why. The field is proposed by this
ADR; it is not shipped here.

### 2. v1 default = `user`, zero behaviour change for existing installs

Three reasons for keeping the default at `user` in v1:

- **Bundled hooks are user-scope-natural.** The bundled `mm session
  start` / `mm search` / `mm index` / `mm session end` commands act on
  `~/.memtomem/` regardless of which project the editor is in. Putting
  them at `~/.claude/settings.json` is the right home for the data
  they operate on.
- **Existing v0.1.x installs already populated the user tier.**
  Flipping the default would silently double-fire hooks, since Claude
  Code merges all three tiers additively (Background, "Tier-merge
  assumption"). A user with `~/.claude/settings.json` populated by an
  older `mm context sync` who runs `mm init` on a fresh checkout would
  get two `mm session start` invocations per session — silent and
  confusing.
- **Mirrors the staged-default-flip pattern.** Per the project's
  default-change conventions, opt-in flag first (default unchanged),
  default flip in a later release with CHANGELOG entry plus duplicate
  detection. §5 codifies the trigger.

### 3. Plumbing surface (informational — implemented in follow-up PRs)

Scope value resolves from two layers, both already supported by the
existing config infrastructure:

- **`hooks.target_scope` config field at the user-level config layer.**
  `~/.memtomem/config.json` REPLACE layer
  (`packages/memtomem/src/memtomem/config.py:973-976`,
  `_CONFIG_OVERRIDE_PATH`) or a `~/.memtomem/config.d/*.json` APPEND
  fragment (`:1053`, `_CONFIG_D_PATH`). This sets the *default* scope
  across all projects.
- **One-shot `--scope=…` CLI flag on `mm context sync`** for per-
  invocation override (e.g., a user whose default is `user` but wants
  `project_local` for one specific repo).

memtomem has **no `<project>/.memtomem/config.json` layer today** —
that would be a novel architectural addition. This ADR deliberately
does NOT propose introducing one. Per-project default selection (vs.
per-invocation override) is acknowledged as a future extension that
would need its own ADR for the project-config layer itself, plus
precedence rules vs. the user-level layer. Implementers of the first
follow-up PR should resist accidentally introducing the project-config
layer alongside this work.

Three call sites must respect the scope:

- `_claude_target()` — take `project_root` and the resolved scope;
  return the resolved Path.
- `ClaudeSettingsGenerator.target_file()` — same signature change.
- `ClaudeSettingsGenerator.is_available()` (`settings.py:119-120`) —
  loosen the user-`.claude` probe to "any of the three scopes
  resolves" so a user with only project-local settings doesn't see
  the tile vanish.

The Web UI hooks panel
(`packages/memtomem/src/memtomem/web/static/settings-hooks-watchdog.js:21-49`,
template `packages/memtomem/src/memtomem/web/static/index.html:758-768`)
surfaces two read-only signals only: the active scope (whatever the
user-level default plus any `--scope=` override resolved to) and a
banner when duplicate-tier memtomem-managed hooks are detected. **No
scope picker in v1** — a picker implies per-project persistence, which
would require the project-level config layer this ADR explicitly
defers. The picker is gated on the future project-config ADR. This
keeps the Web UI consistent with the CLI surface, which has only the
per-invocation `--scope=…` flag and no persistent per-project setting.

**Superseded Web note.** This ADR originally kept per-sync override out
of the Web UI. The later Context Gateway parity change made settings
routes accept `?target_scope=` with default `project_shared`, matching
skills / commands / agents Web routes. CLI users still use
`mm context sync --include=settings --scope=project_local` for a
one-shot override.

### 4. Migration

Detection-only in v1; no automatic migration. Two detection surfaces,
neither extending `mm sync-doctor` (which is deliberately scoped to
private-repo multi-device sync hygiene per
`packages/memtomem/src/memtomem/cli/sync_doctor_cmd.py:1` — hooks
coexistence is a different concern axis):

- **Sync-time warning (primary surface).** `mm context sync
  --include=settings` and the Web UI hooks panel's sync action check
  before write: if the user's effective scope differs from a tier
  where memtomem-managed hook entries already exist (matched by
  canonical signature, not literal equality), surface a warning naming
  the offending tier and pointing at the future settings-migrate
  subcommand. This fires in the user's actual workflow, not behind a
  separate command.
- **Scoped on-demand check.** A new subcommand for CI / scripting use.
  **Final name TBD** in the implementation issue — three plausible
  shapes, none of which currently exist in the CLI:
  - `mm context settings-doctor` — flat hyphenated subcommand under
    `mm context`. Closest fit to the existing
    `@context.command("migrate")` flat shape at
    `packages/memtomem/src/memtomem/cli/context_cmd.py:1383`. Lowest
    CLI surface expansion.
  - `mm context settings doctor` — nested `settings` group under
    `mm context`. Cleaner namespace if future `settings`-scoped
    subcommands appear, but introduces a new group level that doesn't
    exist today.
  - `mm doctor settings` — peer to `mm sync-doctor` under a new
    `mm doctor` top-level group. Cleanest semantically but expands
    the top-level CLI surface and creates parallel doctor commands.

  This ADR records the *responsibility* (a scoped duplicate-hooks
  diagnostic separate from `mm sync-doctor`); naming is deferred to
  the implementation issue.

### 5. Default-flip trigger

Mirror ADR-0001 §5 / ADR-0007 dwell-time precedent. The criteria split
into "features available" (events) and "soak passed" (duration), which
ADR-0001 §5.1 keeps separate by listing dwell time only on the bug-
count criterion.

Flip the `mm init` default from `user` → `project_local` when **all**
hold:

- `hooks.target_scope` config field has shipped in a tagged release.
- Sync-time duplicate-hook warning (per §4) has shipped in a tagged
  release.
- Settings-migrate subcommand has shipped in a tagged release.
- After all three above are available, no P0/P1 (or equivalent
  severity) open issues against the surface for ≥2 weeks.
  *(Verbatim from ADR-0001 §5.1.)*

The flip itself is a separate ADR-light commit (CHANGELOG + a one-line
default change), not a re-litigation of the choice.

## Consequences

- Three implementation issues are unblocked once this ADR reaches
  Accepted: (1) `hooks.target_scope` config field plus
  `_claude_target()` / `target_file()` plumbing; (2) sync-time
  duplicate-hook warning plus the scoped doctor subcommand whose name
  is deferred per §4; (3) the settings-migrate subcommand. Each ships
  as its own PR; only the default-flip waits for §5's trigger.
- The settings tile becomes the only canonical-source artifact whose
  fan-out direction is *user-selectable*. Agents/skills/commands stay
  project-scope-only — the asymmetry shifts from "hardcoded exception"
  to "documented user choice."
- `_claude_target()` and `ClaudeSettingsGenerator.target_file()` gain a
  scope-resolution responsibility. Tests must cover all three scope
  values for both call sites.
- Claude Code 2.x's additive multi-tier merge is now a load-bearing
  assumption. A user with hooks in two tiers will see them fire from
  both. The future user-guide update accompanying the implementation
  PRs must cross-reference this.

## Considered & rejected

- **Status quo, document the asymmetry as deliberate.** Rejected
  because it forecloses team-shared workflows (committed project
  hooks for a team that adopts memtomem together). Locking in user-
  scope only also cements an asymmetry that ADR-0001 §1 didn't
  anticipate, and future readers of the codebase keep re-deriving the
  exception.
- **Configurable + flip default to `project_local` immediately.**
  Rejected for v1 because existing users on v0.1.x would see their
  populated `~/.claude/settings.json` hooks suddenly stop being
  managed by `mm context sync`, while a new empty
  `<project>/.claude/settings.local.json` becomes the target —
  silent loss of management surface plus duplicate-fire risk via the
  additive merge. Staged flip per §5 avoids this.
- **Amend ADR-0001 with a new §6 instead of authoring a new ADR.**
  Rejected because ADR-0001 is `Accepted` with no amendment precedent
  in the repo (`docs/adr/0001-context-gateway-sync-policies.md` has a
  single `**Date:**` line and no "Update YYYY-MM-DD" entries). New ADR
  is the established pattern — ADR-0007 and ADR-0008 both layer onto
  ADR-0001's policies without amending it.

## Open questions for the implementation issues

Accepted directly without the RFC dwell period — the v0.1.x install
base is small enough that staged-flip + conservative defaults already
cover migration risk, and there is no meaningful pool of feedback to
wait on. The points below stay live for the implementation issues to
resolve in their own scopes; they do not block this ADR:

- The `project_shared` vs. `project_local` naming. If a clearer pair
  emerges during implementation, the rename can land alongside the
  config-field PR before any user-visible release.
- The final name of the scoped doctor subcommand (§4 lists three
  candidates). Picked in the implementation issue, not here.
- Whether the project-level config layer (`<project>/.memtomem/
  config.json`) should land in the same epic as the
  `hooks.target_scope` field or wait for its own ADR. Default posture
  per §3 is "wait"; revisit if the first follow-up PR finds it
  unavoidable.

## References

**Issues / PRs / milestones**

- Issue #867 — this ADR's source.
- PR #866 — Hooks panel layout cleanup (the fix that surfaced this).
- Tiered context gateway v2 milestone (#868 umbrella) — the broader
  3-tier model this ADR informs.

**ADRs**

- ADR-0001 §1 — canonical=project-scope policy for agents/skills/
  commands.
- ADR-0001 §5.1 — `≥2 weeks no P0/P1` readiness wording mirrored in
  §5 of this ADR.
- ADR-0007 — trigger-criteria-then-flip precedent referenced in §5.
- ADR-0009 — RFC pattern precedent for Proposed-status ADRs with
  multi-week dwell.

**External docs**

- Anthropic Claude Code settings reference —
  https://docs.claude.com/en/docs/claude-code/settings (source for the
  additive multi-tier merge claim in Background).

**Source files**

- `packages/memtomem/src/memtomem/web/routes/settings_sync.py:28-29` —
  `_claude_target()`.
- `packages/memtomem/src/memtomem/context/settings.py:119-123` —
  `ClaudeSettingsGenerator.is_available()` and `target_file()`.
- `packages/memtomem/src/memtomem/config.py:973-976` and `:1053` —
  user-level config REPLACE layer and APPEND fragment loader, the home
  for the proposed `hooks.target_scope` field.
- `packages/memtomem/src/memtomem/cli/context_cmd.py:1383` —
  `mm context migrate` shape, one of the candidate parent locations
  for the future scoped doctor subcommand whose name is deferred per
  §4.
- `packages/memtomem/src/memtomem/cli/sync_doctor_cmd.py:1` — docstring
  scope that §4 explicitly does NOT extend.
- `packages/memtomem/src/memtomem/web/static/settings-hooks-watchdog.js:21-49`
  and `…/web/static/index.html:758-768` — Web UI hooks panel insertion
  sites for the read-only scope display and duplicate-tier banner.
- `docs/guides/integrations/claude-code.md:153-201` — current hooks
  setup guide (target of future doc updates, not modified by this
  ADR).
