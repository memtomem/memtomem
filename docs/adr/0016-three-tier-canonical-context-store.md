# ADR-0016: Three-tier canonical context store (epic #868 umbrella)

**Status:** Accepted
**Date:** 2026-05-11
**Context:** Epic issue #868 (`Tiered context gateway v2`) calls for a
"uniform tiered model for the entire context gateway." Most of that
model already exists in tree as three separate ADRs that landed on
different days for different surfaces — ADR-0010 (settings hooks),
ADR-0011 (memory / agents / skills / commands canonical), ADR-0015
(Web layer scope vocabulary). What is missing is (a) a single
authoritative roll-up of what the three-tier model means across all
artifact types, and (b) an explicit decoupling of two ideas that
ADR-0001 §1 left implicitly fused: **where canonical context resides**
and **where runtime context is materialized**. This ADR provides both.

ADR-0016 does not introduce a new three-tier model from scratch. It
consolidates ADR-0010, ADR-0011, and ADR-0015, and separates two
concepts that ADR-0001 left implicitly coupled: where canonical
context resides, and where runtime context is materialized.

## Terminology

This ADR uses two qualified terms to talk about tier and fan-out
separately. The implementation-level identifier `target_scope`
continues to exist (see §"Backward compatibility"); the qualified
terms below are the ones new ADRs, new docs, and new request shapes
should reach for.

| Term                  | Dimension                                                                   | Where it lives today                                                                                       |
|-----------------------|-----------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------|
| **tier**              | Canonical residency / ownership / quota / write target                      | `TargetScope` literal at `config.py:745` (`Literal["user", "project_shared", "project_local"]`)            |
| **runtime scope**     | Fan-out direction / runtime materialization target per tier                 | `RUNTIME_FANOUT_TABLE` (`context/_runtime_targets.py:71`); ADR-0011 §1 table                               |
| **project_scope_id**  | Per-request project-root selector (Web only; unchanged from ADR-0015)        | `resolve_scope_root` (`web/routes/context_projects.py:67`)                                                  |

Working definitions, kept short for cross-ADR citation:

> **scope** describes where context is synchronized or materialized
> for runtime use. **tier** describes where canonical context is
> owned, stored, quota-managed, and written.

`project_shared` continues to mean "git-tracked," **not** "shared
between agents" (inherited verbatim from ADR-0010 / ADR-0011 / ADR-0015
to keep one vocabulary across the codebase).

## Background

### What is already decided

- **ADR-0010 (Accepted 2026-05-09)** — settings hooks gain a 3-tier
  `target_scope`; v1 default `user`; staged-flip trigger in §5.
- **ADR-0011 (Proposed 2026-05-09)** — memory / agents / skills /
  commands canonical extend the same 3-tier axis; `project_local`
  for non-memory artifacts is a draft tier with **no runtime
  fan-out** (§3); v1 defaults preserve current behaviour; ADR-0011
  §7 supersedes ADR-0001 §1's "canonical = project-scope only"
  default in *availability* but not in *defaults*. PR-A through PR-E
  shipped 2026-05-09…11; PR-F (Web UI + docs) pending.
- **ADR-0015 (Accepted 2026-05-11)** — Web layer disambiguates
  `project_scope_id` (per-request project-root selector) from
  `target_scope` (canonical artifact tier); list / sync / overview
  route ownership decided per §2; sync routes resolve `target_scope`
  per-request, not from `config.hooks.target_scope` (§4c).

### What ADR-0001 §1 left implicitly coupled

ADR-0001 §1 fixed two principles for agents / skills / commands:
deterministic reverse-sync runtime priority, and **one-way fan-out
(canonical → runtime)** with canonical living at project scope.
"Canonical lives at project scope" was the only tier value available
at the time, so §1 had no occasion to name the dimension. As a
side-effect, "where the canonical lives" and "where fan-out targets"
ended up referenced through one and the same word ("project-scope"),
which meant any reader of ADR-0001 §1 inherited an implicit 1:1
coupling between residency and runtime materialization.

ADR-0011 §1's per-artifact table already broke that 1:1 in practice
— `project_local` canonical exists with **no runtime fan-out**, and
`user`-tier canonical for agents / skills / commands fans out to a
*different* path (`~/.claude/agents/`) than `project_shared` does
(`<proj>/.claude/agents/`). The implicit coupling is gone in the
code; this ADR retires it in the vocabulary.

### Why now

Two specific forces make the umbrella ADR worth landing rather than
deferring further:

1. **Sub-issue filing for #868 is the next planning step.** The
   epic body lists eleven sub-issues (detector, fan-out generators,
   reverse sync, hooks tier landing, Web UI, CLI, migration tooling,
   docs, tests). Without a fixed vocabulary, each sub-issue will
   independently reach for "tier" / "scope" / "target" in whichever
   sense its author happens to mean.
2. **`tier` ↔ `runtime scope` decoupling is load-bearing for the
   pending sub-issues.** Web UI tier badges (PR-F), CLI tier filters,
   and the eventual migration tooling all need to refer to the
   canonical-residency dimension without dragging fan-out semantics
   in. ADR-0011 §1's table covers the per-artifact mapping; what is
   missing is the cross-artifact vocabulary the sub-issues will cite.

## Decision

### 1. ADR-0016 is the umbrella

`Tiered context gateway v2` (#868) is the union of the decisions
already recorded by ADR-0010, ADR-0011, and ADR-0015 — not an
additional twelfth ADR worth of new policy. New sub-issues file
against ADR-0010 / ADR-0011 / ADR-0015 for their respective surfaces
and cite this ADR for the umbrella vocabulary; they do not reopen
the decisions already made in those three ADRs.

### 2. Conceptual split: tier ≠ runtime scope

The two dimensions are tracked independently from this ADR onwards:

- **tier** — canonical residency. Answers: "where does the canonical
  file live? who owns it? which quota does it count against? where
  does a canonical write land?" Values: `user` / `project_shared` /
  `project_local` (mirroring ADR-0010 / ADR-0011).
- **runtime scope** — fan-out direction per tier per artifact.
  Answers: "given a canonical at tier T, where does `mm context
  sync` materialize it?" The answer is not always one path — for
  agents / skills / commands, `user`-tier canonical fans out to
  `~/.claude/<artifact>/`, `project_shared`-tier canonical fans out
  to `<proj>/.claude/<artifact>/`, and `project_local`-tier canonical
  fans out to **nothing** (ADR-0011 §3). The mapping table is
  `RUNTIME_FANOUT_TABLE` at `context/_runtime_targets.py:71`;
  ADR-0011 §1 holds the per-artifact reading of it.

The two dimensions are **not 1:1**. ADR-0011 §3 already exercises a
zero-to-one shape (`project_local` canonical for agents / skills /
commands has no runtime fan-out). A future artifact type may exercise
the one-to-many case (e.g., a `user`-tier canonical that materializes
into both `~/.claude/` and `~/.codex/` for cross-runtime agents). The
vocabulary is built to allow that without rewording.

**Settings is the artifact where the two dimensions invert.** memtomem's
settings canonical is single — `<proj>/.memtomem/settings.json`,
regardless of tier (per ADR-0010 §Background's canonical/runtime
table). ADR-0010's `target_scope` axis on settings hooks selects the
**runtime fan-out target** (`~/.claude/settings.json` /
`<proj>/.claude/settings.json` / `<proj>/.claude/settings.local.json`),
not the canonical residency. The vocabulary still holds — there is
exactly one canonical tier for settings, and three runtime scopes
selected by `target_scope` — but the reader should not infer from
ADR-0011 §1's settings row that those `~/.claude/...` paths are
canonical. They are resolved runtime targets, and ADR-0011 §1's row
is internally inconsistent on this point (flagged as a one-line
cleanup follow-up; tracked in §"Open questions").

### 3. Tier values and defaults — pinned

The three tier values are inherited unchanged from ADR-0010 and
ADR-0011; this ADR does not redefine them. What the tier *selects*,
however, differs by artifact type — §2 names the split, this section
shows it concretely with two tables.

**Memory / agents / skills / commands** — tier selects **canonical
residency** (per ADR-0011 §1):

| `tier`            | Canonical path                                                                                                  |
|-------------------|-----------------------------------------------------------------------------------------------------------------|
| `user`            | `~/.memtomem/<artifact>/...`                                                                                    |
| `project_shared`  | `<proj>/.memtomem/<artifact>/...`                                                                               |
| `project_local`   | `<proj>/.memtomem/<artifact>.local/...` (no runtime fan-out for agents / skills / commands, per ADR-0011 §3)    |

**Settings (special case per §2)** — canonical is single at
`<proj>/.memtomem/settings.json`; tier (`config.hooks.target_scope`,
per ADR-0010 §Terminology) selects the **runtime fan-out target**:

| `tier`            | Runtime fan-out target                            |
|-------------------|---------------------------------------------------|
| `user`            | `~/.claude/settings.json`                         |
| `project_shared`  | `<proj>/.claude/settings.json`                    |
| `project_local`   | `<proj>/.claude/settings.local.json`              |

Settings has exactly one canonical residency (project-shared by
construction, regardless of the configured `target_scope`); the
three values of `target_scope` index the runtime side. For
memory / agents / skills / commands, the canonical and runtime
sides are coupled via `RUNTIME_FANOUT_TABLE` per ADR-0011 §1.

v1 defaults preserved unchanged from ADR-0010 §2 (settings:
`target_scope=user` → runtime at `~/.claude/settings.json`) and
ADR-0011 §2 (memory canonical: `user`; agents / skills / commands
canonical: `project_shared`). ADR-0016 introduces **no default flips**;
ADR-0011 §8 already commits to "no default flip, ever" for memory /
agents / skills / commands, and ADR-0010 §5's settings-tier flip
trigger is its own ADR's concern.

### 4. Read merge order (cross-tier reads)

Reads that span tiers follow one consistent precedence rule:

> `project_local` > `project_shared` > `user`

This is the same order ADR-0011 §6 pins for memory same-relevance
tie-breaks, and it mirrors Claude Code 2.x's documented additive
tier-merge order for settings / agents / skills / commands.

Which surfaces actually perform a cross-tier read by default is **not**
uniform; the precedence rule above applies only when the surface has
chosen to span tiers. Three clarifications worth recording here:

- **CLI and runtime spans by default.** `mm context list`, `mm mem
  search` without an explicit `--scope`, and the Claude Code runtime
  load span tiers by default and rank under the precedence rule above
  (the runtime via Claude Code 2.x's tier-merge; CLI / memory via
  ADR-0011 §6).
- **Web defaults are single-tier, not cross-tier.** ADR-0015 §4a / §4f
  pins the Web overview and list views to **hide `project_local`** and
  **default `?target_scope=` to `project_shared`** when omitted.
  ADR-0016 does not relax those defaults. The precedence rule applies
  on the Web side only when a request explicitly opts into a wider
  view (`?target_scope=project_local`, or a future "all tiers"
  affordance if one is added). Without that opt-in, the Web read is
  single-tier and the rule does not fire.
- **Memory cross-tier reads are project-aware, not naively
  additive.** ADR-0011 §6 already pins this: when a project context
  is detected, default search returns `scope = 'user' OR
  project_root = <X>`; cross-project shared/local rows are excluded.
  ADR-0016 does not relax that — "project_local > project_shared >
  user" is the *within-project* precedence rule, not a
  cross-project union directive.
- **Runtime tier-merge is load-bearing.** Claude Code 2.x's
  additive merge of user-tier + project-tier runtime entries (for
  agents / skills / commands; same for settings) is the mechanism
  by which fanned-out canonical entries from two tiers both load.
  Caveat inherited from ADR-0010 / ADR-0011: if a future Claude
  Code revision retracts the merge, "user copy is dead config" —
  the tradeoff shifts but the staged conservative defaults in §3
  do not.

### 5. Write target rule

Writes land in **exactly one tier per invocation**. Two orthogonal
resolutions happen on every write — tier resolution and (when the
chosen tier needs one) project-root resolution. They are kept
separate because conflating them was the §"Background" mistake
ADR-0001 §1 left implicit.

**Tier resolution.** Tier is resolved per artifact type, in this
priority order:

1. Explicit caller argument:
   - CLI: `--scope=<tier>` (ADR-0011 PR-D / PR-E).
   - MCP: `scope=<tier>` kwarg (ADR-0011 §5 Gate B for memory; the
     parallel context tool surface inherits the same kwarg).
   - Web: `?target_scope=<tier>` query param (ADR-0015 §4b for list
     routes, §4c for sync routes). Web does **not** spell the
     argument `?scope=` — that name is reserved for the existing
     `?scope_id=` / `?project_scope_id=` alias pair per ADR-0015 §5.
2. v1 default for the artifact type (settings → `user`; memory →
   `user`; agents / skills / commands → `project_shared`). Settings
   alone source this default from `config.hooks.target_scope`; every
   other artifact resolves the default per-request, not from config
   (ADR-0015 §4c records why sync deliberately diverges from
   settings on the source-of-default axis).

**Project-root resolution.** Only fires when the chosen tier is
`project_shared` or `project_local`; `user`-tier writes do not need
a project root. The resolution rule:

- Web: `?project_scope_id=` (with `?scope_id=` accepted as the
  ADR-0015 §5 alias) when present; otherwise the server cwd.
  Mutator routes other than `sync` stay cwd-locked per ADR-0015
  §4d.
- CLI / MCP: the caller's cwd, walked up to the nearest
  `<project>/.memtomem/` ancestor (the existing
  `resolve_scope_root` / `categorize_memory_dir` machinery).

Tier resolution and project-root resolution do not interact —
choosing a tier never selects a project root, and choosing a
project root never overrides the tier resolution.

There is **no** config-field default for the write tier outside of
`config.hooks.target_scope` (settings only). ADR-0011 §5 records
the explicit decision to refuse a `MemoryConfig.default_write_scope`
field; ADR-0015 §4c records the parallel decision for Web sync.
ADR-0016 reaffirms both: a future contributor introducing such a
field must reopen one of those ADRs, not this one.

### 6. Conflict resolution

Within a single tier, conflicts on artifact name are an error
(`mm context list` highlights; `mm context migrate --to <tier>`
refuses to overwrite without `--force`). Across tiers, the read
merge order (§4) decides which entry wins at read time; both
entries remain visible to `mm context list`. ADR-0011 §"Open
questions" item 2 owns the user-facing warning copy for the
cross-tier collision case.

For settings, ADR-0010's documented Claude Code tier-merge handles
duplicate keys additively (one hook entry per tier fires per tier);
"conflict" in the ADR-0016 sense does not apply.

For memory, the SQLite unique index `(namespace, source_file,
content_hash, start_line)` already disambiguates per ADR-0011 §4;
two chunks with identical content but different `(scope,
project_root)` columns are not conflicts, they are distinct chunks
by design.

### 7. CLI / Web UI user-facing names

User-facing surfaces use **the same three tokens** the
implementation does — `user`, `project_shared`, `project_local` —
not localized or alias-renamed variants. Rationale: the tokens
appear in `--scope=` flags, `?target_scope=` query params, config
files, and runtime path segments; introducing display aliases
("Personal" / "Team" / "Local Draft") would force every doc and
error message to disambiguate which language layer it speaks. The
tokens are stable across CLI, MCP, Web, config, and docs.

The one user-facing affordance ADR-0016 does pin: any surface
that renders `project_local` for agents / skills / commands
**must** annotate "no runtime fan-out" (or equivalent) inline. The
CLI list output already does this per ADR-0011 §"Consequences"
(`(draft, no fan-out)`); the Web UI's PR-F badges inherit the same
rule.

### 8. Relationship to ADR-0015 — tier and runtime scope are not 1:1

ADR-0015 §1 already established `target_scope` (canonical tier) as
distinct from `project_scope_id` (project-root selector). ADR-0016
adds one further distinction the Web layer alone did not need to
make: the canonical tier and the runtime fan-out scope are not
required to be 1:1. ADR-0011 §3 already exercises this — a
`project_local` canonical has *no* runtime scope, which is a
zero-to-one mapping. Future artifact types may exercise the
one-to-many case (one tier, multiple fan-out paths).

For Web request vocabulary, `target_scope` continues to denote the
canonical tier (per ADR-0015), and there is no plan to introduce a
separate `?runtime_scope=` param — fan-out direction is determined
by the tier and the artifact type, not by the caller.

### 9. Relationship to ADR-0001 §1 — supersession scope

ADR-0011 §7 already supersedes ADR-0001 §1's "canonical =
project-scope only" default in *availability* (new scopes added)
while preserving it in *defaults* (agents / skills / commands still
default to `project_shared`). ADR-0016 narrows the supersession
further by separating the two ideas ADR-0001 §1 fused:

- "Fan-out is one-way (canonical → runtime)" — **kept intact**.
  Three-tier canonical does not change directionality; ADR-0011
  PR-E preserves one-way fan-out across all tiers.
- "Canonical lives at project scope" — **retired as an implicit
  default**. The default for agents / skills / commands is
  `project_shared`, but the system supports `user` and
  `project_local` as first-class tiers.

ADR-0001 stays in place as historical context; its body is not
amended in this ADR. A separate ADR (call it ADR-0001 amendment
or ADR-NEW; sub-issue filing in §"Open questions" decides) carries
the formal §1 amendment if one is judged necessary after PR-F
ships.

### 10. #867 — closed by ADR-0010

The original "hooks user-scope asymmetry" issue (#867) is **closed,
absorbed by ADR-0010**. ADR-0016 does not reopen the asymmetry, and
sub-issues filed against #868 should not list #867 as a pending
near-term patch. The asymmetry described in #867's body is the
exact one ADR-0010 fixed with `target_scope: user / project_shared
/ project_local` for settings hooks.

## Backward compatibility

- The implementation-level identifier `target_scope` (at
  `config.py:721/745`) is **not** renamed by this ADR. New ADRs and
  new docs use the qualified "tier" / "runtime scope" terms; the
  field name stays as-is to avoid surface-level churn that would
  outweigh the vocabulary benefit. A future ADR may revisit
  renaming `target_scope` → `target_tier` once the umbrella ADR has
  bedded in.
- ADR-0015's permanent alias rules (`?scope_id=` →
  `?project_scope_id=`; response field doubling) are unchanged.
- CLI flag `--scope=<tier>` and MCP `scope=<tier>` argument names
  continue to read as "scope" rather than "tier" for the same
  back-compat reason; new flags added by future ADRs may pick
  `--tier=` if a fresh surface is being introduced.

## Consequences

- **Sub-issue filing for #868 has a vocabulary anchor.** Each
  sub-issue cites ADR-0010 / ADR-0011 / ADR-0015 for its surface
  and ADR-0016 for the cross-surface terms.
- **ADR-0001 §1's implicit residency/fan-out coupling is retired.**
  Readers of ADR-0001 §1 are pointed here for the disambiguation;
  the §1 body itself is not amended.
- **No new defaults, no new flips, no new code.** ADR-0016 is a
  documentation roll-up. Acceptance ships markdown only.
- **`target_scope` survives unchanged at the identifier level.**
  Implementers do not need to grep-rename anything to comply with
  this ADR. The qualified "tier" / "runtime scope" terms are
  doc-and-ADR vocabulary, not code identifiers.
- **#867 stays closed.** Sub-issue filing against #868 does not
  reopen it, and the epic body's "hooks tier landing" sub-bullet
  is satisfied by ADR-0010 / ADR-0011 — no separate hooks-specific
  ADR is needed.

## Considered & rejected

- **Rename `target_scope` → `target_tier` in this ADR.** Rejected
  per §"Backward compatibility": surface churn is large (config
  field, CLI flag, MCP arg, query param, docs, tests, migration),
  the immediate gain over reading the ADR-level vocabulary as a
  documentation layer is small, and ADR-0016's primary job is
  consolidation rather than mass rename. A follow-up ADR can carry
  the rename if it is judged worth the churn after PR-F lands.
- **Introduce `tier` as a new config field separate from
  `target_scope`.** Rejected for the same reason ADR-0015 §3
  refused a separate `config.context.target_scope`: divergence risk
  with no driving user need. One field, one source of truth.
- **Author ADR-0001 §1 amendment in the same PR as ADR-0016.**
  Rejected because amending ADR-0001 has no precedent in this repo
  (ADR-0007 / ADR-0008 / ADR-0010 / ADR-0011 all layered onto
  ADR-0001 without amending it). If a formal amendment is judged
  necessary, it ships as a separate ADR after PR-F.
- **File the eleven #868 sub-issues alongside this ADR.** Rejected
  for the reason the user request that prompted this ADR
  identified: sub-issues authored before the umbrella ADR lands
  drift toward independent vocabularies. Sub-issue filing happens
  after this ADR merges (see Open questions).
- **Pin a "runtime scope" type literal in `config.py`.** Rejected;
  `RUNTIME_FANOUT_TABLE` already encodes the per-artifact mapping,
  and a parallel type literal would duplicate the table without
  serving a typed call site. If a future Web or CLI surface needs
  to accept a `runtime_scope=` arg, that ADR introduces the type
  literal then.

## Open questions for the sub-issues

These follow-ups inherit the vocabulary fixed by this ADR; they do
not block ADR acceptance.

- **Sub-issue split for #868.** Filing approach: one issue per
  ADR-0011 PR-F deliverable (Web UI tier badges; CLI list tier
  filter; migration tooling polish; docs rewrite), plus one
  catch-all for the eventual `target_scope` → `target_tier`
  decision. The exact split is the first action item after ADR-0016
  acceptance.
- **`target_scope` → `target_tier` rename ADR.** Whether to
  author one, and when. Default posture: wait until PR-F ships
  and a concrete contributor signal exists ("the field name
  confused me when reading X"). If no signal emerges within 3
  months, file an ADR concluding the rename is not worth the
  churn and close the question.
- **ADR-0001 §1 formal amendment.** Whether the cross-references
  in ADR-0011 §7 and ADR-0016 §9 are sufficient, or whether a
  separate amendment ADR is warranted. Default posture: skip
  unless a sub-issue author reports that ADR-0001 §1's wording
  actively misleads them after this ADR lands.
- **Web UI tier badges and CLI tier filter copy.** ADR-0011
  PR-F's responsibility; this ADR pins the underlying tokens but
  not the visual copy.
- **Cross-runtime fan-out** (one tier → multiple runtime scopes,
  e.g., `user`-tier canonical materializing into both
  `~/.claude/` and `~/.codex/` for cross-runtime agents). Not in
  scope here; flagged so future readers know the vocabulary
  permits it.
- **ADR-0011 §1 settings row cleanup.** That row currently lists
  `~/.claude/settings.json` etc. under columns labelled
  `Canonical (user / project_shared / project_local)` while
  asserting in its rightmost cell that "settings have no
  canonical/runtime split — they ARE the runtime". This contradicts
  ADR-0010 §Background's canonical/runtime table for settings
  (`<proj>/.memtomem/settings.json` canonical, `<host>/.claude/...`
  resolved runtime). ADR-0016 §2 / §3 work around it by promoting
  settings to a documented special case, but a one-line docs cleanup
  on ADR-0011 §1's settings row would make the contradiction
  disappear. Tracked as a follow-up doc PR; can be folded into the
  868-C public docs rewrite slice.

## References

**Issues / PRs / milestones**

- Issue #868 — epic umbrella; this ADR provides its vocabulary
  anchor.
- Issue #867 — `CLOSED`, absorbed by ADR-0010 (settings hooks
  asymmetry). Not a live sub-issue.
- PR #876 — settings-migrate subcommand, ADR-0010 §4 implementation.
- PR #882 — memory schema + read/write surface, ADR-0011 PR-B / C / D.
- PRs #889 / #890 / #893 — agents / skills / commands canonical
  scope axis, ADR-0011 PR-E.
- PR #914 — ADR-0015 (Web scope vocabulary).
- ADR-0011 PR-F — pending; Web UI tier badges and public docs.

**ADRs**

- ADR-0001 §1 — implicit canonical residency / fan-out coupling
  retired in §9.
- ADR-0010 — settings hooks 3-tier scope; absorbs #867.
- ADR-0011 — memory / agents / skills / commands canonical
  3-tier scope; PR-A through PR-E shipped.
- ADR-0015 — Web layer scope vocabulary (`project_scope_id` vs
  `target_scope`).

**Source anchors** — line numbers reflect `origin/main` at
ADR-0016 acceptance time; readers should grep the symbol if a number
drifts.

- `packages/memtomem/src/memtomem/config.py:745` —
  `TargetScope` literal (single source of truth across settings
  and artifacts; ADR-0010 / ADR-0011 / ADR-0015 cite it at
  pre-drift line numbers).
- `packages/memtomem/src/memtomem/config.py:758` —
  `target_scope` default value (`"user"`) on the settings hooks
  config schema (ADR-0010 §2 v1 default).
- `packages/memtomem/src/memtomem/context/_runtime_targets.py:71`
  — `RUNTIME_FANOUT_TABLE` (per-tier per-artifact fan-out
  mapping).
- `packages/memtomem/src/memtomem/context/_skip_reasons.py:25`
  — `NO_PROJECT_FANOUT_FOR_RUNTIME` skip code (ADR-0011 §3).
- `packages/memtomem/src/memtomem/web/routes/context_projects.py:67`
  — `resolve_scope_root` (project-root resolver; ADR-0015).
