# ADR-0026: Context Gateway first-time-user onboarding & comprehension layer

**Status:** Proposed
**Date:** 2026-06-14
**Context:** A first-time-user end-to-end smoke test of the Context Gateway
web UI (driven through Playwright against an isolated, seeded HOME) found
that the dashboard is *functionally* complete but *conceptually* opaque to
a new user. Tracking issue: #1353. This ADR records the proposed
onboarding / information-architecture (IA) layer that sits **on top of**
the already-accepted Context Gateway architecture (ADR-0009 info surface,
ADR-0011 canonical scope hierarchy, ADR-0015 request vocabulary, ADR-0016
three-tier store, ADR-0021 portal, ADR-0023 transfer). It does **not**
re-open any of those decisions.

The Gateway exposes a four-axis model — artifact-type × tier × project ×
sync-state — without ever stating the single idea that makes the rest
legible: **memtomem keeps one source-of-truth store under `.memtomem/`
and pushes it one-way out to runtime tools (Claude Code, Codex, Kimi…).**
Because that model is never surfaced in the UI, every downstream label
("canonical", "tier", "enroll", "fan-out", "Sync" vs "Import", and four
overlapping status badges) reads as undefined jargon, and a first-time
user cannot answer five basic questions: *What is canonical? Sync or
Import? Which project am I in? Where do errors live? Why enroll?*

## Scope and non-goals

This ADR is about the **user-facing display + onboarding layer only**. It
is deliberately **non-normative** on the request / identifier vocabulary:

- It does **not** rename `project_scope_id` or `target_scope` — those are
  the request-vocabulary terms fixed by **ADR-0015**, kept verbatim for
  backward compatibility.
- It does **not** preempt the deferred `target_scope` → `target_tier`
  *identifier* rename (ADR-0016 §"Open questions" §2, tracked by #922,
  review window 2026-08-11). Any display-term change proposed here is a
  separate, display-only concern and must not be read as resolving #922.
- It does **not** change any route, schema, gate, or sync behaviour
  (ADR-0011 §3 `project_local` no-fan-out, ADR-0015 §4 product semantics,
  ADR-0023 transfer gates all stand). It changes copy, one display-label
  helper, and additive UI affordances.
- **Caveat — ADR-0016 §7 (load-bearing).** ADR-0016 §7 ("CLI / Web UI
  user-facing names") already **decided** that user-facing surfaces use
  the literal tier tokens `user` / `project_shared` / `project_local` and
  **rejects** display aliases ("Personal" / "Team" / "Local Draft"). The
  glossary below therefore keeps the literal tokens by default and only
  *defines* them. Friendlier tier-value labels would be a **narrow
  supersession of ADR-0016 §7 for Web display copy only** — that is a
  maintainer decision (Q-A), not something this ADR assumes. So this ADR
  is non-normative on prior decisions **except** that Q-A explicitly puts
  ADR-0016 §7 on the table.

The string-level localization/copy *defects* found by the same smoke test
are tracked and fixed separately (#1348 raw move/copy verbs, #1349 portal
empty-state empty quotes, #1350 `reason_code` → i18n leaks, #1351
`settings.ctx.*`/`settings.hooks.*` ko.json gap + install-guide literals,
#1352 wording polish). Those make the existing words *correct and
translated*; this ADR makes the words *comprehensible in the first place*.
The two layers are complementary: the glossary below is intended to be the
single source of truth that #1351's bulk ko.json pass localizes against
(so EN and KO converge on the same user-facing terms instead of
transliterating "canonical" → "캐노니컬").

## Decision

### 1. Surface the mental model in the UI (do not leave it to docs)

State the model once, in the Overview, in plain language:

> memtomem keeps one **Store** (your master copies, in `.memtomem/`).
> **Sync** pushes them out to your **Runtimes** — Claude Code, Codex,
> Kimi. **Import** pulls existing ones back in. The flow is one-way:
> edit in the Store, then Sync.

```
   ┌─────────────────┐                          ┌──────────────┐
   │  STORE          │      ── Sync (push) ──▶   │  Claude Code │
   │  .memtomem/     │                      ├──▶ │  Codex       │
   │  (your masters) │      ◀── Import (pull) ── │  Kimi …      │
   │  Scope: User ·  │      (subset, fixed order)│  (RUNTIMES)  │
   │  Shared · Draft │                          └──────────────┘
   └─────────────────┘   one-way fan-out; a Runtime copy is
                         overwritten on the next Sync.
```

The Store is the single write source. Sync is one-way out (fan-out to
every detected runtime). Import is the narrow exception — pulling a
runtime copy back in, only from runtimes that are read-readable (other
runtimes are export-only). Drafts (the `project_local` tier) are
deliberately never pushed (ADR-0011 §3).

The reason this must be in the UI and not only in docs: the audits showed
every on-screen label is *only* interpretable relative to this model,
which today exists only in the maintainer's head, in code comments, and
across ADR-0011/0015/0016.

### 2. A single user-facing display glossary

Adopt one consistent set of **display** terms. The left column is the
current jargon that should disappear from user-facing strings (it survives
only in code, request params, and ADRs).

| Current jargon (UI) | Recommended display term | One-line definition |
|---|---|---|
| canonical / canonical store | **Store** ("Stored source" already used at `en.json:591`) | The single master copy of an item, in `.memtomem/`; the one place Sync reads from. |
| runtime / runtimes | **Runtimes** (kept) | The AI tools memtomem pushes to: Claude Code, Codex, Kimi… detected on your machine. |
| fan-out | **Sync** / "pushes" (verb) | Copying the Store's items out to every detected runtime — one-way. |
| Sync / Sync All | **Sync** (kept) | Push the Store's items out to your runtimes. |
| Import | **Import** (kept) | Pull an existing item from a runtime back into the Store. |
| tier (the `target_scope` axis) | **OPEN QUESTION — do _not_ reuse "Scope"** (see §"Open questions" Q-A) | Where in the Store a copy lives and how widely it applies. |
| user / project_shared / project_local (values) | **Keep the literal tokens** + a one-line tooltip — see Q-A. ADR-0016 §7 pins these tokens and rejects display aliases; friendlier labels ("Personal"/"Team"/"Draft") would need to supersede ADR-0016 §7 (Web display only). | All your projects / committed to git, your team gets it / gitignored draft, never pushed. |
| enroll | **Track** ("Enable sync") | Opt a project in to receiving pushes — like adding a git remote. |
| Server CWD | the project's real label + a **`(current folder)`** marker | The folder the server launched in; show the real label, not a synthetic second identity. |
| status: out of sync / not in runtime | **Out of sync → Sync** / **Not in runtimes → Sync** | Store has changes/items the runtime lacks; Sync to push. |
| status: not yet imported | **In runtime only → Import** | A runtime has an item the Store doesn't; Import to bring it in. |

`project_shared` means "git-tracked", **not** "shared between agents"
(inherited verbatim from ADR-0011 / ADR-0015 Terminology). Whatever Q-A
decides for the rendered tier values, the `project_shared` tooltip must
carry that meaning ("committed to git — your team will see it"); the
`project_local` tooltip must carry "gitignored draft — never pushed"
(ADR-0011 §3).

**Vocabulary-collision note (load-bearing).** The proposal that seeded
this ADR suggested displaying the tier axis as "Scope". That is rejected
here as written, because **ADR-0015 explicitly retired unqualified
"scope"** — it already names two distinct dimensions, `project_scope_id`
(project-root selector) and `target_scope` (tier). Introducing "Scope" as
the *display* word for the tier axis would re-create exactly the ambiguity
ADR-0015 fought, and would collide with the project axis users already
read as "Project". The tier-axis display term is therefore left as an
explicit open question (Q-A), with non-colliding candidates: keep "Tier"
(and define it), "Storage location", or "Visibility". The current UI label
"저장 위치" / "Stored in" is already a non-colliding choice and may simply
need a definition rather than a rename.

### 3. Phased delivery — Minimal → Moderate → (validate) → Bold

The three approaches evaluated are not mutually exclusive; they are
increasing depths of the same fix (surface the model → restructure
exposure → re-vocabularize). Ship in order; each phase de-risks the next
and is independently shippable.

| Dimension | **P0 Minimal** | **P1 Moderate** | **P2 Bold** |
|---|---|---|---|
| What changes | Additive copy only: Overview primer + canonical→runtime diagram + always-visible status legend + 3 glossary tooltips + 1 confirm-string rewrite + 1 display-label helper edit | Default **Simple mode** with progressive disclosure: tier/project axes hidden behind an **Advanced** toggle; per-type inline-action rows; 3-state display remap | Full re-frame around a `git push` metaphor (verb rename Sync→Push↑ / Import→Pull↓, status collapse to ahead/behind/in-sync) |
| First-user impact | States the model once + keeps a legend in view; ~80% of comprehension gain | Collapses the steepest cliff (mandatory tier axis + 4 statuses) into one primary task | Highest comprehension for the git-native audience; direction is in the verb |
| Effort / Risk | **S–M / Low** (reuses tested patterns; no badge-ladder/gate/confirm-math change) | M / Medium (default-flip discipline, two label layers) | L / Medium-high (terminology churn breaks external docs/screenshots; one-way) |
| Reversibility | Trivial (all additive) | High (Advanced toggle restores today's UI verbatim) | Low (central-verb rename is a one-way product decision) |

**Recommendation: accept P0 now, scope P1 next, and gate P2 behind a
first-run user test.** P2's central-verb rename is the single
highest-comprehension move but is communication-heavy and irreversible; it
must only follow validation (see §Validation).

#### P0 — Minimal (proposed for immediate scheduling)

Each item: **what** · **where** · **acceptance criterion**.

1. **Overview primer banner** · clone the `tab-help-bar` pattern (e.g.
   `index.html:161-163`, also used by the Index/Sources tabs) into
   `#settings-ctx-overview` after the desc at `index.html:567`, scoped
   `data-help-tab="ctx-overview"`; new key `settings.ctx.primer` (EN+KO)
   · *Banner renders expanded on first visit; dismiss persists via
   `body.help-hidden`, independent of the other tabs' dismiss state.*
2. **Store→runtime flow diagram** · a new `#ctx-flow-diagram` flex child
   inserted into the existing `.ctx-overview-header` between
   `.ctx-overview-root` and `.ctx-overview-runtimes`
   (`context-gateway.js:1495-1500`); ~6 lines CSS · *An explicit
   `Store ──Sync→── Runtimes` arrow is visible without scrolling; reuses
   the existing header box (no new container).*
3. **Always-visible status legend** · one `.help-tip` "i" popover on the
   Overview `<h2>` (`index.html:569`); new key
   `settings.ctx.status_legend` mapping each of the four statuses
   (`status_in_sync`/`status_out_of_sync`/`status_missing_target`/
   `status_missing_canonical`, `en.json:497-500`) to its single resolving
   action · *Hovering/focusing the "i" reveals "In sync = nothing to do ·
   Not in runtime → Sync · Not yet imported → Import · Out of sync →
   Sync". No badge string mutated.*
4. **Glossary tooltips on the worst on-screen terms** · `.help-tip` next
   to the tier-filter label and "Runtimes" (`context-gateway.js:1500`),
   plus rewrite the confirm jargon leak at `en.json:415`
   (`move_copy_shared_confirm_message`) so raw `canonical` /
   `project_shared` no longer appear in the move/copy confirmation ·
   *"canonical"/"project_shared" no longer appear raw in the UI; tier +
   runtimes definitions are one hover away.* (Coordinate with #1348, which
   also touches this confirm copy.)
5. **Consistent project naming** · edit `_ctxScopeDisplayLabel`
   (`context-gateway.js:579`) so the cwd case *appends* a
   `(current folder)` marker instead of *replacing* the whole label with
   "Server CWD"; new key `settings.ctx.cwd_marker`; unlabeled folders keep
   the `server_cwd` fallback · *The same folder shows one consistent name
   everywhere it routes through this helper (dropdown, overview header,
   move/copy confirm).*
6. **i18n + gate hygiene** · pair every new key EN+KO in the same change;
   bump `?v=N` cache-bust on changed JS/CSS; add new keys to
   `test_i18n.py` parity; the primer's `data-i18n-html` passes the
   innerHTML/langchange checklist · *`test_i18n.py` green; cache-bust
   bumped; langchange re-renders the primer in place.*

**New Overview (P0) — low-fi mockup:**

```
┌─ Context Gateway ───────────────────────────────────────────┐
│  ╔══════════════════════════════════════════════════ [✕]╗   │ ← P0-1 reused tab-help-bar
│  ║ memtomem keeps one STORE (.memtomem/). SYNC pushes it ║   │
│  ║ to RUNTIMES (Claude·Codex·Kimi); IMPORT pulls back.   ║   │
│  ╚═══════════════════════════════════════════════════════╝   │
│  Context Gateway (i)←legend       [Refresh] [Sync All]       │ ← P0-3 one help-tip
│  ┌─ ctx-overview-header (existing box) ────────────────────┐ │
│  │ Project: Alpha Service (current folder)   ← P0-5 naming │ │
│  │ 📦 Store ──[ Sync → ]──▶ Runtimes (i) [Claude][Codex]  │ │ ← P0-2 diagram into existing box
│  └─────────────────────────────────────────────────────────┘ │
│  ┌ Skills 3/3 ✅┐ ┌ Commands ⚠ →Sync ┐ ┌ Agents ⚠ →Import ┐ │ ← existing tiles, unchanged
│  └──────────────┘ └──────────────────┘ └──────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

#### P1 — Moderate (proposed for the next iteration)

1. **Simple/Advanced toggle** (`localStorage` flag) shows/hides
   `#ctx-control-bar` and the Projects/Wiki/Hooks nav. Default Simple;
   Advanced restores today's UI verbatim; `#ctx-control-bar` stays in the
   DOM (hidden) so the existing hoist guard stays green.
2. **One-line verdict + per-type inline actions** — each surfaced problem
   carries its resolving verb button on its own row; Sync vs Import is
   never ambiguous.
3. **3-state Simple labels** (display remap only) — Advanced keeps the
   original four; no status string mutated; confirm create/overwrite math
   untouched.
4. **Default-flip fan-out** — Simple-as-default ships with its
   onboarding-docs fan-out in the same change (per the repo's
   default-change discipline); consider a staged opt-in (Advanced default,
   flip after the user test) — see Q-F.

**Simple default mode (P1) — low-fi mockup:**

```
┌─ Context Gateway ───────────────────────────  [ Advanced ▢ ] ┐
│ ▾ How sync works                                        [✕]  │
│   memtomem keeps the master copy of your skills/commands/    │
│   agents here and copies them out to your AI tools. One-way: │
│   edit here, then Sync.                                      │
│   [ This project's store ] ──Sync──▶ [ Claude · Codex · Kimi]│
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 3 items not yet in your tools.        [ Sync to tools ]  │ │
│ └─────────────────────────────────────────────────────────┘ │
│  Skills      2 in your tools · 1 not yet           [ Sync ]  │
│  Commands    all in your tools                        ✓      │
│  Agents      1 changed — needs re-sync             [ Sync ]  │
│  MCP servers 1 in a tool, not saved here         [ Import ]  │
│  ⓘ What the states mean                                      │
└──────────────────────────────────────────────────────────────┘
```

#### P2 — Bold (deferred, gated on a first-run user test)

Re-frame as a "git push console": rename Sync→Push↑ / Import→Pull↓,
collapse the statuses to ahead/behind/in-sync (+ two error states), add a
`git status`-style headline verdict, and resolve the tier display-term
(Q-A) under the same metaphor (e.g. Global/Shared/Draft). This relabels
~40 `settings.ctx.*` keys in both locales and breaks external
docs/screenshots, so it is **deferred pending** the §Validation user test.
Backend `reason_code`s and request vocabulary (ADR-0015) are unchanged.

## Consequences

- A first-time user can state the canonical→runtime model and pick Sync
  vs Import without hovering — the #1 documented failure — after P0.
- The glossary (§2) becomes the term set #1351's ko.json pass localizes
  against; #1351 and this ADR must agree on terms before either lands
  user-facing strings, to avoid double-churn.
- No behavioural, route, schema, or gate change in P0/P1. P2 changes only
  display copy + status presentation, never `reason_code`s or request
  params (ADR-0015 preserved).
- P0 item 4 overlaps #1348's confirm-copy edit at `en.json:415`;
  sequencing must be coordinated (Q-E) so the two do not collide.
- The tier display-term decision (Q-A) is intentionally *not* taken here,
  to avoid colliding with ADR-0015 / pre-empting #922.

## Validation

Because the audits behind this ADR were produced by an *informed* tester
(who already knows the model), the comprehension claims must be re-checked
against genuinely naive behaviour before the irreversible P2 ships.

**Heuristic re-check (cheap, per phase):** re-run a Nielsen pass after P0
and confirm the previously-violated heuristics (visibility of system
status, match-real-world, recognition-over-recall, help/docs) now pass on
the Overview surface. Add a `test_i18n.py`-style source-grep guard that
fails if the purged jargon (`canonical`, `project_shared`,
`project_local`, `fan-out`) re-enters user-facing strings outside code
comments.

**Lightweight first-run user test (5–6 participants, ~20 min):** recruit
Claude Code / Codex CLI developers who have never used the Gateway; fresh
isolated HOME; single task *"get this project's skills into Claude Code."*
Observe without hints. Probes: (1) direction — "which button fixes 'Out of
sync' vs 'Not yet imported' without hovering?"; (2) model — "where's the
master copy, what does Sync do?"; (3) identity — "how many projects, which
are you in?"; (4) scope — "you want your teammate to get this skill —
which tier?"; (5) **safety (P2 gate)** — "what will Push do to the Claude
copy that's ahead?" (must predict "overwrite"); (6) recovery — seed a
parse-error item, "what's wrong, how fix?". **Pass bars:** P0 ships if
probes 1–3 succeed for ≥4/6 without docs; P2 is gated on probe 5
succeeding for ≥5/6 and the status-merge not costing power users the
create-vs-overwrite distinction.

## Open questions

- **Q-A. Tier vocabulary — axis term AND value display.** Two coupled
  sub-decisions: (i) the tier-**axis** display term — reject "Scope" (it
  collides with ADR-0015's retired unqualified "scope" and the project
  axis); pick a non-colliding term (keep/define "Tier", or "Storage
  location" / "Visibility" / the existing "Stored in"). (ii) the tier-
  **values** — ADR-0016 §7 currently pins the literal tokens
  `user`/`project_shared`/`project_local` for user-facing surfaces and
  **rejects** display aliases. The glossary keeps the literals by default;
  adopting friendlier labels (e.g. "Personal"/"Team"/"Draft") requires a
  **narrow supersession of ADR-0016 §7 for Web display copy only**. Decide
  literals-plus-tooltip vs. supersede-0016-§7. Neither (i) nor (ii) may
  pre-empt the #922 `target_scope`→`target_tier` *identifier* rename.
- **Q-B. P2 directional verbs — full rename vs. softer label.** Three
  options, not two: (a) keep Sync/Import and carry direction only via the
  diagram + legend (P0/P1); (b) **soft** — keep the action names
  Sync/Import but add secondary labels/icons ("push to runtimes" ↑ /
  "pull into Store" ↓) for the directional cue without git semantics;
  (c) **full** rename Sync→Push↑ / Import→Pull↓ (highest comprehension,
  one-way, breaks external docs/screenshots). Which?
- **Q-C. Status-merge — mixed multi-runtime states.** The overview
  aggregates `(runtime, name, status)` triples, so one artifact can be
  in-sync for one runtime and missing/out-of-sync for another
  (`context-gateway.js:1511`). P2's ahead/behind/in-sync collapse must
  define how a **mixed** item renders (worst-status wins? per-runtime
  chips?) and whether list/Sync-All rows still expose overwrite risk
  *before* the confirm modal — not just "create-vs-overwrite moves to the
  modal". Is the collapse acceptable given mixed states?
- **Q-D. Simple-mode empty-tier handling.** P1 Simple pins to
  `project_shared`; a user whose items live only in the User tier sees an
  empty Overview until they find Advanced. Split into two: (i) the hint —
  "turn on Advanced"; (ii) **auto-switching the active tier is risky** (it
  conflicts with the stable `project_shared` default per ADR-0015/0016).
  Safer option: a **read-only empty-state summary** that names which other
  tier holds items ("3 items in your User tier — open Advanced to manage")
  *without* changing the active tier. Hint, auto-switch, or read-only
  summary?
- **Q-E. Sequencing + a single glossary owner.** P0 rewrites `en.json:415`
  (overlaps #1348) and defines terms #1351 must translate. Beyond issue
  order: name **one glossary owner / source-of-truth** that #1348–#1352
  and this ADR all localize against, so the same EN/KO keys are not
  re-churned by parallel changes. Land P0 before/after/interleaved?
- **Q-F. Default-flip blast radius.** P1's Simple-as-default is a default
  change requiring same-change docs fan-out **and** it hides
  navigation/control surfaces. The decision should weigh discoverability
  (will power users find Advanced?) and a rollback signal (telemetry or a
  visible toggle), not only docs. Staged opt-in first (Advanced default,
  flip after the user test), per the repo's default-change discipline?
- **Q-G. Accessibility & localization of the new visual onboarding.** P0/P1
  add a primer banner, a Store→Runtimes diagram, and a hover/focus legend.
  Define acceptance criteria before scheduling: screen-reader text for the
  diagram (it must not be the only carrier of the model), keyboard access
  to the legend/tooltips, no color-only status encoding, dark-mode
  rendering, and RTL/localized-width layout for the ASCII-style diagram.
  What is the a11y/localization bar P0 must clear?

If this ADR is accepted with P2 left deferred, add a TRACKER.md row for
Q-B (trigger: the §Validation first-run user test) pointing at #1353.

## References

**Issues**

- #1353 — tracking issue (first-time-user onboarding & IA); this ADR's home.
- #1348 / #1349 / #1350 / #1351 / #1352 — string-level companions (raw
  move/copy verbs; portal empty-state; `reason_code`→i18n leaks; ko.json
  gap + install-guide literals; wording polish).
- #922 — deferred `target_scope`→`target_tier` identifier rename
  (ADR-0016); Q-A must not pre-empt it.

**ADRs**

- ADR-0009 — Context Gateway dashboard info surface (the surface this
  layer annotates).
- ADR-0011 §3 — `project_local` has no runtime fan-out (load-bearing for
  the diagram + the "Draft, never pushed" definition).
- ADR-0015 — request vocabulary `project_scope_id` / `target_scope` and
  the retirement of unqualified "scope" (load-bearing for §2 / Q-A);
  this ADR is the display-layer counterpart and does not change it.
- ADR-0016 §7 ("CLI / Web UI user-facing names") — **pins** the literal
  tier tokens for user-facing surfaces and rejects display aliases
  (load-bearing for Q-A(ii); the glossary keeps literals unless Q-A elects
  to supersede it for Web display copy). §"Open questions" §2 — deferred
  `target_scope`→`target_tier` *identifier* rename (#922); Q-A must not
  pre-empt it.
- ADR-0021 — Context portal (the Projects portal whose dual project
  identity P0-5 fixes).
- ADR-0023 — cross-project artifact transfer (the move/copy flow whose
  confirm copy P0-4 rewrites).

**Source files** — line numbers reflect the branch at draft time; grep by
symbol if they drift.

- `packages/memtomem/src/memtomem/web/static/index.html:161-163` —
  `tab-help-bar` pattern (P0-1 source); `:567` overview desc; `:569`
  Overview `<h2>`.
- `packages/memtomem/src/memtomem/web/static/context-gateway.js:1495-1500`
  — `.ctx-overview-header` / `.ctx-overview-root` / `.ctx-overview-runtimes`
  (P0-2 insertion point); `:579` `_ctxScopeDisplayLabel` (P0-5); `:1511`
  the `(runtime, name, status)` aggregation comment (load-bearing for Q-C
  mixed multi-runtime states).
- `packages/memtomem/src/memtomem/web/static/locales/en.json:415`
  `move_copy_shared_confirm_message` (P0-4); `:497-500` status keys
  (P0-3); `:591` "Stored source".
