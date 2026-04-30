# ADR-0007: Namespace CRUD prod exposure model

**Status:** Proposed (deferred pending trigger)
**Date:** 2026-04-30
**Context:** Issue #586 — Settings → Namespaces tab (NS list, color,
description, rules editing) is dev-mode only. PR #604 (#582 4.10a) just
ungated the Search NS filter dropdown for prod. The deeper product
question — "what mental model do prod users actually need for namespaces?"
— stayed open. This ADR records the analysis and the leaning.

## Background

Namespaces (NS) in memtomem are a string-valued bucket on each chunk used
for filtering and isolation. They are populated three ways today
(`packages/memtomem/src/memtomem/indexing/engine.py:522-549`,
`_resolve_namespace`, in priority order):

1. **Explicit** — caller passes `namespace=` to `index_file` /
   `mem_add` / `POST /api/index`.
2. **Rules-based** — `NamespaceConfig.rules`
   (`config.py:353`, `NamespacePolicyRule` at `config.py:279`) match
   glob → namespace, evaluated first-match. Rules live in
   `~/.memtomem/config.json` (or fragments under `~/.memtomem/config.d/`).
3. **Auto-namespace** — if `enable_auto_ns=True` (default `False`),
   `{bucket}-{kind}:{segment}` is derived from path during indexing.
4. **Default** — `default_namespace` (`config.py:354`, default `"default"`).

After indexing, prod users see NS chips appear in Sources but currently
have no GUI affordance to **predefine, rename, color-code, or describe**
them — the implicit position is "NS = an indexing detail, surfaced for
filtering, not curated as a primitive."

The Web UI codifies that position. Today, after PR #604:

| Surface | Endpoint | Routing tier | Prod visible? |
|---------|----------|--------------|---------------|
| NS dropdown (search/timeline/export filters) | `GET /api/namespaces` | `namespaces_read` (prod tier) | ✅ (#604 4.10a) |
| Settings → Namespaces tab (list cards) | `GET /api/namespaces` (re-call) | same | ❌ blocked by JS dev-gate |
| NS color / description edit | `PATCH /api/namespaces/{ns}` | `admin_router` (`namespaces.py:57`) | ❌ admin only |
| NS rename | `POST /api/namespaces/{ns}/rename` | `admin_router` (`namespaces.py:76`) | ❌ admin only |
| NS delete | `DELETE /api/namespaces/{ns}` | `admin_router` (`namespaces.py:93`) | ❌ admin only |
| NS rules editor | (none — config.json file edit) | n/a | ❌ no GUI |

Frontend gate: `web/static/settings-namespaces.js:179` —
`if (STATE.uiMode !== 'dev') return;` inside `loadNamespacesTab()`. This
single line hides every CRUD interaction in prod.

## The five decisions this ADR settles

The issue body enumerated five axes. Each is treated below with options,
leaning, and rationale.

### Axis A — Model

> Auto-create only, predefine + auto-create, or predefine-only?

| Option | Behavior |
|--------|----------|
| A.1 — auto-create only | NS appears as a side-effect of indexing; no UI to predefine |
| A.2 — **predefine + auto** | both flows coexist; auto continues to fill from indexing |
| A.3 — predefine-only | turn off auto entirely; require explicit declaration |

**Leaning: A.2.** Both flows answer different questions:

- *Auto-create* is the zero-config onboarding path — drop a folder in,
  see chunks appear under a derived NS. This is what onboarding tutorials
  rely on.
- *Predefine* is the power-user flow — "I have a model in mind for how I
  want my memory partitioned, and I want to set it up before I indexize
  anything." Without this, users with that intent fall back to editing
  `config.json` rules by hand, which is the exact gap this ADR addresses.

The current `enable_auto_ns=False` default (`config.py:355`) is its own
decision (recorded in `project_auto_namespace_format`); this ADR doesn't
change it. A.2 means "we keep both, and we make predefine reachable from
the GUI for users who want it."

### Axis B — Editing scope

> Cosmetic only, or also rename / bulk delete / bulk re-tag?

| Option | Behavior |
|--------|----------|
| B.1 — **cosmetic (color, description)** | low-risk metadata; no chunk migration |
| B.2 — + rename | requires bulk-update of `chunks.namespace` column |
| B.3 — + bulk delete | requires audit + undo affordance for irreversibility |
| B.4 — + bulk re-tag | re-namespace many chunks at once |

**Leaning: B.1.** Color and description don't touch chunk rows — they
live on a NS-metadata side table and are purely cosmetic. They unblock
the "I want to make my NS panel readable" workflow without dragging in
storage migration. Rename, bulk delete, and bulk re-tag are each their
own design beasts (B.2: chunk-id stability under string-keyed NS; B.3:
undo policy and audit trail; B.4: safety affordance for ambiguity).
Each deserves a separate ADR if the product surface earns it.

The PATCH endpoint (`namespaces.py:57`) already implements color +
description on the backend; this ADR's implementation is just lifting
the JS dev-gate and admin-router classification on that one verb.

### Axis C — Empty state

> Empty panel + first-time CTA, or hide the panel entirely until ≥1 NS
> exists?

| Option | Behavior |
|--------|----------|
| C.1 — **empty + first-time CTA** | "Define your first namespace" tile + onboarding link |
| C.2 — hide until ≥1 NS exists | tab doesn't appear in fresh installs |
| C.3 — read-only list with no actions | shows existing NS only, no creation flow |

**Leaning: C.1.** Hiding the panel (C.2) makes NS undiscoverable — the
user has no way to learn "namespaces are a thing" until indexing happens
to populate one. Read-only (C.3) is a worse version of the dev-only
status quo (no actionable next step). The empty + CTA tile teaches the
primitive at the moment the user looks for it.

### Axis D — NS rules editor

> GUI editor for `NamespacePolicyRule`, or stay in `config.json` / CLI?

| Option | Behavior |
|--------|----------|
| D.1 — GUI editor | full glob-pattern UI, `{parent}` placeholder builder, etc. |
| D.2 — **config.json / CLI** | rules stay in file; NS panel surfaces the result |
| D.3 — partial GUI (simple rules) + file for advanced | hybrid |

**Leaning: D.2.** Rules carry power-user mechanics that don't translate
well to a GUI without dragging the prod UX down:

- Glob syntax (gitignore-style with negation) is hard to teach in-line.
- The `{parent}` placeholder in `NamespacePolicyRule` is non-obvious.
- Rule order matters (first-match), and reordering UI is its own design.

Keeping rules in `config.json` aligns with how STM/CLI users already
work with them, and means the GUI panel stays "look, edit cosmetics,
delete-as-power-user" rather than becoming a rules IDE. If D.1 is ever
right, it earns its own ADR.

### Axis E — Migration on rename

> If rename ends up in scope, what's the ergonomics of bulk-updating
> chunks?

| Option | Behavior |
|--------|----------|
| E.1 — bulk update `chunks.namespace` | single transaction, expensive on large stores |
| E.2 — **rename out-of-scope for this ADR** | defer; reframe as separate ADR if needed |
| E.3 — rename = "create new + tag transfer" alias | indirect; preserves chunk-id stability |

**Leaning: E.2.** Rename was the first item the issue body flagged as
non-trivial, and it depends on a chunk-stability question that's not
this ADR's to answer (see ADR-0005 for the parallel discussion on
force-reindex chunk-id semantics). Out-of-scope here means the rename
verb (`POST /api/namespaces/{ns}/rename`, `namespaces.py:76`) stays
admin-only after this ADR's implementation; a future trigger can
promote rename via its own ADR with full chunk-migration design.

## Decision

**Defer.** Leaning toward **A.2 + B.1 + C.1 + D.2 + E.2** when
implementation is triggered.

### Why hold instead of implement now

- **Single signal.** The issue body is one design audit. Below the
  "twice = pattern" bar; the same threshold ADR-0004 used to defer.
- **Post-#604 surface freshness.** PR #604 just exposed NS via the
  filter dropdown for the first time in prod. Usage will need 1-2
  release cycles to reveal whether prod users actually want CRUD or
  whether the dropdown alone closed the gap.
- **Prod-user voice is missing.** The implicit position ("NS =
  indexing detail") may be correct. Deferring lets a user-facing
  signal (issue, support comment, review) fire before we build the
  empty-state CTA and risk teaching a primitive nobody asked for.

### Trigger criteria (any one promotes to "Accepted")

1. **Prod user feedback ≥ 2 reports** along the lines of "I don't
   understand what these namespaces are" or "I want to rename / color
   / describe a namespace from the UI." Two reports = pattern, not
   anomaly.
2. **NS rules in onboarding flow.** If the onboarding wizard or
   `mm init` ever needs to surface a rule (e.g., "by default we
   bucket your projects by parent folder — want a different rule?"),
   the predefine flow becomes load-bearing and this ADR's gating
   should ramp.
3. **Multi-agent grouping verdict.** The deferred `project_multi_agent_grouping_deferred`
   (verdict 2026-05-09) may upgrade NS from "indexing detail" to
   "user-facing primitive" if the multi-agent design picks
   namespaces as the grouping unit. In that case A.2 + B.1 + C.1
   become a prerequisite.

### Implementation outline (when triggered)

In rough order, all in `packages/memtomem/src/memtomem/`:

- **PR-A — Lift the JS dev-gate; expose PATCH in prod tier.**
  - Remove the `if (STATE.uiMode !== 'dev') return;` early-return at
    `web/static/settings-namespaces.js:179`. Read-only listing in
    prod was already half-wired (the dropdown comment at line 60-62
    already says the read endpoint is prod tier).
  - Move `PATCH /api/namespaces/{ns}` from `admin_router` to
    `namespaces_read` (or to a new `namespaces_edit` mid-tier router).
    `namespaces.py:57` is the registration site. POST/{ns}/rename and
    DELETE/{ns} stay on `admin_router`.
  - The frontend already has the form for color/description. Verify
    it doesn't reach for the dev-only DELETE path on submit.
- **PR-B — Empty-state CTA + onboarding link.**
  - When `loadNamespacesTab()` resolves to an empty list in prod, render
    a first-time tile: "Define your first namespace" with a one-line
    explanation and a link to the user-guide section on NS rules.
  - Optional: a "Create" button that POSTs to a new prod-tier
    `POST /api/namespaces` (currently nonexistent — `admin_router`
    doesn't have it, NS are auto-created by indexing). If creating an
    empty NS turns out to be conceptually weird (a NS with no chunks?),
    leave creation to indexing and have the CTA link to "add a folder
    that uses your NS rule" instead.
- **PR-C (gated by separate triggers, not this ADR's) — rename / bulk
  delete.**
  - Each is its own ADR with chunk-migration design.

## Consequences

- **NS becomes a half-curated primitive.** Read + cosmetic edit in
  prod, structural ops (rename, delete) in dev. This is intentional —
  it teaches the model without exposing the parts that need migration
  policy.
- **Documentation surface widens.** The user-guide gains an NS section
  for prod (today the topic is implicit in "auto-namespace format"
  power-user docs). Doc-fanout follows the project's default-change
  convention — README + getting-started + user-guide same PR.
- **`namespaces_edit` router (or equivalent).** Adding a mid-tier
  router introduces a new tier between `_PROD_ROUTERS` and
  `_DEV_ONLY_ROUTERS` (the actual list names in `web/app.py:63, 80`).
  If the count stays at one verb (`PATCH`), inline classification on
  the existing prod router is fine; no new file needed.
- **Empty state is a teaching moment.** First-time installs with no
  indexing yet now see "what is a namespace" instead of an empty UI.
  This is a small but material onboarding improvement that doesn't
  exist today even in dev mode.

## Considered & rejected upstream

- **Promote rename in this ADR.** Rejected: rename's chunk-migration
  question is a separate ADR-shaped beast (string-keyed FK, chunk-id
  stability under bulk update, undo / audit). Bundling would
  drown this ADR.
- **Show CRUD as a "danger zone" in prod.** Rejected: the dev-only
  classification today exists precisely because the structural ops
  aren't audited. Calling them "danger zone" but enabling them anyway
  shifts a hard-rule into a polite-warning, which is the wrong
  trade-off until rename has a real design.
- **Drop dev-mode entirely from this surface.** Rejected: dev-mode
  remains the right home for rename and delete until they earn their
  own ADR. This ADR ungates only what it can ungate cleanly.

## References

- Issue #586 — ADR placeholder, this document is the deliverable.
- PR #604 — Search NS filter dropdown ungating (#582 4.10a). Sibling,
  not superseded.
- ADR-0004 — same "deferred pending trigger" shape this ADR mirrors.
- ADR-0005 — chunk-id stability discussion (informs why rename is
  out-of-scope here).
- `packages/memtomem/src/memtomem/web/static/settings-namespaces.js:172` —
  `loadNamespacesTab()`; line 179 dev-gate.
- `packages/memtomem/src/memtomem/web/static/settings-namespaces.js:59` —
  `loadNamespaceDropdowns()`; comment 60-62 records that the read
  endpoint is prod tier (post-#604).
- `packages/memtomem/src/memtomem/web/routes/namespaces.py:17, 22` —
  `admin_router` / `list_namespaces`; routing tiers.
- `packages/memtomem/src/memtomem/web/routes/namespaces.py:57` — PATCH
  (cosmetic edit, prod-promotion target).
- `packages/memtomem/src/memtomem/web/routes/namespaces.py:76, 93` —
  rename / DELETE (stay admin-only).
- `packages/memtomem/src/memtomem/config.py:279` —
  `NamespacePolicyRule`.
- `packages/memtomem/src/memtomem/config.py:353-355` —
  `NamespaceConfig` (default_namespace, enable_auto_ns).
- `packages/memtomem/src/memtomem/indexing/engine.py:522-549` —
  `_resolve_namespace` priority.
- Sibling deferred decision: multi-agent grouping (verdict
  2026-05-09; tracked in maintainer notes outside this repo).
