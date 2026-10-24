# ADR-0038: Goal records are Pinned Context blocks, selected by project scope and namespace

**Status:** Proposed
**Date:** 2026-10-24
**Extends:** [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §5 and §6 —
the two-gate write model and the project-aware read default, applied to one more
kind of pinned block
**Extends:** [ADR-0028](0028-per-project-agent-team-namespaces.md) §3 — a
namespace such as `shared:<project>` or `security` gains its own objectives, not
only its own memories
**Qualifies:** [ADR-0032](0032-per-namespace-day-files.md) §"Alternatives
considered" — the rejected "directory per namespace", which this ADR adopts for
the pinned tree only, for the reason given in §2

## Context

memtomem stores *what happened* well and *what we are trying to do* badly.

- **The handoff skill records checkpoints, not state.** It writes one immutable
  record per save (`objective`, `completed`, `blockers`, `next_action`, …) into
  `shared:<project-slug>` at `project_local` scope
  (`packages/memtomem-claude-plugin/skills/handoff/SKILL.md`). An updated
  objective is a new record; the old one stays under the same tags, and resume
  reads the union of matching rows. That is the right shape for "where did the
  last runtime stop", and the wrong one for "what is this workstream's current
  objective, and which of its criteria are done".
- **Pinned Context has no namespace axis.** `PinnedBlock` carries `scope` and an
  optional `agent_id`, and `PinnedContextStore.list(agent_id=…)` keeps blocks
  whose `agent_id` is `None` or the caller's (`pinned.py`). A user who runs a
  `security` track and a `perf` track in one project can pin both tracks'
  objectives only as general blocks, so every compose carries both, inside a
  6,000-character pinned budget.
- **The namespace is already known where it would be used.**
  `ContextAssembler.compose(…, namespace=…)` accepts a namespace but applies it
  only to the retrieved leg. `mem_session_start` derives an effective namespace
  for every session — the explicit argument, the bound agent's namespace, the
  server's current namespace, or `default` — and has nothing to return for it.
- **Pinned blocks already reach the places a goal must reach.** The STM proxy
  calls `context_compose` with its configured namespace, places the returned
  pinned blocks first, and exempts them from its surfacing dedup. A goal that is
  a pinned block is delivered there without an STM change, within two STM-side
  limits: each pinned bullet is cut to the proxy's preview length (300
  characters by default), and the whole memory block can still be truncated at
  its injection limit.

The same parts also say what *not* to build. The pinned store already owns the
ADR-0011 gates (Gate B consent for `project_shared`, Gate A secret refusal), the
read-only and tier-round-trip refusals, and the exclusion of pinned roots from
the composed path's retrieved leg. A parallel store would have to restate all of
them.

## Decision

### 1. A goal is a pinned block with `kind: goal`

Goals live in the pinned tree as Markdown files with YAML frontmatter and are
read through `PinnedContextStore`. There is no goal table. The file is the
record, a person can edit it, and a `project_shared` goal is committed with the
repository like any other shared memory.

Goals and ordinary blocks are separate kinds. They share the pinned root and the
compose budget, but not directories: every goal lives under its own `goals/`
subtree (§2), so the two never occupy the same path. Ordinary blocks keep every
behaviour they have today, including same-id shadowing across tiers and agents.
The rules below apply to goals only. A goal and an ordinary block may carry the
same `id` without affecting each other.

### 2. Goals get their own subtree, with a namespace level

```text
<scope_dir>/pinned/general/<id>.md                                      (today, ordinary)
<scope_dir>/pinned/agents/<agent_id>/<id>.md                            (today, ordinary)
<scope_dir>/pinned/goals/general/<id>.md                                (new, project-wide goal)
<scope_dir>/pinned/goals/agents/<agent_id>/<id>.md                      (new)
<scope_dir>/pinned/goals/namespaces/<ns-dir>/<id>.md                    (new)
<scope_dir>/pinned/goals/namespaces/<ns-dir>/agents/<agent_id>/<id>.md  (new)
```

`<ns-dir>` is the slug-plus-digest name ADR-0032 §1 defined for day files,
without the date. The slug is built in four steps: replace every character
outside `[A-Za-z0-9._-]` with `-`, strip leading and trailing `-`, cap the result
at 60 bytes on a UTF-8 boundary, and strip `-` again. The name is then
`<slug>-<digest>`, or `<digest>` alone when the slug comes out empty. The digest
is the first 16 hex characters of the SHA-256 of the exact namespace bytes. One
shared helper produces both this name and the day-file name, so the two cannot
drift. The reasons ADR-0032 gave for the digest (`:` is not a legal Windows
filename character, case-folding filesystems, the 255-byte component cap) apply
unchanged.

Only goals live under `goals/`, and goals live nowhere else. The ordinary pinned
surfaces (`pinned list`, `get`, `set`, `delete` and their MCP twins) keep their
current coordinates and do not read or write the `goals/` subtree. They also
leave alone any file elsewhere whose frontmatter says `kind: goal`, for example
one moved there by hand. Today's reader walks the pinned tree recursively, so the
implementation has to exclude both explicitly. A goal file found outside
`goals/` is not a goal either: `goal_get` reports it as misplaced.

ADR-0032 rejected a directory per namespace because a sanitized directory name
would feed `enable_auto_ns`'s parent-folder derivation and the `memory_dirs`
registry. Neither hazard is created here. The directory sits inside an already
registered tree and registers nothing. The name is never read back as a
namespace. A goal's namespace is its frontmatter field (§3). Every write through
the goal tools indexes the file with that namespace stated explicitly; a
project-wide goal, which has no namespace field, is indexed under the
configured default namespace, also stated explicitly. Nothing is left to the
rules, to `enable_auto_ns` or to the default fallback.

A mismatch can still arise for a goal file that no tool has written yet, for
instance one created in an editor and first indexed by the watcher. Its rows
then carry whatever namespace resolution gives, so the goal can be missing from
a `namespace="security"` search and present in a default one. Goal *selection*
is unaffected, because compose and the goal tools read files, not rows. The first
tool write repairs the rows, and ADR-0033's preservation rule then keeps them on
later re-indexes. Until then the mismatch is reported (§6), not silent.

### 3. A goal file's fields

| Field | Meaning |
|---|---|
| `id` | Authored name, `[A-Za-z0-9._-]`, stable across edits. |
| `kind` | `goal`. |
| `namespace` | The namespace the goal belongs to, or absent for a project-wide goal. Must agree with the directory. |
| `status` | `active`, `blocked`, `paused`, `completed`, or `superseded`. |
| `priority` | Ordering inside the pinned budget, as for other blocks. |
| `version` | Integer, incremented by every tool write. |
| `updated_at` | UTC timestamp of the last tool write. |
| `done_criteria` | List of `{text, done}` items. |
| `next_step` | One line. |
| `blockers` | List of strings. |
| `valid_from` / `valid_to` | The existing temporal-validity frontmatter, unchanged in meaning. |

The body is the objective, in prose.

A goal's **identity is its coordinate**: scope tier (with its project), namespace,
agent and `id`. Every goal tool addresses one goal by that full coordinate. A
lookup that leaves part of it out returns every visible goal that matches,
together with each one's coordinate, and never picks one.

Two goal files with the same `id` can exist in different tiers, projects,
namespaces or agent directories. A project pulled from git, a migrated store or
an editor can create one at any time, and no write-time check can prevent that.
What this ADR guarantees instead is that goals do not shadow each other (§4).

### 4. Which goals a compose returns

Selection runs over the pinned roots visible to the caller: the user root, and
the project's two roots when the call resolves a project.

1. **Status.** `active` and `blocked` goals are selected. `paused`, `completed`
   and `superseded` goals are not. A blocked goal stays in front of the agent,
   because that is when its blockers need attention.
2. **Namespace.** A goal with no namespace is always eligible. A goal with a
   namespace is eligible only if the compose call names that namespace
   *exactly*. A comma list names each of its members. A glob names none: globs
   select retrieval rows, and a pattern that happens to match a goal's namespace
   is not a request for that goal. `None` or an empty filter names nothing, so
   only project-wide goals are selected.
3. **Agent.** A goal in an `agents/<agent_id>/` directory is eligible only for
   that `agent_id`.
4. **No shadowing.** If two or more eligible goals share an `id`, wherever they
   live, none of them is returned. The id is reported in a new `ambiguous_ids`
   list beside the existing `omitted_block_ids`. A project goal does not silently
   override a user goal of the same name, because a goal is a statement of
   intent and not a default to override. Resolving the duplicate is the user's
   decision.
5. **Order and budget.** Selected goals and selected ordinary blocks are placed
   in one list and ordered by the existing rule, highest `priority` first and
   then by `id`. The pinned budget is filled in that order.

### 5. What a compose sends for a goal

Consumers read a pinned block's `content`. For a goal, `content` is a canonical
rendering of the file. It begins with a short actionable prefix — the objective's
headline, a `Next:` line, and a `Blockers:` line when there are any — and then
continues with the rest of the objective and the criteria as a checklist.

The prefix has a fixed upper bound of 300 characters, so a consumer that keeps
only the start of a block, as the STM proxy does by default, still receives what
to do next and what is in the way. The goal tools enforce the bound by capping
the headline, next step and blocker lengths at write time. The exact caps belong
to the implementation.

The 2,000-character cap for one block and the 6,000-character pinned portion of
a compose apply to the full rendering, not to the body alone. A goal whose
rendering would not fit the remaining pinned budget is omitted whole and named
in `omitted_block_ids`, which is how compose treats any block today. Goals have
no retrieval fallback, because the composed path excludes the pinned roots from
its retrieved leg. That holds for every pinned block today and is not changed
here.

### 6. Goals are written only through goal tools

Four actions are added to the existing `pinned` tool category: `goal_set`,
`goal_get`, `goal_update` and `goal_resolve`. They are reachable through `mem_do`
in every tool mode. They go in the existing category because a new one would
also reopen which categories the `standard` mode exposes, a question this ADR
does not need to answer.

- **Create is create.** `goal_set` creates a goal at a coordinate where no file
  exists, and refuses when one does. It never replaces a goal or an ordinary
  block.
- **Every change is compare-and-set.** `goal_update` and `goal_resolve` take the
  `version` and the content hash the caller read. A mismatch refuses the write
  and returns the current file. The hash catches an editor save, which does not
  change `version`.
- **Tool writers are serialized.** Each goal mutation does its read, compare,
  increment, write and index while holding the lock that guards that file. Two
  tool writers that both read version 3 cannot both succeed.
- **Editors are covered on a best-effort basis.** A writer that ignores the lock
  can still land between the compare and the replace. The hash is checked once
  more immediately before the replace, and only the window that remains after
  that check is uncovered. That window is the cost of keeping goals in files a
  person can edit, and this ADR accepts it.
- **The file is the record.** Once the file write succeeds, the change has
  happened. If indexing then fails, the write is not rolled back and not
  repeated. The response says that indexing failed. Selection is unaffected,
  because it reads files, and the next goal write or an ordinary re-index
  repairs the rows.
- **Automatic transitions.** Adding a blocker to an `active` goal makes it
  `blocked`. Removing the last blocker from a `blocked` goal makes it `active`.
  Nothing else changes status without an explicit call.
- **Resolution.** `goal_resolve` sets `status` to `completed` or `superseded`, and
  sets `valid_to` to the current UTC date, in one write to that one file. The goal
  leaves compose at once. Default search keeps returning it until the end of that
  UTC day, because a date-only `valid_to` is inclusive through 23:59:59. That tail
  is accepted: validity is a ranking default, not a boundary
  ([ADR-0036](0036-id-addressed-access-scope-vs-namespace.md) §2), and
  instant-precision bounds are a separate change. The resolved file is the
  archive. It stays on disk, stays indexed, and answers `as_of` queries. No
  summary is written elsewhere, and nothing is deleted.
- **Replacing a goal** is two explicit calls: `goal_resolve` with `superseded` on
  the old goal, then `goal_set` for the new one. There is no single
  "replace" operation and no link between the two files that any rule reads.
- **Every other writer refuses goals.** Goal files are indexed, so their chunk
  ids reach every surface that mutates a source by chunk id. Those surfaces would
  bypass the compare-and-set rule, and a delete would erase the archive. So every
  writer other than the goal tools refuses a source whose frontmatter says
  `kind: goal`, and names the goal tools instead. That covers `pinned set` and
  `pinned delete`, `mem_edit` and `mem_delete`, the web chunk `PATCH` and
  `DELETE` routes, and any later writer addressed by chunk id. Maintenance
  policies (expire, archive, consolidate, tag) skip goal sources entirely.
- **Diagnostics.** `goal_get` reports four conditions next to the goals it
  returns: frontmatter that disagrees with its directory, a `kind: goal` file
  outside the `goals/` subtree, rows whose stored namespace differs from the
  declared one, and ambiguous ids. `mm doctor` is not
  the home for these, because ADR-0035 gives it the runtime axis only.

### 7. Sessions show goals, read-only

`mem_session_start` adds the goals that §4 selects to its response, using the
session's effective namespace and its bound agent: objective, next step and
blockers. It never writes a goal. Proposing a change to a goal from session
activity is [ADR-0039](0039-memory-steward-review-first.md)'s subject, and goes
through review.

### 8. The ADR-0011 gates apply unchanged

A goal write to `project_shared` needs `confirm_project_shared`, on every path
that writes one, including a reviewed approval (ADR-0039 §4). A goal whose bytes
match a secret pattern is refused at the chokepoint. Both run on the exact bytes
about to be written, frontmatter included. A read-only or excluded target is
refused before anything is written.

### 9. What the implementation decides

This ADR fixes the invariants above. Contract details are settled in the
implementation's own design review before code, as ADR-0030 §10's snapshot
protocol was. These include field length caps, argument and error shapes, the
lock helper and its acquisition from async code, the response shape for partial
coordinates, and how the compose schema version is announced.

## What this guarantee is, and is not

**It is a selection rule.** It decides which objectives reach an agent's context
for a given project and namespace. It also makes "what are we trying to do here"
a file that people, agents and git can all read and change.

**It is not an access boundary.** A goal in another namespace is not hidden from
anyone who asks for it by coordinate, `goal_get` or search. That is the same
statement ADR-0028 makes for namespaces and ADR-0036 §2 makes for validity.

**It is not coordination.** Two agents may hold conflicting goals in two
namespaces, and this ADR does not reconcile them. Inside one goal file, the
compare-and-set rule makes conflicting tool writes visible. It does not decide
between them.

**It is not an automatic writer.** Nothing in this ADR changes a goal without an
explicit tool call. Proposals derived from activity are ADR-0039's, and they wait
for review.

## Consequences

- Four registered actions are added, so the tool-count constants in
  `test_docs_guards.py` and the guides that mirror them change together. The
  `pinned` category's `mem_do help` output keeps its 4,000-character budget, and
  the new actions have to fit inside it.
- `PinnedBlock` gains `namespace` and the goal fields, and the compose path gains
  namespace-aware goal selection. The compose response gains `ambiguous_ids`.
  That is a new key, so the compose schema version is bumped and announced the
  way schemas 2–4 were.
- The STM proxy does not pass `agent_id` to compose. Agent-specific goals
  therefore do not reach it. Project-wide and namespace goals do, cut to its
  preview length, which §5's prefix bound is sized for. Any change to either
  limit belongs to the STM repository.
- The `SessionStart` hook in `memtomem-claude-automation-plugin` checks versions
  only, and the base plugin ships no hooks. Showing goals at session start, and
  having handoff resume read the active goal, are plugin changes and land
  separately.

## Alternatives considered

**A goal table in SQLite with its own file tree.** Rejected. It would restate
both ADR-0011 gates, the read-only and round-trip refusals, the composed-path
exclusion and STM delivery. It would also make the database a second source of
truth beside the files.

**Extend the handoff record.** Rejected. A handoff is an immutable checkpoint in
a 1,200-character, fenced, tag-addressed format, and resume reads the union of
matching rows. Making it mutable would break the property that makes it useful,
and it would still not reach compose.

**Encode the namespace in the block id** (`security--release-goal`). Rejected.
Namespaces contain `:`, which no Windows filename can, and a lossy id prefix
reintroduces the collision ADR-0032 §1 solved with a digest.

**Let goals shadow like ordinary blocks.** Rejected. Shadowing is how a project
overrides a user *default*. Two goals with one name are two intents, and picking
the higher tier hides the other one from the agent without anyone deciding to.

**Unique goal ids enforced at write time.** Rejected as a guarantee. Visibility
changes when the working directory changes, a git pull can add a file with an
existing id, and a store migration can merge two trees. A rule that only the
write path checks would be false as soon as any of those happens, so ambiguity is
detected where goals are read (§4.4).

**A single "replace goal" operation that links the old and new files.**
Rejected. The link has to name the old goal by something stable across tiers, and
the replaced state has to survive whatever later happens to the new goal. Each
version of that rule considered either resolved a name to the wrong file or let
the old goal reappear. Two explicit calls (§6) carry no such rule.

## References

- [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §5–§6 — the gates and the project-aware read default.
- [ADR-0028](0028-per-project-agent-team-namespaces.md) — namespaces as routing convention; `shared:<project>`.
- [ADR-0030](0030-explicit-preview-pull.md) §10 — the precedent for settling an implementation contract in its own design review.
- [ADR-0032](0032-per-namespace-day-files.md) §1 — slug plus digest.
- [ADR-0033](0033-force-reembed-vs-namespace-reassignment.md) — stored-namespace preservation on re-index.
- [ADR-0035](0035-mm-doctor-runtime-axis.md) — `mm doctor` owns the runtime axis.
- [ADR-0036](0036-id-addressed-access-scope-vs-namespace.md) §2 — validity and namespace hiding are defaults, not boundaries.
- [ADR-0039](0039-memory-steward-review-first.md) — how changes to goals are proposed and reviewed.
- `docs/guides/pinned-context.md` — current pinned and compose contract.
