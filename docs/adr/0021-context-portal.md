# ADR-0021: Context Portal — multi-project state board over the gateway dashboard

**Status:** Accepted
**Date:** 2026-06-02
**Context:** Two independent design reviews (`agy`/Antigravity UX notes and a
`codex`/Claude technical synthesis) converged on the same ask: grow the
Context Gateway dashboard from a single-project info surface into a
**"Context Portal" — a project × runtime × artifact state board**. This ADR
records the scope of that elevation and how it relates to the constraints
ADR-0009 set for the dashboard.

## Background

ADR-0009 (Accepted 2026-05-08) answered one question for the dashboard: is it
a *thin info surface* or an *authoritative action cockpit*? It chose
**informational cockpit** — read-only context above the tile grid, mutation
**push-only** (canonical → runtime), pull/import left to leaf pages, and
**single current-project** semantics (Info-2/§4). ADR-0009 left any aggregate
multi-project overview to a future dedicated surface/ADR; the code carries the
matching deferred note (the `_compute_detected_runtimes` docstring's
"`registered_roots` shape … deferred", #829).

Since then the surrounding machinery has matured well past what the dashboard
exposes:

- **Known-projects store** — `context/projects.py`
  (`KnownProjectsStore`, `discover_project_scopes`, `compute_scope_id`) plus
  `GET /api/context/projects`, `POST`/`DELETE /api/context/known-projects/...`
  already persist and enumerate multiple project scopes, and the UI has a
  lightweight **add/remove/scope-list** surface over them (`context-gateway.js`
  add/remove flows + the POST/DELETE known-projects routes). What is missing is
  *management*: stale/missing health, label edit, and search/sort.
- **Per-artifact status** — `/api/context/overview` aggregates **per-artifact
  status vocabularies** that differ by type: skills/commands/agents emit
  `in_sync` / `out_of_sync` / `missing_target` / `missing_canonical` /
  `parse_error`; settings emits `in sync` / `out of sync` / `missing target` /
  `skipped` / `error` (no `missing_canonical`); MCP servers omit
  `missing_canonical`; and `local_draft` is synthesized by the overview
  aggregation (`_count_context_statuses`), not by the `diff_*` functions.
- **Runtime detection** — `_compute_detected_runtimes`
  (`web/routes/context_gateway.py`) reports `{name, available}` by OR-ing
  on-disk artifact surfaces (ADR-0009 §1), but cannot answer "is this client
  installed and is memtomem registered into it" — the registration writers
  live only in `cli/init_cmd.py` / `cli/uninstall_cmd.py`.

The two review docs — a codex/Claude synthesis and an Antigravity (`agy`) UX
note, both ephemeral design inputs rather than committed artifacts — ask for
four themes between them; this ADR commits a focused **v1** and defers the
rest. The reviews disagreed on nothing material — `agy`
framed the UX (project switch + one-click sync, provider filter chips,
discovery/`init` linkage, conflict dialog); `codex`/Claude framed the data
model (Project Portal, Runtime status, Artifact Inventory, Docs Drift Doctor).

## Decision

The Context Gateway dashboard is elevated to a **Context Portal**: a board
that makes "which project, which runtime, which artifact is in what state"
legible at a glance, with a **bounded** set of actions. The tab name
`Context Gateway` and the `ctx-*` section vocabulary are kept; "Portal" is the
overview's role, not a rename (minimizes i18n / deep-link / test churn).

### v1 scope (this ADR)

- **A. Project Portal** — extends the existing add/remove/scope-list surface
  into a management view over the known-projects store: list registered
  projects with **stale/missing** health, inline label edit, search/sort, and
  **unregister** (remove the registry entry only). Today's `KnownProjectsStore` is
  add/delete-only — `add()` is idempotent and does **not** update labels, and
  discovery renders basename labels — so v1 adds
  `PATCH /api/context/known-projects/{scope_id}` (label update) and makes
  `GET /api/context/projects` return the stored label when present. Deleting
  actual `.memtomem/` or runtime files is **out of scope** (see Open questions).
- **B. Runtime/Provider status** — detected/installed/registered status and
  filtering across the four **provider clients** the user runs: **Claude,
  Antigravity, Codex, Kimi**. These map one-to-one onto the artifact fan-out
  runtimes `KNOWN_RUNTIMES = (claude, gemini, codex, kimi)`
  (`context/_runtime_targets.py`) — **Antigravity is the gemini-family client**
  (a CLI + IDE on the `~/.gemini/...` paths) and stands in for the `gemini`
  runtime, which keeps its internal id. The standalone **Gemini CLI**
  (`~/.gemini/settings.json`, deprecated upstream 2026-06-18) and generic MCP
  editors with no artifact fan-out (Cursor, Windsurf, Claude Desktop, …) are
  **out of scope for v1**. Detection is read-only and **not** keyed on whether
  `mm init` auto-writes the config (`mm init` automates only Claude / project
  `.mcp.json` / Kimi; Antigravity is a paste-hint registration the registry
  still *detects*). This *client/provider* axis is the user-facing surface; the
  internal fan-out runtime set is unchanged — **v1 adds no new fan-out
  runtime**. It is a *multi-client* status model, **not** a single-provider
  selector.
- **C. Sync All** — the existing `ctx-sync-all-btn` gains per-phase progress
  and a result summary; no new backend endpoint (see §"Sync orchestration").

### Relationship to ADR-0009 — narrow supersession

ADR-0009 is **narrowly superseded on two axes**; everything else it decided
stands.

1. **Single-project → multi-project, for read/management only.** The Portal
   adds listing, switching, and lifecycle management (label, unregister) across
   the registered set — resolving the read/management half of the
   `registered_roots` shape ADR-0009 deferred to #829. **Mutations remain
   single active-project per invocation.** **Artifact** writes (Sync All)
   target exactly one `(project_root, target_scope)` — preserving ADR-0016 §5
   ("writes land in exactly one tier per invocation") and ADR-0001's
   per-project pipeline unchanged. **Registry-lifecycle** mutations (unregister,
   label edit) mutate `known_projects.json` for a single `project_scope_id`,
   carry **no artifact-tier dimension** (ADR-0015 classifies project-discovery
   routes as project-root-only), and therefore sit **outside** ADR-0016's
   artifact-tier write rule. Cross-project *bulk* sync is explicitly deferred
   (Open questions).

2. **Read-only info surface → bounded actionable surface.** The dashboard may
   now host actions that are (a) **push-only from canonical** (Sync All —
   already ADR-0001/ADR-0009 semantics, just surfaced from the board) and
   (b) **registry-only lifecycle** (project unregister, label edit — these
   mutate `known_projects.json`, not artifacts). Pull/import stays a leaf-only
   action with a pointer, exactly as ADR-0009 §"Decision" requires. No new
   destructive (file-deleting) action is introduced in v1.

### Runtime registration detection — trust boundary

B requires reading client config files
(e.g. `~/.claude.json`, `~/.codex/config.toml`, the Antigravity CLI/IDE
`mcp_config.json` / `mcp.json` set, `~/.kimi/mcp.json` — the authoritative set
is `docs/guides/mcp-clients.md`) to answer "installed?" and
"memtomem/mms registered?". These files contain provider tokens and API
keys. The detection layer is therefore constrained:

- **Read-only.** The registry never writes a client config; registration
  remains owned by `cli/init_cmd.py` / `cli/uninstall_cmd.py`. Path resolvers
  are shared from a single source so detector and writer cannot drift.
- **No raw config egress.** The HTTP/MCP surface returns only booleans, a set
  of `registered_locations`, and `$HOME`-collapsed `config_paths` — never raw
  config bytes. Any string that could embed a secret passes the existing
  `memtomem.privacy.scan` redaction (the `_redact_message` pattern at
  `web/routes/context_gateway.py`).
- **No STM coupling.** "mms registered" is decided by inspecting the client
  config's MCP-server map for the `mms` / `memtomem` keys — **never** by
  importing `memtomem_stm` or constructing an in-process STM client (forbidden
  cross-repo coupling, CLAUDE.md invariant).
- **`docs/guides/mcp-clients.md` is the source of truth** for the set of
  registration locations per **in-scope provider client** (§Decision B). The
  registry must enumerate *all* documented locations for those clients (a
  client can register in user / project-local / committed / IDE spots); a probe
  of only the top-level location would false-negative common registrations. A
  conformance test pins "registry locations ⊇ documented locations for the
  in-scope clients" so a newly-documented location cannot silently drop out.
  (Out-of-scope generic editors are not covered by this test.)

### Sync orchestration — reuse, do not add a backend endpoint

A combined backend `POST /api/context/sync-all` was considered and rejected
for v1 on two grounds:

- `_gateway_lock` (`web/routes/_locks.py`) is a non-reentrant
  `_LoopLocalLock`. A single endpoint that called the per-artifact sync route
  handlers while holding the lock would re-acquire the same lock and deadlock.
- `_sync_atomic.sync_atomic_artifact` (`context/_sync_atomic.py`) **excludes
  skills by design** (staging-dir promotion shape) and does not model the
  settings / MCP-server result shapes, so it cannot be the one engine behind a
  uniform sync-all.

Instead, v1 reuses the **existing front-end `ctx-sync-all-btn`**, which calls
each per-artifact `POST /sync` sequentially — each acquiring and releasing
`_gateway_lock` independently, sidestepping both problems. A future atomic
backend endpoint (lock-free per-type core helpers under one outer lock, with
phase-preserving native results aggregated into a summary) is an Open question.

## Consequences

- The Portal reuses existing infra: `GATEWAY_SECTIONS` routing, the ADR-0009
  tile/deep-link carrier, tier gating, the #972 stale-response guard,
  `emptyState`, and the per-artifact diff/sync/import routes. The only new
  backend module is a read-only `runtime_registry`.
- `/api/context/overview`'s response shape is **extended additively** — the
  `detected_runtimes: [{name, available}]` field keeps `available` for
  backward compatibility and gains `installed` / `memtomem_registered`. No
  field is removed or renamed.
- MCP parity: runtime status is exposed through `mem_context_detect` via a
  **detect-scoped** include (or a dedicated parameter), **not** by widening the
  shared include set used by `init`/`generate`/`diff`/`sync` — adding
  `runtimes` to the shared set would make `mem_context_sync(include="runtimes")`
  silently accepted and ignored. MCP `sync` continues to support only
  `skills` / `agents` / `commands` / `settings`; `mcp-servers` and an `all`
  alias are not part of the MCP sync contract (Open questions).

## Open questions & v1 scope-outs

Two kinds of entry. **Tracked deferred decisions** (§1–§2) are genuine
architectural decisions with event-driven triggers; each has a one-line row in
`docs/adr/TRACKER.md`. **v1 scope-outs** (§3–§5) are features intentionally
left out of v1, recorded here for context but **not** tracked as deferred
decisions (no TRACKER row) — per the TRACKER authoring rule that reserves rows
for triggered deferred decisions, not roadmap items.

1. **Docs Drift Doctor** — surface impl-path vs upstream-doc-path divergence
   (e.g. Antigravity CLI vs IDE config locations, Kimi `.kimi` vs `.kimi-code`,
   Gemini CLI deprecation). *Trigger:* upstream path drift produces a support
   issue or repeated false-negative runtime detection that the
   mcp-clients.md-SoT conformance test does not already catch. (TRACKER row.)
2. **Atomic backend `POST /api/context/sync-all`** — lock-free per-type core
   helpers under one outer `_gateway_lock`, native per-type results
   (incl. skills staging + settings + MCP) aggregated. *Trigger:* a need for
   cross-type all-or-nothing sync, or a user report of partial-failure pain
   from the sequential front-end orchestration. (TRACKER row.)
3. **Inline 3-button conflict editor** — Keep-User / Overwrite-Canonical /
   side-by-side Compare-and-merge on `409`/out-of-sync, replacing the v1
   "view diff" leaf pointer. *Revisit if:* repeated user friction resolving
   conflicts via the leaf diff. (v1 scope-out — not tracked.)
4. **Destructive project delete** — deleting `.memtomem/` and/or runtime
   materializations, behind a typed confirmation and a dev-tier gate, distinct
   from registry unregister. *Revisit if:* a concrete user need to purge a
   project's canonical/runtime state from the Portal. (v1 scope-out — not
   tracked.)
5. **MCP Sync-All parity** — an `all` alias and `mcp-servers` support in the
   MCP `sync` contract so headless agents reach full Sync-All parity with the
   web button. *Revisit if:* an agent/headless workflow needs MCP-driven
   mcp-server sync. (v1 scope-out — not tracked.)

## Alternatives considered

- **Rename the tab to "Context Portal."** Rejected for v1: forces a migration
  of i18n keys, deep-link `?section=` values, `GATEWAY_SECTIONS`, and browser
  tests for no functional gain. "Portal" is adopted as the overview's role.
- **Full four-theme delivery in one epic.** Rejected: the reviews' four themes
  span greenfield (Drift Doctor) to one-line tweaks; bundling them violates
  "one focused change per PR" and delays the high-value Project Portal slice.
- **Backend `/sync-all` endpoint in v1.** Rejected — see §"Sync orchestration".

## References

- ADR-0001 (per-project sync pipeline), ADR-0009 (dashboard info surface —
  *narrowly superseded here*), ADR-0011 / ADR-0015 / ADR-0016 (tier vs runtime
  scope; one-tier-per-write).
- `docs/guides/mcp-clients.md` — registration-location source of truth.
- Design inputs (ephemeral, not committed; their substance is summarized in
  §Background): a codex/Claude technical synthesis and an Antigravity (`agy`)
  UX note. The durable record is this ADR and the implementation PRs; the plan
  behind them was reviewed across four `codex` rounds before acceptance.
- Deferred-question rows: `docs/adr/TRACKER.md`.
