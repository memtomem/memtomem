# ADR-0028: Per-project agent-team namespaces (flat + convention)

**Status:** Accepted
**Date:** 2026-06-29
**Context:** Maintainers run multiple agent *teams* — often one (or more) per
project — against a single memtomem LTM server, and asked how to keep one
project's team memory from bleeding into another's. The multi-agent docs
(MCP server instructions, the agent-team tutorial) teach only the
*namespace* axis (`agent-runtime:<id>` + a single global `shared`), so a
team that follows them verbatim lands every project's memory in the one
global tier and the teams collide. This ADR records the analysis and pins
the decision: per-project teams are expressed by **convention over flat
namespaces**, not by a new schema-level project axis. It also resolves the
multi-agent namespace-grouping question deferred 2026-05-09.

## Background

memtomem has **two orthogonal scoping axes**, and the confusion comes from
expecting one axis to do the other's job:

1. **Namespace axis — *who*.** A string bucket on each chunk. Multi-agent
   tools produce exactly two shapes: `agent-runtime:<agent_id>` (per-agent
   private, derived in `multi_agent.py:_resolve_agent_namespace` and
   `session.py`) and `shared` (one global constant,
   `constants.py:SHARED_NAMESPACE = "shared"`). `validate_namespace`
   enforces a strict 2-segment arity on the `agent-runtime:` prefix
   (`constants.py:186-192`, test-pinned via issue #496's
   `HOSTILE_NAMESPACES`), so `agent-runtime:<project>:<agent>` is rejected —
   the namespace **cannot** carry a project segment under that prefix.

2. **Scope axis — *where* (ADR-0011/0015/0016).** A per-row `scope`
   (`user` / `project_shared` / `project_local`) plus a `project_root`
   column, anchored to which `.memtomem/memories[.local]` directory the
   chunk lives in and resolved against cwd via
   `search._resolve_project_context_root`. This is about **residency tier
   and git-tracking**, not agent identity — `project_shared` means
   "git-tracked", not "shared between agents" (ADR-0011).

The two axes are independent and composable. Critically, the scope axis is
**not** the team-distinction mechanism: `mem_add` / session-bound writes
default to `scope="user"` (`memory_crud.py`), i.e. the global tier with
`project_root = NULL`, so relying on scope to separate teams would require
passing `scope=` on every write *and* still would not partition the `shared`
namespace (which the scope filter does not touch). The clean per-project
boundary therefore lives on the **namespace** axis, applied by convention.

## Decision

### 1. Namespaces stay flat — no schema-level project axis

The `agent-runtime:` 2-segment arity invariant is **preserved**. We do not
add `agent-runtime:<project>:<agent>` nesting, a `current_project` field on
`AppContext`, or a `project_root` qualifier on the namespace. Rationale:
the arity rule is a deliberate, tested security/consistency invariant
(`agent-runtime:` is *convenience* isolation, not a security boundary — see
`constants.py`), and the per-project need is fully met by convention below
at zero migration cost.

### 2. Per-project PRIVATE memory — encode the project in the flat `agent_id`

```
mem_agent_register(agent_id="projA-planner")
mem_session_start(agent_id="projA-planner")      # → agent-runtime:projA-planner
mem_add(content="...")                            # session-bound to that namespace
```

`projA-planner` is a valid `agent_id` (`[A-Za-z0-9._-]`, no leading dash),
derives the single-segment namespace `agent-runtime:projA-planner` (passes
`validate_namespace`), stays hidden from default `mem_search` via the
`agent-runtime:` system prefix (`constants.py:_DEFAULT_SYSTEM_PREFIXES`),
and is auto-resolved + auto-scoped by `mem_agent_search` and the
session-bound write contract. Project B's team uses `projB-*`. The two
teams never collide because the literal `agent_id` strings differ. **No code
change is required for this case.**

**Amendment (2026-07-21, #1875).** The session-bound write contract requires a
*bound* agent, and `"default"` is now reserved to mean **no agent bound** at
every binding surface (`mem_session_start`, `mm session start`, the LangGraph
adapter's `start_agent_session`). Previously `mem_session_start()` with no
`agent_id` bound the literal `"default"` and routed all subsequent writes into
`agent-runtime:default` — a hidden system namespace, so the caller could not
read back their own entries with a plain `mem_search`. An unbound session now
leaves writes where they would land with no session at all. This does not touch
the arity invariant (§1) or the `projA-planner` convention above; it only
removes an accidental binding. The reservation applies to *inferred* bindings
only: `agent-runtime:default` stays addressable through an explicit
`agent_id=` / `namespace=` argument, which is the sole recovery path for chunks
written there before the fix (there is no namespace-rename primitive).

### 3. Per-project SHARED memory — a `shared:<project>` bucket

Team-shared-but-project-isolated knowledge goes to a per-project shared
bucket named `shared:<project>` (a valid 2-segment namespace; the
`agent-runtime:` arity check does not apply to the `shared` prefix):

```
# write (either form)
mem_add(content="team decision ...", namespace="shared:projA")
mem_agent_share(chunk_id=..., target="shared:projA")     # already supported

# read — agent's private + the project's shared bucket, one call
mem_agent_search(query=..., shared_namespace="shared:projA")
# or, tool-agnostic, via the comma-joined namespace filter mem_search accepts:
mem_search(query=..., namespace="agent-runtime:projA-planner,shared:projA")
```

To make the read ergonomic, `mem_agent_search` gains an optional
`shared_namespace: str | None = None` parameter: when set, `include_shared`
merges that bucket instead of the global `SHARED_NAMESPACE`. Default
behaviour is unchanged (`None` → `"shared"`), so existing single-team
workflows are untouched. `mem_agent_share` already accepts an arbitrary
validated `target=`, so no change is needed on the write side.

### 4. The scope axis stays orthogonal

ADR-0011 scope tiers (`user` / `project_shared` / `project_local`) remain a
**residency** concern, composable with the conventions above but not the
team-distinction mechanism. A team that also wants its memory git-tracked
under the project can independently pass `scope="project_shared"` on writes;
that is a separate decision and out of scope for this ADR.

## Consequences

- **`shared:<project>` is not a system-namespace prefix.**
  `_DEFAULT_SYSTEM_PREFIXES` is `("archive:", "agent-runtime:")`, so a
  `shared:<project>` bucket *does* surface in a default `namespace=None`
  `mem_search` (unlike `agent-runtime:`). For orchestrated teams that always
  pass explicit namespaces this is benign; widening the default hide-list is
  a default-behaviour change with onboarding-doc fan-out and is **deferred**
  (see Tracker).
- **Session-level project binding is not added here.** `mem_session_start`
  still has no `project=` / `scope=`; each per-project shared write names
  its bucket (or scope) explicitly. A future `project=` sugar that derives
  both the `agent_id` prefix and the shared bucket is **deferred** (see
  Tracker) — pinning the convention first de-risks that work.
- **Docs become the primary fix.** The agent-team tutorial and the
  `03_agent_memory_patterns` notebook gain a worked two-project example;
  the `mem_agent_search` docstring documents `shared_namespace=`.

## Alternatives considered

- **`agent-runtime:<project>:<agent>` namespace nesting** — rejected (XL).
  Reverses the tested 2-segment arity invariant (#496), needs a schema
  migration (`sessions`, `namespace_metadata`), an `AppContext.current_project`
  field, and threading through every derivation/search/UI site, for a need
  the flat convention already meets.
- **Scope axis as the team boundary** — rejected. Scope is residency/
  git-tracking, defaults to global `user` for agent writes, and never
  partitions the namespace layer; using it for team isolation is both
  fragile and incomplete.
- **`project=` sugar on `mem_session_start` / `mem_agent_register`** —
  deferred, not rejected. Good ergonomics, but medium-risk parity work
  across MCP/CLI/LangGraph; revisit once the convention has real usage.
