# ADR-0026: Context Gateway first-time-user onboarding & comprehension layer

**Status:** Accepted & partially shipped — P0, P1a/P1b, and the P1
Simple-as-default flip (D-F) are merged. P2's **verb rename** (Sync→Push /
Import→Pull) is **adopted via ADR-0030 §2** (campaign 1, PR-E) as reversible
copy — see the P2 supersession note below; P2's **status collapse** remains
deferred. See §"Implementation status (as of 2026-06-18)". **D-F shipped 2026-06-18 as a
reversible experiment** (Simple is now the default) rather than gated on the
§Validation user test — 6 naive participants proved impractical, and the
Advanced toggle (per-user) plus the one-line constant (global) are the
rollback. The §Validation test + kit remain available if P2 (irreversible)
ever needs naive evidence.
**D-A is now decided (2026-06-17):** the Web display uses friendly tier
labels ("User" / "Project (shared)" / "Project (local)") + a "Stored in"
axis — a narrow supersession of ADR-0016 §7 for Web display copy only, with
the CLI / request identifiers unchanged. The remaining provisional decisions
(D-B / D-C) stay open, gated on the §Validation user test.
**Date:** 2026-06-14 (status updated 2026-06-18)
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
  *identifier* rename — ADR-0016 §"Open questions" raises it; TRACKER.md
  (row "0016 §2") binds it to #922 with a 2026-08-11 review window. Any
  display-term change proposed here is a separate, display-only concern and
  must not be read as resolving #922.
- It does **not** change any route, schema, gate, or sync behaviour
  (ADR-0011 §3 `project_local` no-fan-out, ADR-0015 §4 product semantics,
  ADR-0023 transfer gates all stand). It changes copy, one display-label
  helper, and additive UI affordances.
- **Caveat — ADR-0016 §7 (load-bearing).** ADR-0016 §7 ("CLI / Web UI
  user-facing names") **decided** that user-facing surfaces use the literal
  tier tokens `user` / `project_shared` / `project_local` and **rejects**
  display aliases ("Personal" / "Team" / "Local Draft"). D-A (decided
  2026-06-17) **narrowly supersedes ADR-0016 §7 for Web display copy only**:
  the Context Gateway **Web** surface renders friendly labels ("User" /
  "Project (shared)" / "Project (local)") + a "Stored in" axis and defines
  them, while CLI `--scope=` flags, `?target_scope=` params, config, MCP, and
  path segments keep the literal tokens unchanged. So this ADR is
  non-normative on prior decisions **except** that D-A narrowly supersedes
  ADR-0016 §7 for Web display copy.

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
| tier (the `target_scope` axis) | **"Stored in" / "Store in"** (저장 위치) — _decided, D-A (2026-06-17)_; "Tier" kept only as the code/concept word; "Scope" rejected (collides with ADR-0015) | Where in the Store a copy lives and how widely it applies. |
| user / project_shared / project_local (values) | **Web display: friendly labels** "User" / "Project (shared)" / "Project (local)" + a one-line tooltip (_decided, D-A_ — narrow ADR-0016 §7 supersession, Web display only); CLI / params / config keep the literal tokens. | All your projects / committed to git, your team gets it / gitignored draft, never pushed. |
| enroll | **Track** ("Enable sync") | Opt a project in to receiving pushes — like adding a git remote. |
| Server CWD | the project's real label + a **`(current folder)`** marker | The folder the server launched in; show the real label, not a synthetic second identity. |
| status: out of sync / not in runtime | **Out of sync → Sync** / **Not in runtimes → Sync** | Store has changes/items the runtime lacks; Sync to push. |
| status: not yet imported | **In runtime only → Import** | A runtime has an item the Store doesn't; Import to bring it in. |

`project_shared` means "git-tracked", **not** "shared between agents"
(inherited verbatim from ADR-0011 / ADR-0015 Terminology). Under D-A's
friendly rendered labels, the `project_shared` tooltip must still
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
read as "Project". "Scope" stays rejected as the display word. The
tier-axis display term was an open question (D-A); it is **now resolved
(2026-06-17)** to the already-shipped, non-colliding UI label
**"Stored in" / "저장 위치"**, which the glossary defines (the other kept
candidates were "Storage location" and "Visibility"; "Tier" survives only
as the code/concept word).

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
   `#ctx-control-bar` and the Projects/Wiki/Hooks nav. Simple is the
   *target* default — and per D-F it shipped: Simple is the default since
   2026-06-18 (a reversible flip; the §Validation test was skipped as
   impractical, the toggle is the rollback). Advanced restores today's UI
   verbatim; `#ctx-control-bar` stays
   in the DOM (hidden) so the existing hoist guard stays green.
2. **One-line verdict + per-type inline actions** — each surfaced problem
   carries its resolving verb button on its own row; Sync vs Import is
   never ambiguous.
3. **3-state Simple labels** (display remap only) — Advanced keeps the
   original four; no status string mutated; confirm create/overwrite math
   untouched.
4. **Default-flip fan-out** — Simple-as-default ships with its
   onboarding-docs fan-out in the same change (per the repo's
   default-change discipline) — shipped 2026-06-18 as a reversible flip (the
   staged opt-in was skipped as impractical; the toggle is the rollback) — see D-F.

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
`git status`-style headline verdict, and — as a *separate, later*
supersession of the now-decided D-A display copy — optionally re-frame the
tier labels under the same metaphor (e.g. Global/Shared/Draft). This relabels
~40 `settings.ctx.*` keys in both locales and breaks external
docs/screenshots, so it is **deferred pending** the §Validation user test.
Backend `reason_code`s and request vocabulary (ADR-0015) are unchanged.

> **Supersession note (2026-07-19, ADR-0030 §2).** The **verb-rename** portion
> of P2 — Sync→Push / Import→Pull across UI labels, CLI help, and guides — is
> **adopted** by ADR-0030 §2 (campaign 1, PR-E), a **narrow supersession of
> this §P2's deferral** that reverses the "#1353 P2 NO-GO — deferred pending a
> naive-user validation gate" posture. It ships as **reversible copy** (locale
> / help / guide relabel; `reason_code`s, request/response identifiers, and
> route paths stay frozen — exactly the cut described above), so it does not
> require the §Validation gate. The **status collapse** to ahead/behind/in-sync
> is **not** adopted: ADR-0030 §4 supersedes it with a two-axis preview model
> (`content_status` × `gate_status`) that keeps `differs` / `in sync` / `out of
> sync` rather than collapsing to a single directional set. The tier-label
> re-framing under the same metaphor remains deferred.

## Consequences

- A first-time user can state the canonical→runtime model and pick Sync
  vs Import without hovering — the #1 documented failure — after P0.
- The glossary (§2) becomes the term set #1351's ko.json pass localizes
  against; #1351 and this ADR must agree on terms before either lands
  user-facing strings, to avoid double-churn.
- No route, schema, or gate change in P0/P1. P1's Simple-as-default flip
  changes first-run *presentation* only and stays reversible (Advanced toggle /
  one-line constant). P2 changes only display copy + status presentation, never
  `reason_code`s or request params (ADR-0015 preserved).
- P0 item 4 overlaps #1348's confirm-copy edit at `en.json:415`;
  sequencing must be coordinated (D-E) so the two do not collide.
- The tier display-term decision (D-A) is **taken** (2026-06-17): the Web
  display uses friendly labels + a "Stored in" axis (narrow ADR-0016 §7
  supersession, Web display only) without colliding with ADR-0015 or
  pre-empting #922 — request identifiers (`target_scope`) are unchanged.

## Implementation status (as of 2026-06-18)

Shipped-state companion to the phased plan in §3. #1353 remains the home
for the remaining work; this table is the source of truth for *what has
landed* so that later work does not re-implement shipped pieces or read the
provisional design language as an unstarted project. Phase rows map to
merged PRs; verify by symbol grep if the §"Source files" line numbers have
drifted.

| Phase / item | Status | Shipped in |
|---|---|---|
| **P0 — Minimal** — Overview primer, Store→Runtimes flow diagram, always-visible status legend, glossary tooltips, `(current folder)` naming, i18n/a11y hygiene (D-G) | **Shipped** | #1356 |
| P0-3 status-legend help-tip a11y — static aria-label | **Shipped** (follow-up) | #1377 |
| P0-4 confirm/glossary de-jargon — `move_copy_shared_confirm_message` rewritten (now "…into the project's shared store — committed to git…", no raw `canonical`/`project_shared`); scope-ID tooltips; nav glossary *defines* "canonical" | **Shipped** | #1356 (confirm rewrite); de-jargon companion PRs #1368 / #1375 (under issue #1352) |
| **P1a — Simple-mode scaffold** — Simple/Advanced `localStorage` toggle, read-only (default later flipped to Simple — see the D-F row) | **Shipped** | #1358 |
| **P1b — Simple-mode inline actions** — per-type Sync/Import rows, cross-tier empty-state summary (D-D lean iii), 3-state Simple labels | **Shipped** | #1360 |
| **P1 — Simple-as-default flip** (D-F) | **Shipped (reversible)** — `_CTX_SIMPLE_DEFAULT = true` (`context-gateway.js`); Simple is the default-when-unset. Shipped 2026-06-18 as a reversible experiment instead of gating on the §Validation test (6 naive participants impractical): the Advanced toggle (per-user, persisted) + reverting the constant (global) are the rollback. Onboarding-docs fan-out shipped same-PR (the `context-gateway.md` guide). The "scoping tighten" the staged plan anticipated is a no-op — the nav/control-bar deep-link trap was already `:has`-guarded in P1a and the tile grid lives only in the Overview, so it cannot strand a user. | 2026-06-18 |
| **P2 — Bold** — Push/Pull verb rename, status collapse to ahead/behind/in-sync (D-B / D-C) | **Verb rename adopted** via ADR-0030 §2 (reversible copy, no gate — see the P2 supersession note); **status collapse still deferred** (superseded by ADR-0030 §4's two-axis model) | verb rename: campaign-1 PR-E |

**§Validation status:**

- *Heuristic terminology guard.* **Partially in place.** Per-key
  jargon-free guards already cover the shipped surfaces —
  `test_ctx_p0_onboarding_keys_jargon_free`,
  `test_ctx_scope_id_tooltips_jargon_free`, and the #1352 tier/enroll
  guards in `packages/memtomem/tests/test_i18n.py` — each enumerating the
  specific user-facing keys and forbidding raw `canonical` /
  `project_shared` / `project_local` / `fan-out` in their values. A
  **blanket** scan over all `settings.ctx.*` values is deliberately **not**
  used: "canonical" is a legitimate product term on the Wiki editor surface
  (where `test_ctx_nav_sub_glossary_consistency` requires it to be
  *defined*, not removed) and also appears as the `{canonical}` path
  placeholder in empty-state hints,
  so a namespace-wide grep would false-positive. Extending the per-key
  guard as new user-facing keys are added remains open work under #1353.
- *First-run user test (5–6 naive participants).* **Not run — and no longer
  gating P1.** Recruiting 6 naive participants proved impractical, so the P1
  Simple-as-default flip (D-F) shipped 2026-06-18 as a reversible experiment
  instead (the Advanced toggle is the rollback). The test now gates only **P2**
  (the irreversible Push/Pull re-frame); the moderated + Option-A async
  protocols + the seed harness in `memtomem-docs/memtomem/testing/` remain
  ready if P2 is taken up. Use the pass bars defined below verbatim — they are
  the single source of truth; do not re-derive thresholds elsewhere.
  A facilitator **self-run gauge** (not a naive run) on build `6080fcb7`
  (2026-06-20) re-verified the seed harness rendering and confirmed the
  reversible D-F default lands a clean first-run in Simple with no
  `localStorage` step (harness + default-path only — no comprehension claim);
  it is recorded on #1353. By the self-run's own validity limit an informed
  N=1 cannot clear the /6 bars, so it yields harness validation + a friction
  gauge only — **P2 stays deferred** pending a naive run.

**Companion string issues** #1348 / #1349 / #1350 / #1351 / #1352 — all
**closed**.

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
parse-error item, "what's wrong, how fix?". **Pass bars:** probes 1–3
(≥4/6 without docs) were the original P0/P1 comprehension gate — now
**diagnostic/historical**, since P0/P1 (incl. the reversible
Simple-as-default flip) shipped without the study. The live gate is **P2
only**: probe 5 succeeding for ≥5/6 **and** the status-merge not costing
power users the create-vs-overwrite distinction.

### Reproducing the first-run state

`mm context seed-validation <dir>` (a hidden CLI helper backed by
`seed_adr0026_validation_states` in `memtomem.context._validation_seed`) seeds
the six probe affordances above into one project — out-of-sync, not-yet-imported,
empty type, MCP orphan, MCP parse-error, and an in-sync baseline — so every
participant sees the same Overview. The seeder ships in the wheel (it lives under
`src/`, not the unpackaged `tests/`), so a participant who only `pip install`-ed
memtomem can reproduce the state without a source checkout — the prerequisite for
an unmoderated async run. The command refuses a non-empty target directory
unless `--force`, so it cannot silently overwrite a real project (the Gateway
follows the server's working dir). `test_ctx_validation_harness.py` pins the seed
against the real diff engine, including the easy-to-invert Store-vs-runtime
direction (a runtime-only artifact reads as "Not yet imported", never "Out of
sync"), so it cannot silently rot, and stays in lockstep with the moderated
facilitator protocol that consumes it.

## Provisional decisions

These are the author's **recommended leans**, filled in so the ADR reads
as a concrete draft. Each is **provisional** — it records the chosen
option *and keeps its alternatives* so the maintainer can re-open any one
without re-deriving the analysis. Until the maintainer confirms, treat
each D-x as a recommendation, not a settled decision — **except D-A, which
is now decided (2026-06-17); D-B–D-G remain leans.** (Each D-x maps 1:1
to the prior open question Q-x.)

- **D-A. Tier vocabulary — axis term AND value display. — DECIDED 2026-06-17
  (ratifies what shipped in #1356 / #1372).**
  - **Decision (Web display):** (i) the axis display term is **"Stored in" /
    "Store in"** (KO "저장 위치") — the non-colliding alternative; "Scope"
    stays rejected (collides with ADR-0015's retired unqualified "scope") and
    "Tier" is kept only as the *code/concept* word, never rendered as the Web
    axis label. (ii) the tier values render as friendly labels **"User" /
    "Project (shared)" / "Project (local)"** (KO "사용자" / "프로젝트(공유)" /
    "프로젝트(로컬)"), each with a defining tooltip — a **narrow supersession
    of ADR-0016 §7 for Web display copy only**, recorded in the §7
    supersession note in ADR-0016.
  - **Scope of the supersession:** Web UI *display copy* only. CLI `--scope=`
    flags, `?target_scope=` query params, MCP, config files, runtime path
    segments, i18n KEY names, and the `{tier}` placeholder keep the literal
    tokens `user`/`project_shared`/`project_local` unchanged; the ADR-0016 §7
    inline "no runtime fan-out" annotation on `project_local` still stands.
  - **Constraint (unchanged):** this display-only decision must **not**
    pre-empt the #922 `target_scope`→`target_tier` *identifier* rename
    (TRACKER row "0016 §2", deferred to 2026-08-11).
  - **Original lean (not taken — superseded by what shipped):** keep "Tier"
    as the rendered axis term + the literal value tokens. Other kept axis
    candidates: "Storage location" / "Visibility".
- **D-B. P2 directional verbs.**
  - **Lean:** option (b) **soft** — keep the action names Sync/Import but
    add a secondary directional cue ("push to runtimes" ↑ / "pull into
    Store" ↓). Captures most of the comprehension gain without git-semantic
    over-promise or external-doc churn.
  - **Alternatives (kept):** (a) no verb change — direction carried only by
    the diagram + legend (P0/P1); (c) **full** rename Sync→Push↑ /
    Import→Pull↓ (highest comprehension, one-way, breaks external
    docs/screenshots) — **chosen and adopted 2026-07-19 by ADR-0030 §2** as
    reversible copy (locale/help/guide relabel; wire vocabulary frozen), which
    reframed it as reversible and therefore not gated on the §Validation test.
- **D-C. Status-merge — mixed multi-runtime states.**
  - **Lean:** defer any status collapse to **P2 only** (post-validation);
    when rendering a **mixed** item (in-sync for one runtime, out-of-sync
    for another — `context-gateway.js:1511`) use **worst-status-wins for
    the row badge + per-runtime chips** for detail, and keep the
    create-vs-overwrite cue on list/Sync-All rows *before* the confirm
    modal (do not hide it in the modal).
  - *Observed 2026-06-20 (author self-run gauge, build `6080fcb7`; string
    rendering only):* today's four-status model already surfaces this cue
    explicitly in the Sync confirm modal — `settings.ctx.confirm_sync_impact`
    ("create {create} missing and overwrite {overwrite} out-of-sync runtime
    files…") plus `settings.ctx.confirm_sync_overwrite_warning`. A P2
    status-collapse to ahead/behind/in-sync must therefore preserve an
    equivalent create-vs-overwrite signal, not regress it — this constraint is
    the concrete form of the second P2 gate condition (the gate itself stays
    unmet, pending the naive run).
  - **Alternatives (kept):** collapse to a single ahead/behind/in-sync
    badge with no per-runtime chips (simpler, but loses the mixed-state and
    overwrite-risk signal at a glance); keep today's four-status model
    unchanged (no collapse at all).
- **D-D. Simple-mode empty-tier handling.**
  - **Lean:** option (iii) a **read-only empty-state summary** that names
    which other tier holds items ("3 items in your User tier — open
    Advanced to manage") *without* changing the active tier — preserves the
    stable `project_shared` default (ADR-0015/0016) while staying
    discoverable.
  - **Alternatives (kept):** (i) a plain "turn on Advanced" hint only;
    (ii) **auto-switch** the active tier to the populated one (rejected in
    the lean — conflicts with the stable default, but recorded for
    re-evaluation).
- **D-E. Sequencing + a single glossary owner.**
  - **Lean:** designate **this ADR's §"A single user-facing display
    glossary" as the source-of-truth**; land **P0 before** #1351's bulk
    ko.json pass so translation localizes against settled terms; coordinate
    P0-4's `en.json:415` rewrite with #1348 (same string) so they don't
    double-churn.
  - **Alternatives (kept):** let #1351 land first and have this ADR conform
    to whatever terms emerge; or run them fully in parallel with a
    post-hoc reconciliation pass (higher churn risk).
- **D-F. Default-flip blast radius. DECIDED 2026-06-18 — flip now, reversibly.**
  - **Decision:** shipped Simple-as-default immediately (`_CTX_SIMPLE_DEFAULT =
    true`) with the onboarding-docs fan-out in the same change, keeping the
    Advanced toggle visible (not buried) as the rollback signal. The
    §Validation signal the original lean waited for was skipped as impractical
    (6 naive participants out of reach); the bet is acceptable because the
    change is reversible — per-user via the toggle, globally via the one-line
    constant — and is watched by real-world feedback rather than a pre-ship study.
  - **Original lean (not taken):** staged opt-in first — ship with Advanced as
    the default, gather the §Validation signal, then flip. Rejected on the
    recruiting cost alone; revisit if real-world feedback shows the flip hurt
    naive first-runs.
- **D-G. Accessibility & localization of the new visual onboarding.**
  - **Lean:** make a11y a **P0 gate** (not deferred): the Store→Runtimes
    diagram must carry an equivalent text alternative (it is never the only
    carrier of the model — the primer prose is), the legend/tooltips must be
    keyboard-reachable and focus-visible, status must not be color-only,
    and the layout must render in dark mode and survive RTL/localized
    widths. Add these as acceptance criteria to P0 item 6 (i18n/gate
    hygiene).
  - **Alternatives (kept):** treat a11y polish as a fast-follow after P0
    ships (rejected in the lean — the diagram/legend are comprehension-
    critical, so their a11y is load-bearing, not polish).

If this ADR is accepted with P2 left deferred, add a TRACKER.md row for
D-B/D-C (trigger: the §Validation first-run user test) pointing at #1353.

## References

**Issues**

- #1353 — tracking issue (first-time-user onboarding & IA); this ADR's home.
- #1348 / #1349 / #1350 / #1351 / #1352 — string-level companions (raw
  move/copy verbs; portal empty-state; `reason_code`→i18n leaks; ko.json
  gap + install-guide literals; wording polish).
- #922 — deferred `target_scope`→`target_tier` identifier rename
  (ADR-0016); D-A must not pre-empt it.

**ADRs**

- ADR-0009 — Context Gateway dashboard info surface (the surface this
  layer annotates).
- ADR-0011 §3 — `project_local` has no runtime fan-out (load-bearing for
  the diagram + the "Draft, never pushed" definition).
- ADR-0015 — request vocabulary `project_scope_id` / `target_scope` and
  the retirement of unqualified "scope" (load-bearing for §2 / D-A);
  this ADR is the display-layer counterpart and does not change it.
- ADR-0016 §7 ("CLI / Web UI user-facing names") — originally **pinned**
  the literal tier tokens for user-facing surfaces and rejected display
  aliases; **D-A (decided 2026-06-17) narrowly supersedes it for Context
  Gateway Web display copy only** (CLI / params / config / identifiers keep
  the literal tokens — see the §7 supersession note). §"Open questions"
  raises the
  deferred `target_scope`→`target_tier` *identifier* rename (bound to #922
  with a 2026-08-11 review window in TRACKER.md, row "0016 §2"); D-A must
  not pre-empt it.
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
  the `(runtime, name, status)` aggregation comment (load-bearing for D-C
  mixed multi-runtime states).
- `packages/memtomem/src/memtomem/web/static/locales/en.json:415`
  `move_copy_shared_confirm_message` (P0-4); `:497-500` status keys
  (P0-3); `:591` "Stored source".
