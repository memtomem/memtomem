# ADR-0015: Context Gateway scope vocabulary (project_scope_id vs target_scope)

**Status:** Accepted
**Date:** 2026-05-11
**Context:** Issue #901 surfaced that the Context Gateway Web layer uses
"scope" for two unrelated dimensions — a per-request project-root selector
(`?scope_id=`) and the canonical artifact tier
(`TargetScope = user | project_shared | project_local`, sourced from
`config.hooks.target_scope`). The split is workable while Web writes are
mostly cwd / project-shared centric but becomes easy to misuse as tier-aware
UI is added (overview, Sync All, future multi-scope writes), and issue #900
(artifact sync service extraction) cannot fix its service interface names
until the vocabulary settles. This ADR lifts the RFC draft and maintainer
recommendation that were posted as comments on #901 into a checked-in
record so #900 and the implementation follow-ups have a single authoritative
reference.

## Terminology

The Context Gateway Web layer uses two distinct scope dimensions; the names
in this ADR are the qualified forms to be used everywhere new:

| Term                | Dimension                              | Source on `main`                                                                              |
|---------------------|----------------------------------------|-----------------------------------------------------------------------------------------------|
| `project_scope_id`  | Per-request project-root selector      | Resolved via `resolve_scope_root` (`packages/memtomem/src/memtomem/web/routes/context_projects.py:67`) |
| `target_scope`      | Canonical artifact tier                | `TargetScope = Literal["user", "project_shared", "project_local"]` (`packages/memtomem/src/memtomem/config.py:745`), sourced from `config.hooks.target_scope` |

Unqualified "scope" in older code refers to whichever of the two
dimensions the caller cared about; this ADR retires that usage in new
code. Existing query params, response fields, and path segments keep
their names for backward compatibility (§5).

`project_shared` means "git-tracked", **not** "shared between agents" —
inherited verbatim from ADR-0011's Terminology, recorded here so future
readers do not conflate the artifact-tier `target_scope` axis with the
memory `agent-runtime:` / `shared:` namespace axis.

## Background

### What ADR-0010 and ADR-0011 established

- ADR-0010 (`docs/adr/0010-settings-hooks-target-scope.md`) introduced
  `target_scope: user / project_shared / project_local` for settings
  hooks and validated the 3-tier model in this codebase.
- ADR-0011 (`docs/adr/0011-canonical-artifact-scope-hierarchy.md`)
  extended the same axis to canonical agents / skills / commands /
  memory artifacts. ADR-0011 §3 also pinned that `project_local`
  artifacts have **no runtime fan-out** — canonical-only draft items at
  this tier surface as typed `NO_PROJECT_FANOUT_FOR_RUNTIME` skips
  rather than runtime files.

### The Web-layer inconsistency this ADR fixes

The artifact-side `target_scope` axis from ADR-0010/0011 is already in
the codebase, but the Context Gateway Web routes consume it only
partially:

- `GET /context/{skills,commands,agents}` accepts `?scope_id=` via
  `resolve_scope_root` for project-root selection but **does not** accept
  any `target_scope` parameter — there is no way to ask "list skills in
  user scope only" or "list commands in project_local scope only" from
  the Web layer. The effective tier is whatever the internal
  `list_canonical_*` / `diff_*` calls default to (which is
  `scope="project_shared"`).
- Detail / diff / rendered routes for the same artifact types are
  cwd-locked via `get_project_root` and silently ignore `?scope_id=`.
  A client that switches projects on the list view and clicks an item
  lands on a response from a different project. This is a real bug, not
  a stylistic complaint, but its fix is out of scope here — this ADR
  only names the vocabulary the fix will use.
- `GET /context/overview` mixes tier-aware settings counts (sourced via
  `get_hooks_target_scope`, `packages/memtomem/src/memtomem/web/deps.py:57`)
  with tier-agnostic artifact counts that implicitly default to
  `project_shared` through `diff_*()` calls. The shape is consistent
  in practice today but ambiguous on paper.
- Mutator routes (POST/PUT/DELETE/sync/import on
  skills/commands/agents) are locked to server cwd and ignore
  `?scope_id=` entirely.

### Why now

Two specific drivers force the vocabulary decision now rather than
"whenever the next tier-aware feature lands":

1. **Issue #900 (artifact sync service extraction) is gated on §4
   below.** The service interface must commit to whether sync receives
   `project_root` from cwd or from `project_scope_id`. That is the §4d
   decision; it cannot be deferred and still let #900 progress.
2. **Tier-aware UI is being scoped.** Adding a `?target_scope=` filter
   to list routes, or a tier-aware overview, requires deciding the
   default behaviour now so existing clients are not silently
   broadened.

## Decision

### 1. Vocabulary

Adopt `project_scope_id` (project-root selector) and `target_scope`
(canonical tier) as the canonical request-vocabulary terms. New route
params, new response keys, and new doc copy use these names. The
literal type at `config.py:745` is already named `TargetScope`; this
ADR only lifts the matching term into request vocabulary.

### 2. Route ownership

Routes split into four ownership groups; each group's rule fixes which
of the two dimensions it accepts per request:

**2a. List / read artifact routes** — accept both `?project_scope_id=`
(with `?scope_id=` accepted as a permanent alias, per §5) and
`?target_scope=`. The `?target_scope=` default is `project_shared`
(§4b). Affects:

- `GET /context/skills` (`context_skills.py:61`)
- `GET /context/commands` (`context_commands.py:71`)
- `GET /context/agents` (`context_agents.py:85`)
- `GET /context/{skills,commands,agents}/{name}` (detail routes,
  currently cwd-locked: `context_skills.py:112`,
  `context_commands.py:110`, `context_agents.py:124`)
- `GET /context/{skills,commands,agents}/{name}/diff`
  (`context_skills.py:283`, `context_commands.py:332`,
  `context_agents.py:343`)
- `GET /context/commands/{name}/rendered` (`context_commands.py:148`)
- `GET /context/agents/{name}/rendered` (`context_agents.py:152`)

Aligning the detail / diff / rendered routes on the list-route
vocabulary remediates the silent-ignore-`?scope_id=` bug in
Background; the actual route changes ship as their own follow-up
issue (see §"Open questions").

> **2026-06 (#1277):** Remediated. The detail / diff / rendered routes
> (and the versions read route, which postdates this ADR) resolve their
> project root through the same `resolve_scope_root` dependency as the
> list routes — `?project_scope_id=` with `?scope_id=` as the permanent
> alias, unknown / stale selectors → 404. The route swap itself shipped
> with the Web project switcher (#993); the "currently cwd-locked" line
> references above describe the pre-remediation tree. Route tests
> parametrize a non-cwd scope per route family
> (`test_web_routes_context_projects.py`).

**2b. Settings routes** — originally config-driven for `target_scope`;
superseded by the parity change in §4e. Current Web settings routes
accept per-request `?target_scope=` with default `project_shared`.
Affects:

- `GET /context/settings` (alias `/settings-sync`,
  `settings_sync.py:180–181`)
- `POST /context/settings/sync` (alias `/settings-sync`,
  `settings_sync.py:209–210`)
- `POST /context/settings/resolve` (alias `/settings-sync/resolve`,
  `settings_sync.py:261–262`)

**2c. Project-discovery routes** — project-root surface only, no tier
dimension. Affects:

- `GET /context/projects` (`context_projects.py:160`)
- `POST /context/known-projects` (`context_projects.py:181`)
- `DELETE /context/known-projects/{scope_id}`
  (`context_projects.py:226`) — keeps the existing path-segment name;
  rename to `{project_scope_id}` is doc-only and deferred (§5).

**2d. Mutator routes** — sync routes accept `?project_scope_id=`
(§4d Option C) **and `?target_scope=`** (default `project_shared`,
matching §4b — see §4c for why sync resolves `target_scope` per-request
rather than from config); create / update / delete / import stay
cwd-locked. Unlock applies to:

- `POST /context/skills/sync` (`context_skills.py:335`)
- `POST /context/commands/sync` (`context_commands.py:381`)
- `POST /context/agents/sync` (`context_agents.py:390`)

Stays cwd-only:

- `POST /context/{skills,commands,agents}` (`:150`, `:205`, `:209`)
- `PUT /context/{skills,commands,agents}/{name}` (`:185`, `:238`,
  `:242`)
- `DELETE /context/{skills,commands,agents}/{name}` (`:235`, `:287`,
  `:298`)
- `POST /context/{skills,commands,agents}/import` (`:367`, `:417`,
  `:431`)
- `POST /context/{skills,commands,agents}/{name}/import` (`:394`,
  `:446`, `:460`)

### 3. Settings vs artifact `target_scope` relationship

The artifact-tier filter on Web routes is per-request and does not need
a config field of its own. `config.hooks.target_scope` remains the CLI /
config default for settings hooks, while Web settings routes now accept
the same `?target_scope=` query parameter as the other Context Gateway
surfaces.

### 4. Product-semantics decisions

The RFC draft on #901 deferred six product-semantics decisions to the
maintainer. The maintainer recommendation comment on the same issue
picked options for all six; this ADR records those picks as the
accepted decisions. The recommendation was posted on 2026-05-11
against `origin/main@4efe1c6`; the picks are unchanged here.

#### 4a. `project_local` overview visibility — Option B (hide by default)

Default Web overview and list views hide `project_local` artifacts.
They become visible only when a request explicitly sets
`?target_scope=project_local`.

Rationale: ADR-0011 §3 already pins that `project_local` has no
runtime fan-out. Default surfaces should reflect the syncable-runtime
worldview that users expect; "5 canonical, 0 generated" cards under
project_local in the default overview would be visual noise.
`project_local` items remain fully inspectable once the explicit
filter is applied.

Affects: `GET /context/overview` (`context_gateway.py:110`), list and
read routes once they accept `?target_scope=`.

#### 4b. `target_scope` default for list routes — Option B (`project_shared`)

When `?target_scope=` is omitted on `GET /context/{skills, commands,
agents}`, default to `project_shared`.

Rationale: not a behaviour change. Internal `list_canonical_*` /
`diff_*` already default to `scope="project_shared"`, so existing Web
clients are already seeing project-shared-only responses today; an
"all tiers" default would silently broaden every existing response.
Clients that want a wider view opt in via `?target_scope=`.

Affects: list routes per §2a.

#### 4c. Sync All cross-tier behaviour — Option B (current root × current tier)

`POST /context/{skills, commands, agents}/sync` operates on one
`(project_root, target_scope)` pair per invocation. `project_root` is
resolved from cwd or `?project_scope_id=` (per §4d). `target_scope` is
resolved from `?target_scope=` with default `project_shared` —
**not** from `config.hooks.target_scope`.

Why pinned to per-request, not config-driven: `config.hooks.target_scope`
currently defaults to `user` (per ADR-0010 §2 v1 default), while every
existing `generate_all_*` sync call resolves to `project_shared` because
the core APIs default `scope="project_shared"` when the caller omits it.
Sourcing sync's `target_scope` from `config.hooks.target_scope` would
silently flip bare `POST /context/{...}/sync` calls from project-shared
to user-tier writes — a behaviour change disguised as a vocabulary
clean-up. Per-request resolution with a `project_shared` default
preserves today's effective behaviour exactly, and mirrors §4b for
list routes so callers reason about both dimensions identically.

This is also why settings (§4e) and sync (§4c here) diverge on source:
settings hooks deliberately live in user-tier by ADR-0010 design,
while sync targets the artifact tier where `project_shared` has been
the de facto default since the routes shipped.

Rationale: minimal write surface, easy to explain and test, avoids
one-click cross-project or cross-tier writes. A future UI that wants
"sync everything visible" can orchestrate multiple explicit calls.

Conditional on §4d Option C below.

Affects: sync routes per §2d. The artifact sync service in #900
inherits this contract — one pair per invocation, no cross-product
batch.

> **2026-06 (#1247):** v1 web routes implement a narrower slice of Option B
> than this section implies: every artifact **write** route (create /
> update / delete / import / sync, across skills / commands / agents /
> mcp-servers) rejects `target_scope != project_shared` with 400
> (`_reject_non_shared_write` — "intentionally deferred" in its
> docstring). `target_scope` is resolved per-request exactly as decided
> here, but only the `project_shared` value is accepted on writes;
> list/read routes accept the full tier set. The engine already supports
> user-tier sync (ADR-0011 PR-E3); exposing it through the web write
> routes is tracked in #1263.

#### 4d. Mutator routes accepting `project_scope_id` — Option C (sync only)

Sync routes accept `?project_scope_id=` (and `?target_scope=` per §4c).
POST / PUT / DELETE / import routes stay cwd-locked.

Rationale: enables the key multi-project operation (syncing a selected
project from the Web project switcher) while keeping higher-risk
canonical mutations (create / update / delete / import) on the
existing cwd-only safety model. Once the Web UI gains stronger
cross-project visibility and confirmation semantics, a future ADR can
revisit the locked routes.

Affects: routes listed in §2d.

#### 4e. Settings per-request `target_scope` override — superseded by parity change

Original decision: settings routes derived `target_scope` from
`config.hooks.target_scope` only and did not accept a `?target_scope=`
query parameter.

Superseding implementation note: the Web Context Gateway now treats
settings like skills / commands / agents for request routing. Settings
GET / sync / resolve routes accept `?target_scope=` with default
`project_shared`, and the Hooks panel sends the currently selected tier.
This keeps Overview, Sync All, and the dedicated Hooks panel pointed at
the same tier.

Affects: settings routes per §2b.

#### 4f. Overview semantics consistency — tier-aware with `project_shared` default

`GET /context/overview` becomes tier-aware end-to-end (settings counts
and artifact counts both filter on `target_scope`). The default
`target_scope` is `project_shared` (matching §4b). When called with
`?target_scope=project_local`, overview reports canonical-only
presence without implying runtime fan-out, consistent with §4a.

Rationale: removes the current mixed shape where settings is
tier-aware but artifact counts implicitly default through `diff_*()`.
Default remains stable; explicit tier requests get a coherent
response.

Follows §4a and §4b.

## Backward compatibility

- `?scope_id=` is accepted forever as an alias for `?project_scope_id=`.
  No deprecation window unless a future ADR explicitly opens one. Cost
  is one alias line in the param resolver.
- Path segment `/context/known-projects/{scope_id}` stays accepted
  under its old name. The rename to `{project_scope_id}` is doc-only.
- Response field renames are additive — both `scope_id` and
  `project_scope_id` keys are present until a future deprecation pass.
- `target_scope` is already the literal name in the existing config
  field (`config.py:745`), so no rename is required there.
- CLI / MCP scope vocabulary is **out of scope** for this ADR, per the
  original issue body. This ADR does not propose any CLI flag rename
  or new MCP tool parameter.

## Consequences

- **#900 unblocked.** The artifact sync service can name its interface:
  `project_scope_id` for project-root resolution, `target_scope` for
  the artifact tier, and a sync-call contract of one
  `(project_root, target_scope)` pair per invocation (§4c).
- **Detail-route silent-ignore bug documented.** §2a calls out the
  current detail / diff / rendered routes' silent ignore of
  `?scope_id=` and points the fix at the same vocabulary; the actual
  route changes ship as their own follow-up issue.
- **One permanent alias line.** `?scope_id=` → `?project_scope_id=`
  acceptance is permanent surface area in whatever resolver helper the
  follow-up implements.
- **Tier-filter on list routes becomes possible.** Without a vocabulary
  decision the filter could not land; with this ADR it is a mechanical
  follow-up (§"Open questions").

## Considered & rejected

- **Option 4a-A (always show `project_local`).** Rejected because it
  surfaces "draft canonical, 0 generated" cards in default overview
  surfaces where users expect syncable runtime artifacts. `project_local`
  stays inspectable via explicit tier filter.
- **Option 4b-A (default `?target_scope=` to all tiers).** Rejected
  because it silently broadens existing list responses. Clients
  relying on the implicit project-shared default — which is what every
  Web client effectively sees today — would suddenly receive user-tier
  and project-local artifacts as well.
- **Option 4d-D (full mutator unlock for `?project_scope_id=`).**
  Rejected as too high-risk before the Web UI has stronger
  cross-project visibility and confirmation semantics for canonical
  edits. Sync-only is the minimum that unblocks the project switcher
  workflow.
- **A second unqualified `?scope=` query param** (the issue body's
  "alternatives considered" entry). Rejected because it doubles the
  ambiguity rather than resolving it — the whole point of this ADR is
  to name the two dimensions distinctly.
- **A separate `config.context.target_scope` field** alongside the
  existing `config.hooks.target_scope`. Rejected per §3 — divergence
  risk with no driving user need.
- **Sourcing sync's `target_scope` from `config.hooks.target_scope`**
  (i.e., treating sync the same as settings under §4e). Rejected per
  §4c — `config.hooks.target_scope` currently defaults to `user`
  while `generate_all_*` defaults to `project_shared`, so a
  config-driven sync default would silently flip bare
  `POST /context/{...}/sync` from project-shared writes to user-tier
  writes. Per-request `?target_scope=` with a `project_shared` default
  preserves today's behaviour and mirrors §4b for list routes.

## Open questions for the implementation issues

These follow-ups inherit the vocabulary fixed by this ADR; they do not
block ADR acceptance.

- Param-resolver helper accepting both `?project_scope_id=` and
  `?scope_id=` (alias per §5).
- `?target_scope=` filter plumbing on list routes (§2a, defaults per
  §4b).
- `/context/overview` tier-aware shape migration (§4f, follows §4a and
  §4b).
- Sync-route `?project_scope_id=` acceptance plus tests (§2d, §4d).
- Detail / diff / rendered route alignment with list-route vocabulary
  — distinct from #900 service extraction, but inherits the same
  vocabulary.
- Path-segment doc rename
  `/context/known-projects/{scope_id}` → `{project_scope_id}` (§2c,
  doc-only).
- Issue #900 (artifact sync service extraction) — unblocked once this
  ADR is checked in.

## References

**Issues / PRs**

- Issue #901 — this ADR's source (RFC draft and maintainer
  recommendation comments).
- Issue #900 — artifact sync service extraction, downstream consumer
  of this ADR.

**ADRs**

- ADR-0010 — settings hooks `target_scope`; introduced the 3-tier
  axis this ADR builds on.
- ADR-0011 §3 — `project_local` has no runtime fan-out (load-bearing
  for §4a).

**Source files** — line numbers reflect `origin/main` HEAD `5470e99`
at ADR-draft time; grep by symbol if they drift.

- `packages/memtomem/src/memtomem/web/routes/context_projects.py:67` —
  `resolve_scope_root` (project-root resolver).
- `packages/memtomem/src/memtomem/config.py:745` — `TargetScope`
  literal.
- `packages/memtomem/src/memtomem/web/routes/settings_sync.py` —
  settings GET / sync / resolve `target_scope` query handling.
- `packages/memtomem/src/memtomem/web/routes/context_skills.py:61` /
  `:112` / `:283` — skills list / detail / diff routes.
- `packages/memtomem/src/memtomem/web/routes/context_commands.py:71` /
  `:110` / `:148` / `:332` — commands list / detail / rendered / diff
  routes.
- `packages/memtomem/src/memtomem/web/routes/context_agents.py:85` /
  `:124` / `:152` / `:343` — agents list / detail / rendered / diff
  routes.
- `packages/memtomem/src/memtomem/web/routes/context_skills.py:335` —
  skills sync (§4d unlock target).
- `packages/memtomem/src/memtomem/web/routes/context_commands.py:381` —
  commands sync (§4d unlock target).
- `packages/memtomem/src/memtomem/web/routes/context_agents.py:390` —
  agents sync (§4d unlock target).
- `packages/memtomem/src/memtomem/web/routes/settings_sync.py:180/181`,
  `:209/210`, `:261/262` — settings get / sync / resolve, paired
  alias + canonical paths.
- `packages/memtomem/src/memtomem/web/routes/context_gateway.py:110` —
  `/context/overview`.
- `packages/memtomem/src/memtomem/web/routes/context_projects.py:160`,
  `:181`, `:226` — project discovery and known-project routes.
- `packages/memtomem/src/memtomem/context/_skip_reasons.py:25` —
  `NO_PROJECT_FANOUT_FOR_RUNTIME` skip code.
- `packages/memtomem/src/memtomem/context/_runtime_targets.py:71` —
  `RUNTIME_FANOUT_TABLE`.
