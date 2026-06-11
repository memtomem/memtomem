# ADR-0011: Canonical artifact scope hierarchy (user / project shared / project local)

**Status:** Accepted
**Date:** 2026-05-09
**Context:** ADR-0010 introduced a 3-tier `target_scope` axis for settings
hooks. This ADR records whether and how to extend the same axis to the
remaining canonical artifact types — memory, agents, skills, commands —
and defines the staged migration. Source: PR #876 settings-migrate
landed today; the user request is "memory 도 같은 계층 구조로 개편하면
어때 + agents/skills 도 포함" — i.e., one scope axis for every canonical
artifact in memtomem.

## Terminology

The three scope values mirror ADR-0010 verbatim:

| `scope` value      | Resolved canonical path (per artifact type)                  | Tracked by git? |
|--------------------|--------------------------------------------------------------|-----------------|
| `user`             | `~/.memtomem/<artifact>/...`                                 | n/a (user home) |
| `project_shared`   | `<project>/.memtomem/<artifact>/...`                         | yes             |
| `project_local`    | `<project>/.memtomem/<artifact>.local/...`                   | no (gitignored) |

`project_shared` means "git-tracked", **not** "shared between agents" —
the latter is the orthogonal `shared` namespace that already exists for
memory (`agent-runtime:` / `shared` namespace conventions). The naming
is inherited from ADR-0010 to keep one vocabulary across the codebase;
the disambiguation is recorded here so future readers do not conflate
the two axes.

## Background

### What ADR-0001 §1 established

ADR-0001 §1 (`docs/adr/0001-context-gateway-sync-policies.md:9-37`) fixed
two related principles for agents / skills / commands:

- **Reverse sync runtime priority** is deterministic per artifact type
  (Claude → Gemini → Codex for agents; detector order for skills).
- **Fan-out is one-way (canonical → runtime)**, with canonical living at
  project scope (`<proj>/.memtomem/<artifact>/`). Codex agents fan out
  to project scope; Codex prompts remain user-scope only because there
  is no project-scope equivalent in the Codex runtime — explicitly the
  exception, not the principle.

The unstated default that emerges from §1: canonical = project-scope for
every artifact type. Memory followed the same default by convention
(single user-local SQLite at `~/.memtomem/`, with auto-discovered
provider dirs as the only "non-project" sources).

### What ADR-0010 changed

ADR-0010 (`docs/adr/0010-settings-hooks-target-scope.md`, Accepted
2026-05-09) introduced `target_scope: user / project_shared /
project_local` for settings hooks specifically, validated the 3-tier
model in this codebase (config field at
`packages/memtomem/src/memtomem/config.py:721`, plumbing across
`_claude_target` / `target_file`, settings-migrate subcommand in
PR #876), and **explicitly excluded memory data** from its scope
(ADR-0010 line 23–31 background table). The deferral was deliberate:
the settings ADR was already large; memory and other canonical
artifacts could ride on the same plumbing primitives in a follow-up.

### Why now

With ADR-0010 plumbing in tree, "canonical = project-scope only" for
memory / agents / skills / commands becomes the unjustified exception
rather than the principled choice. Two concrete user workflows are
foreclosed today:

1. **Team-shared project memory.** A team that wants to commit
   "rules for this codebase" or "patterns we've decided on" memories
   has no place to put them. The current single user-local SQLite
   accepts only personal memory; teammates have to re-enter the same
   facts on each developer machine, or paste them into `CLAUDE.md`
   (which is text-only and not searchable through `mem_search`).
2. **Cross-project personal agents / skills / commands.** A user
   with a personal "deploy-helper" skill that should apply to every
   project has to copy it into each project's `.memtomem/skills/`.
   Claude Code itself supports `~/.claude/skills/` as a user tier;
   memtomem's canonical layer does not expose a way to author there.

Settings (ADR-0010) solved the team-shared workflow for hooks. This
RFC solves it for the remaining four artifact types in one ADR so the
mental model is symmetric.

### Tier-merge assumption — sourced

For agents / skills / commands, Claude Code 2.x merges user-tier
(`~/.claude/<artifact>/`) and project-tier (`<proj>/.claude/<artifact>/`)
runtime entries additively at load time, the same way it merges
settings tiers (sourced: Anthropic Claude Code reference,
https://docs.claude.com/en/docs/claude-code/settings — referenced from
ADR-0010 Background "Tier-merge assumption — sourced"). A user-scope
canonical fanned out to `~/.claude/agents/foo.md` AND a project-scope
canonical fanned out to `<proj>/.claude/agents/foo.md` BOTH load. This
is symmetric with ADR-0010's settings tier-merge; the same caveat
applies (if a future Claude Code revision retracts the merge, the
double-fan-out becomes "user copy is dead config" — the tradeoff
shifts but the staged conservative default in §2 does not).

For project_local on non-memory artifacts, this RFC chooses **no
fan-out** by design (§3) — the runtime tier-merge does not apply
because project_local never reaches runtime.

## Decision

### 1. Adopt 3-tier scope axis for memory / agents / skills / commands

Mirror ADR-0010's `TargetScope` literal verbatim. Reuse the same type
(`Literal["user", "project_shared", "project_local"]` at `config.py:721`)
across all artifact types — one vocabulary, one validator, one parser.

The canonical / runtime layout per scope per artifact type:

| Artifact     | Canonical (user)                              | Canonical (project_shared)                       | Canonical (project_local)                                | Runtime fan-out per scope                                                         |
|--------------|-----------------------------------------------|--------------------------------------------------|----------------------------------------------------------|-----------------------------------------------------------------------------------|
| **memory**   | `~/.memtomem/memories/` + auto-discovered dirs | `<proj>/.memtomem/memories/`                     | `<proj>/.memtomem/memories.local/`                       | indexed into single user-local SQLite (no on-disk runtime mirror)                 |
| **agents**   | `~/.memtomem/agents/<name>.md`                | `<proj>/.memtomem/agents/<name>.md`              | `<proj>/.memtomem/agents.local/<name>.md`                | user → `~/.claude/agents/`; project_shared → `<proj>/.claude/agents/`; project_local → no fan-out |
| **skills**   | `~/.memtomem/skills/<name>/SKILL.md`          | `<proj>/.memtomem/skills/<name>/SKILL.md`        | `<proj>/.memtomem/skills.local/<name>/SKILL.md`          | user → `~/.claude/skills/<name>/`; project_shared → `<proj>/.claude/skills/<name>/`; project_local → no fan-out |
| **commands** | `~/.memtomem/commands/<name>.md`              | `<proj>/.memtomem/commands/<name>.md`            | `<proj>/.memtomem/commands.local/<name>.md`              | user → `~/.claude/commands/`; project_shared → `<proj>/.claude/commands/`; project_local → no fan-out |
| **settings** (ADR-0010, special case per ADR-0016 §2) | `<proj>/.memtomem/settings.json` (canonical is single regardless of tier) | ↑ same | ↑ same | `user` → `~/.claude/settings.json`; `project_shared` → `<proj>/.claude/settings.json`; `project_local` → `<proj>/.claude/settings.local.json` |

Settings inverts the tier ↔ residency mapping the other rows share: per
ADR-0016 §2, the `target_scope` axis on settings hooks selects the
**runtime fan-out target**, not the canonical residency (the canonical
is `<proj>/.memtomem/settings.json` by ADR-0010 §Background, regardless
of the configured `target_scope`). The other four artifacts in this
table couple canonical residency and runtime fan-out through
`RUNTIME_FANOUT_TABLE`; settings is the documented exception.

### 2. v1 defaults preserve current behavior, zero behavior change for existing installs

| Artifact           | v1 default `scope` | Reason                                                          |
|--------------------|--------------------|-----------------------------------------------------------------|
| memory             | `user`             | Current behavior: writes go to `~/.memtomem/memories/`          |
| agents             | `project_shared`   | Current behavior: `mm context init` writes `<proj>/.memtomem/agents/` |
| skills             | `project_shared`   | Same as agents                                                  |
| commands           | `project_shared`   | Same as agents                                                  |
| settings (ADR-0010) | `user`            | Current behavior preserved per ADR-0010 §2                      |

No silent flip. Opting into a non-default scope requires explicit
`--scope=...` on the relevant command. Existing chunks in the SQLite
DB and existing canonical files are classified as their current
locations imply (§4 migration).

### 3. `project_local` for agents / skills / commands is a draft tier with no fan-out

Memory's `project_local` has a clear semantic — it's indexed into the
SQLite DB just like other scopes, just gitignored on disk. For
agents / skills / commands, this RFC chooses a deliberately different
semantic: **`project_local` is a per-checkout draft tier that memtomem
recognises but never fans out to a Claude Code runtime path.**

Rationale:

- Three plausible designs were considered: (a) draft tier with no
  fan-out; (b) memtomem-only loader fan-out to a parallel runtime path
  (`<proj>/.claude/agents.local/`) that Claude Code would not see;
  (c) symmetric additive merge fan-out into `<proj>/.claude/agents/`
  the same way `project_shared` fans out.
- (c) is rejected because two canonical sources fanning to the same
  runtime path would have to define a precedence rule (which file
  wins?); the additive-merge model the runtime applies *across tiers*
  does not apply *within a single tier*.
- (b) is rejected because Claude Code does not recognise an `.local/`
  variant for agents / skills / commands the way it does for
  `settings.local.json`. Inventing one creates a memtomem-only
  surface that confuses users coming in from the runtime side.
- (a) is the cleanest semantic. The canonical lives under
  `<proj>/.memtomem/agents.local/`, gitignored, indexed by
  `mm context status` for the user's own awareness, and explicitly
  invisible to the runtime. Promotion to `project_shared` is
  `git mv agents.local/X.md → agents/X.md` followed by `mm context
  sync`, which fans out the new project_shared canonical normally.

### 4. Memory storage stays single-user-local, schema gains a scope tag per chunk

Memory's SQLite DB at `~/.memtomem/memtomem.db` is derived state —
embeddings, FTS rowids, dedup hashes, chunk-link graph. Splitting the
DB across scopes would force per-scope schema evolution, fragment
dedup, and break `mem_agent_share` (chunk-links would need cross-DB
FKs). **One DB, per-row scope tag** is the only path that keeps the
existing dedup, sharing, and embedding contracts intact.

The `chunks` table gains two columns (idempotent ALTER, mirrors the
namespace migration at `storage/sqlite_schema.py:90`):

```sql
ALTER TABLE chunks ADD COLUMN scope TEXT NOT NULL DEFAULT 'user';
ALTER TABLE chunks ADD COLUMN project_root TEXT;
```

`project_root` is required because a single user-local DB can hold
chunks from multiple worktrees of the same project (or two different
projects whose `project_shared` dirs share a name). Without it,
sibling shared scopes collide on path-prefix lookups. Existing
UNIQUE index `(namespace, source_file, content_hash, start_line)`
does not need extension — `source_file` is absolute and disambiguates
worktrees naturally.

Migration is opt-in: existing rows default to `scope='user'`;
no user action is required to keep current behavior.

### 5. Privacy gates layered: hard refusal at the chokepoint, explicit-flag-and-confirm at the surface

Two gates fire on every project_shared write. A bug in either still
leaves one active.

**Gate A (chokepoint).** `privacy.enforce_write_guard`
(`packages/memtomem/src/memtomem/privacy.py:432`) gains a `scope` kwarg
and rejects `force_unsafe=True` when `scope == "project_shared"` —
hard refusal, not a warning. The decision string is
`blocked_project_shared`; the bypass audit log carries a special
marker so SOC/security pipelines can alert on attempts.

Why hard refusal: `project_shared` content goes into git history. Even
an instant `git rm` cannot retract it from any clone or reflog. The
trust boundary moves from "the user's machine" to "every clone of this
repo forever," so the bypass valve does not belong here.

For agents / skills / commands the corresponding chokepoint is the
`mm context sync` write path, when the canonical is `project_shared`
and the runtime fan-out is about to write. A new
`context/privacy_scan.py` helper runs `privacy.scan` on the canonical
content before each fan-out write; if hits exist and the canonical is
project_shared, the sync write is blocked regardless of `--force-unsafe`.

> **2026-06 (#1247):** Gate A also fires on wiki ingress — `mm context
> install` / `update` (incl. `--all` and the `--force` `.bak`
> preservation copy) scan the wiki bytes (HEAD or pinned git objects)
> before anything lands in `project_shared` — and on the web settings
> `rules/promote` write, which scans the exact appended hooks fragment
> (event key included). Every first-party byte path into
> `project_shared` now scans before landing. Gate B's confirm prompt is
> intentionally absent on install/update: those verbs have no `--scope`
> choice (dest is `project_shared` by construction), so the explicit
> command itself carries the surface intent; only the scan half was
> missing.

**Gate B (surface).** Explicit `--scope project_shared` flag plus
confirm prompt at the CLI/MCP write surface (`mm mem add`,
`mm context init`, `mm context migrate --to project_shared`). The flag must be passed
explicitly — no env var or config-field default. There is
deliberately **no** `memory.default_write_scope` config field: the
explicit `--scope` flag (CLI) and `scope=` kwarg (MCP `mem_add`
/ `mem_batch_add`) are the only paths by which `project_shared`
becomes the active scope. A future contributor introducing such a
default must pass through this ADR rather than landing it as a
silent config addition; reviewers should refuse a
`MemoryConfig.default_write_scope` field on sight.

The confirm prompt:

```
About to write to <project>/.memtomem/<artifact>/<file>.
This file is git-tracked. Anyone with repo access can read this.
Continue? [y/N]:
```

`--yes` overrides the prompt; MCP `confirm_project_shared=True`
overrides for tool calls. Both produce a
`project_shared.confirmed_via=<surface>` audit line.

`mem_edit` and `mem_delete` infer scope from the loaded chunk's
persisted `metadata.scope`, not the caller's parameter — a client that
omits `scope` while editing a project_shared chunk cannot bypass the
gate by accident.

Before PR-D, `mem_batch_add` bypassed `enforce_write_guard` and used
an inline `privacy.scan` instead — the batch path was the obvious
bypass route. PR-D refactored `mem_batch_add` (`server/tools/memory_crud.py:668`)
to call `enforce_write_guard` per entry so the chokepoint applies
uniformly across single-add and batch-add surfaces; this section is
retained for the rationale, not as a future task.

**Authoring-side privacy is explicitly out of scope.** When a user
edits a project_shared agent markdown directly with their text editor,
memtomem cannot gate the save. Pre-commit hooks are the team's choice;
this RFC scopes `mm mem rescan --scope project_shared` and
`mm context rescan --scope project_shared` as the natural composable
building blocks for that workflow, but treats the subcommands as
v1-deferred (Open Questions item 4) — they ship only if a concrete
user reports a need before PR-D / PR-E. Auto-installing pre-commit
hooks is a non-goal regardless: it would imply ingest-time scanning is
bypassable, which contradicts the trust boundary documented in
`privacy.py:7-26`.

The `blocked_project_shared` decision is **LTM-only** and does NOT
sync to STM. `privacy.py`'s asymmetric-sync rule already specifies
secret-class patterns sync from STM but PII-class do not auto-sync;
this new outcome is added to the LTM module's docstring with an
explicit "not synced upstream" note so the next STM-pattern sync does
not try to mirror it.

### 6. Memory search default is project-aware, not naively additive

For settings hooks, ADR-0010 §3 leans on Claude Code 2.x's additive
tier-merge to do the right thing at runtime. Memory has no equivalent
runtime — search runs in memtomem's own SQL and must define semantics
explicitly:

- **Project context detected** (caller's cwd resolves under a
  `<X>/.memtomem/` ancestor): default search returns rows where
  `scope = 'user' OR project_root = <X>`. Other projects' shared/local
  rows are excluded. Prevents cross-project leak.
- **No project context** (`mm mem search` from `~/`): default returns
  `scope = 'user'` only. Project tiers are excluded unless an
  explicit `--scope=project_*` filter is passed.
- **Explicit `--scope=project_shared` from no-project-context:**
  unions every `project_root`'s shared rows — a deliberate
  cross-project search. Document this is intentional, not accidental.

A scope-context SQL fragment is **always** appended to
`bm25_search` / `dense_search` / `recall_chunks`, even when
`scope_filter=None`. The implementation lives in a new
`storage/sqlite_scope.py` mirroring `sqlite_namespace.py`.

Same-relevance results tie-break in scope priority order:
`project_local > project_shared > user`. Pin in regression test.

`scope_filter` composes orthogonally with the existing
`system_namespace_prefixes` filter: both AND together. A chunk in
`namespace=archive:foo` and `scope=project_shared` is hidden by the
default-search archive prefix exclusion regardless of scope — same as
today.

### 7. ADR-0001 §1 supersession

This RFC supersedes the implicit "canonical = project-scope only"
default that ADR-0001 §1 established for agents / skills / commands
through its reverse-sync priority and one-way fan-out rules. Canonical
is now scope-selectable across user / project_shared / project_local
for memory, agents, skills, and commands. **v1 defaults preserve
ADR-0001 §1's behavior** — agents / skills / commands still default
to project_shared canonical when `mm context init` is run without
`--scope`. The supersession is in availability of new scopes, not in
the change of any existing default.

ADR-0001 stays in place as historical context; its body is not
amended. Future readers cross-reference this ADR via the references
section below.

### 8. No default flip planned

ADR-0010 §5 codifies a future default-flip trigger for settings
(`user → project_local` once detection + migration ship and a 2-week
P0/P1 dwell passes). ADR-0011 plans **no default flip, ever** for
memory or agents / skills / commands. Two reasons:

- **Memory authorship is not regenerable.** Hooks are derived
  artifacts that `mm context sync` can re-materialize from canonical;
  flipping their default tier costs little. User-authored memories
  are the canonical — flipping `mem_add` default scope silently moves
  authorship into git history, a trust violation no soak period can
  validate.
- **Agents / skills / commands defaults already match user
  expectation.** `mm context init` defaulting to project_shared
  matches what users do today; flipping to user-scope or project_local
  would silently change where new artifacts land. Without a concrete
  failure mode the flip would solve, the staged-flip pattern's risk
  outweighs the symmetry win.

The asymmetry with ADR-0010 §5 is documented in Consequences below.

### 9. Phasing

Six PRs, each independently revertable. PR-B / PR-C / PR-D were
drafted alongside this ADR and shipped together as a single bundle
in PR #882 (2026-05-09); PR-A landed shortly after to capture the
RFC on `main`. PR-E shipped 2026-05-10/11 in #889 / #890 / #893;
the Web/docs slice of PR-F shipped 2026-05-11 in #929.

| PR    | Scope                                                                                       | Ship status                                                |
|-------|---------------------------------------------------------------------------------------------|------------------------------------------------------------|
| PR-A  | This ADR markdown.                                                                          | This PR. Status: Proposed at merge → Accepted after dwell. |
| PR-B  | Memory schema (append-only columns), `IndexingConfig.project_memory_dirs`, scope classifier, `_resolve_scope` / `_apply_scope`, `dataclasses.replace` refactor of `_apply_namespace`, Gate A on `enforce_write_guard`. No CLI/MCP surface; defaults preserved. | Shipped 2026-05-09 in PR #882.                             |
| PR-C  | Memory read surface — `ScopeFilter`, `scope_context_sql` always-on fragment, project-aware default merge per §6, search pipeline cache key, MCP read tools, CLI `--scope` (comma-list) on read commands. | Shipped 2026-05-09 in PR #882.                             |
| PR-D  | Memory write surface — Gate B (explicit flag + confirm), `mm mem add` restructure (the pre-PR-D hardcoded `~/.memtomem/memories` user_base in `cli/memory.py` was incompatible with project-scope writes), `mem_batch_add` refactor through `enforce_write_guard`, `mem_edit` / `mem_delete` inferred-scope, `mem_consolidate_apply` cross-scope rejection, `mm context memory-migrate` v1 (chunk-id-stable single-DB rename). `mm mem rescan` deferred (Open Questions item 4). | Shipped 2026-05-09 in PR #882.                             |
| PR-E  | Agents / skills / commands canonical scope axis — `context/scope_resolver.py`, each generator's `target_file` / `is_available` accepting `(scope, project_root)`, `mm context init --scope=...`, `mm context sync --scope=...` filter, `mm context migrate <kind> <name> --to <scope>` for cross-tier moves (the originally-named `promote` / `demote` verbs were consolidated into `migrate --to`), sync-time privacy scan (Gate A for non-memory). `mm context rescan` deferred per Open Questions item 4. | Shipped 2026-05-10/11 in PRs #889 (E1+E2), #890 (follow-up nits), #893 (E4 `migrate --to`). |
| PR-F  | Web UI scope badges (memory + context) read-only, `/api/add` rejection with CLI hint plus docs link, public docs updates (user-guide / getting-started / mcp-clients per the default-change fanout convention). | Web/docs slice shipped 2026-05-11 in #929 (closes #924); tier-switching write affordances and detail/diff/rendered-route alignment remain follow-up polish. |

**Sequencing note.** ADR-0010 was Accepted on 2026-05-09 and the
implementation work for ADR-0011 PR-B / PR-C / PR-D landed the same
day. The earlier draft of this section recorded a "≥2 weeks ADR-0010
dwell with no P0/P1 before opening PR-A" prerequisite — applied
literally that would have blocked opening PR-A indefinitely after the
implementation already shipped, which is why PR-B / PR-C / PR-D were
bundled and merged ahead of this ADR landing on `main`. The prerequisite
is retained in spirit as a **post-merge ratification gate** instead:
this ADR enters at Status `Proposed`, and the flip to `Accepted`
requires the same ≥2-week, zero P0/P1 window against ADR-0010's
settings-migrate plumbing and #882's memory scope plumbing.
Implementers of any remaining PR-F follow-up polish should treat the
Accepted flip — not the PR-A merge — as the unblock signal.

## Consequences

- The implementation roadmap is six PRs (see §9 phasing table).
  PR-A is this ADR markdown; PR-B (memory plumbing) / PR-C (memory
  read surface) / PR-D (memory write surface) shipped together in
  PR #882 (2026-05-09). PR-E (canonical scope axis for agents /
  skills / commands) shipped in #889 / #890 / #893, and the PR-F
  Web/docs slice shipped in #929 (2026-05-11); remaining work is
  follow-up polish, not a wholly pending phase (see §9 sequencing note).
- Every canonical artifact in memtomem now uses one scope vocabulary.
  The `TargetScope` literal at `config.py:721` is the single source
  of truth across settings, memory, agents, skills, and commands.
- `<project>/.memtomem/config.json` remains deferred per ADR-0010 §3.
  Per-project scope selection is solved by absolute paths in the
  user-tier `IndexingConfig.project_memory_dirs` field (memory) and
  by per-invocation `--scope` flags (everything else). Implementers
  of PR-B / PR-E should resist accidentally introducing the
  project-config layer; the same warning ADR-0010 §3 records applies
  here.
- Claude Code 2.x's additive merge of user-tier and project-tier
  runtime entries (agents / skills / commands) is now a load-bearing
  assumption for memtomem's fan-out. Same caveat as ADR-0010
  Background Tier-merge note: if the merge model changes upstream,
  the duplicate-name story degrades to "user copy is dead config."
- The asymmetry with ADR-0010 §5 — settings can default-flip, memory
  / agents / skills / commands cannot — is recorded in §8.
  Implementers of any future default-flip ADR for these artifacts
  must reopen this discussion explicitly rather than ride on
  ADR-0010's precedent.
- Privacy `enforce_write_guard` becomes the single chokepoint for
  every memory write surface. The pre-PR-D `mem_batch_add` bypass was
  treated as a bug (closed by PR-D — `mem_batch_add` at
  `memory_crud.py:668` now calls `enforce_write_guard` per entry),
  not as a design choice to preserve.
- For agents / skills / commands, `project_local` exists but does
  not fan out to runtime. Users who expect "everything I author
  ships to .claude/" will be surprised by drafts staying invisible.
  The CLI surfaces this with a `(draft, no fan-out)` annotation in
  `mm context status` output.

## Considered & rejected

- **Status quo: keep memory and agents / skills / commands at
  project-scope only.** Rejected because it forecloses the team-shared
  memory and cross-project personal artifact workflows described in
  Background "Why now". Once ADR-0010 validated the 3-tier model
  in-tree, holding the line for the remaining four artifact types
  becomes the unjustified exception.
- **Configurable + flip default to `project_shared` for memory
  immediately.** Rejected for v1 because flipping the default scope
  for `mem_add` silently moves authorship into git for users who
  don't notice the change. Memory authorship is not regenerable;
  the staged-flip pattern that ADR-0010 uses for hooks does not
  transfer cleanly to canonical authoring surfaces. §8 codifies "no
  flip, ever" as the recommended posture.
- **Symmetric additive merge for agents / skills / commands
  `project_local`.** (Considered design (c) in §3.) Rejected because
  fanning out two canonical sources to the same runtime path requires
  a within-tier precedence rule the runtime tier-merge model does not
  define. Draft-tier-no-fan-out (§3) avoids the problem entirely.
- **memtomem-only loader fan-out to `<proj>/.claude/agents.local/`.**
  (Considered design (b) in §3.) Rejected because Claude Code does
  not recognise the `.local/` runtime variant for non-settings
  artifacts; inventing a memtomem-only path makes the runtime
  surface inconsistent across artifact types.
- **Split memory storage by scope (one SQLite per scope).**
  Rejected because it fragments dedup, breaks chunk-links across
  scopes, and forces per-scope schema evolution. §4 single-DB-with-
  scope-tag preserves every existing memory contract.
- **Pre-commit hook auto-installation for project_shared.** Rejected
  because auto-installing implies ingest-time scanning is bypassable.
  Ship `mm mem rescan` / `mm context rescan` as composable building
  blocks; teams who want a hook wire it themselves in
  `.pre-commit-config.yaml`. §5 records this as an explicit
  non-goal.
- **Amend ADR-0001 §1 instead of authoring a new ADR.** Rejected
  for the same reason ADR-0010 cited: ADR-0001 has no amendment
  precedent in this repo, and ADR-0007 / ADR-0008 / ADR-0010 all
  layered onto ADR-0001 without amending it. New ADR is the
  established pattern.
- **Introduce `<project>/.memtomem/config.json` in this RFC.**
  Rejected because ADR-0010 §3 deferred it deliberately and the
  decision still holds. Per-project scope selection works through
  user-tier absolute paths (memory) and per-invocation flags
  (everything else); the project-config layer is a separate ADR's
  responsibility when a concrete need surfaces.

## Open questions for the implementation issues

The following points stay live for the implementation PRs to resolve;
they do not block this ADR's acceptance.

- The final names of `project_shared` / `project_local`. Inherited
  from ADR-0010 (which itself flagged naming as TBD). If a clearer
  pair emerges during PR-B, the rename can land alongside before any
  user-visible release; both ADRs flip together.
- The same-name conflict resolution for agents / skills / commands
  when one name appears in both project_shared and user scopes. §1
  notes Claude Code natively merges; for memtomem-managed views
  (`mm context status --scope <tier>`, or the Web overview's per-tier
  views), highlight the conflict so the user knows.
  The exact warning copy is implementation issue territory.
- Orphan project chunk garbage collection. If a user indexes
  `/tmp/foo/.memtomem/memories/` then deletes the directory, stale
  rows remain in the user-local DB. Watcher prunes file-level
  deletions within tracked roots, but root-removal is a new case.
  Document as a v1 known limitation in PR-B's release notes; file a
  follow-up issue.
- The use cases for `mm mem rescan` and `mm context rescan` are
  spec'd to two scenarios in §5 (STM pattern sync; git-mv ingest
  bypass). If neither has a concrete user reporting before PR-D /
  PR-E, drop the subcommands from v1 and re-add when a real use
  case shows up.
- chunk-id-stable rename mode for `mm context memory-migrate`.
  v1 reports the count of dropped chunk_links lineage rows; full
  preservation across scope moves is deferred.
- MCP exposure for `mm context init` / `sync` / `migrate`.
  PR-E shipped through #889 / #890 / #893, with scope-tier moves
  consolidated into `mm context migrate <kind> <name> --to <scope>`.
  #887 since landed MCP parity for `init` / `sync` / `generate` /
  `diff` (the `mem_context_*` tools), and memory migration is exposed
  as `mem_context_migrate` — a thin wrapper over `mm context
  memory-migrate` (markdown memory files only). The **artifact**
  scope-tier and flat→dir modes of `mm context migrate <kind> <name>`
  remain **CLI-only by design**: there is no MCP path that migrates
  agents / skills / commands between tiers. #1123 (B5-1 / B5-2) records
  this as a deliberately-deferred gap rather than built — `migrate`'s
  destructive, layout-aware semantics are an interactive/authoring
  surface, not an agent-runtime one.

## References

**Issues / PRs / milestones**

- This ADR's source: user request 2026-05-09 ("memory 도 같은 계층
  구조로 개편하면 어때 + agents/skills 도 포함").
- PR #876 — settings-migrate subcommand, ADR-0010 §4 implementation.
- PR #874 — duplicate hooks detection, ADR-0010 §4.
- PR #873 — `hooks.target_scope` config field + `_claude_target` /
  `target_file` plumbing, ADR-0010 §3.
- Tiered context gateway v2 milestone (#868 umbrella) — the broader
  3-tier model this ADR completes.

**ADRs**

- ADR-0001 §1 — reverse-sync priority and one-way fan-out;
  superseded in part by §7 of this ADR (canonical-scope axis added,
  defaults preserved).
- ADR-0001 §5.1 — `≥2 weeks no P0/P1` readiness wording mirrored in
  the §9 PR-A prerequisite.
- ADR-0007 — trigger-criteria-then-flip precedent, referenced for
  the §8 "no flip" inversion.
- ADR-0009 — RFC pattern precedent for Proposed-status ADRs with
  multi-week dwell, mirrored in §9.
- ADR-0010 — settings hooks 3-tier scope; this ADR's plumbing
  primitives reuse the `TargetScope` literal at `config.py:721`,
  the `_resolve_cli_scope` flag pattern at
  `cli/context_cmd.py:313`, and the `_confirm_settings_host_writes`
  prompt shape at `cli/context_cmd.py:342-366`.

**External docs**

- Anthropic Claude Code settings reference —
  https://docs.claude.com/en/docs/claude-code/settings (source for
  the additive multi-tier merge claim referenced in Background;
  inherited from ADR-0010).

**Source files (anchors for the implementation PRs)**

Line numbers reflect HEAD on `main` after PR #882. Anchors are listed
by symbol where line drift is plausible; readers should grep the
symbol if a number ever stops resolving.

- `packages/memtomem/src/memtomem/config.py:721` — `TargetScope`
  literal (reused as-is across artifacts).
- `packages/memtomem/src/memtomem/config.py:193` — `IndexingConfig`;
  PR-B added `project_memory_dirs` (`config.py:203`) and
  `all_index_roots()` helper (`config.py:290`).
- `packages/memtomem/src/memtomem/config.py:1403` —
  `categorize_memory_dir`; PR-B added sibling `classify_scope`.
- `packages/memtomem/src/memtomem/privacy.py:432` —
  `enforce_write_guard`; PR-B added the `scope` kwarg + Gate A
  hard refusal.
- `packages/memtomem/src/memtomem/storage/sqlite_schema.py:90`
  (namespace migration), `:109` (overlap columns), `:128`
  (temporal-validity) — column-add migration precedents for PR-B's
  `scope` / `project_root` columns at `:147-148`.
- `packages/memtomem/src/memtomem/storage/sqlite_backend.py:1333` —
  `_row_to_chunk`; PR-B updated the optional-column index math for
  the appended columns.
- `packages/memtomem/src/memtomem/indexing/engine.py:1069` —
  `_apply_namespace`; PR-B added sibling `_apply_scope` and refactored
  both to `dataclasses.replace`.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:668` —
  `mem_batch_add` post-PR-D chokepoint (calls `enforce_write_guard`
  per entry). `mem_edit` / `mem_delete` inferred-scope is also in this
  module; grep `_resolve_scope`.
- `packages/memtomem/src/memtomem/cli/memory.py:179` — `user_base`
  fallback in the `mm mem add` write-target resolution; PR-D
  restructured the surrounding flow so `--scope project_shared` /
  `project_local` now resolve through `memtomem.memory_scope`
  rather than the user-tier hardcoded path that existed pre-PR-D.
- `packages/memtomem/src/memtomem/context/agents.py`,
  `context/skills.py`, `context/commands.py`,
  `context/generator.py` — generator base; PR-E will thread
  `(scope, project_root)` through `target_file` / `is_available`.
- `packages/memtomem/src/memtomem/cli/context_cmd.py:313`,
  `:342-366` — `_resolve_cli_scope`, `_confirm_settings_host_writes`;
  PR-D and PR-E reuse both. `mm context memory-migrate` shape lives
  at `cli/context_cmd.py:2000` (`memory_migrate_cmd`); PR-E reuses
  the same Click pattern.
