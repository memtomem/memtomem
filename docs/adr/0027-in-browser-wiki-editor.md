# ADR-0027: In-browser wiki canonical/override editor

**Status:** Accepted (Editor-A #1367, Editor-B #1371, and the §3 commit
affordance are shipped per the recommended leans D-A/D-B/D-C/D-G; two
event-driven questions remain — D-A save→commit dogfooding and D-E privacy
posture — tracked in `docs/adr/TRACKER.md`. Each lean keeps its alternatives
and is reversible)
**Date:** 2026-06-14 (Accepted 2026-06-15)
**Context:** ADR-0008 (wiki layer) shipped its Web UI "read-only-first":
**E-1** a read-only wiki browser (`GET /api/wiki` + per-vendor
`diff`/`lint`, prod tier) and **E-2** a dev-tier override *seed*
(`POST /api/wiki/{type}/{name}/override`, renders the canonical baseline
into `overrides/<vendor>.<ext>`). ADR-0008's PR Breakdown row E
explicitly defers the write-capable editor to its **"own ADR —
save→wiki-write→commit semantics"** (`docs/adr/0008-wiki-layer.md:286`,
quoted verbatim). This is that ADR. It designs an in-browser editor that
lets a user view and edit a wiki artifact's bytes — the per-vendor
**override** and the base **canonical** — without dropping to a terminal,
and resolves the save/commit/privacy questions E-2 deliberately
sidestepped by taking no user input.

The single fact that shapes every decision below: **`~/.memtomem-wiki/`
is a normal git repo** (ADR-0008 Consequences). Its version history *is*
git history — which is exactly why the deferred question names *commit*.
The per-artifact `versions/` snapshot store of **ADR-0022** is a
*project-canonical* mechanism keyed on the tier axis
(`user`/`project_shared`/`project_local`); the host-global wiki has no
tier, so ADR-0022 versioning does **not** apply to the wiki and is out of
scope here (see §Scope).

## Scope and non-goals

This ADR designs a **wiki-only**, **dev-tier** authoring surface. It is
deliberately narrow:

- It edits the **host-global wiki** (`~/.memtomem-wiki/`) only — never
  project canonical under `<project>/.memtomem/`. Consequently it has
  **no tiers** (ADR-0011/ADR-0016 `user`/`project_shared`/`project_local`
  do not exist for the wiki), **no project-scope bar**, **no
  `target_scope`**, and **none of the per-project host-write confirm
  machinery** (`resolve_scope_root` / `host_write_gate`). It inherits
  E-1/E-2's host-global posture verbatim
  (`wiki.py:3-10`, `wiki_mutations.py:13-18`).
- It does **not** introduce an **ADR-0022 version store** for the wiki.
  The wiki's history primitive is git; ADR-0022's `versions/vN.md` +
  `versions.json` is keyed on `(scope, type, name)` where `scope` is a
  tier (`docs/adr/0022-...:86-119`), and the wiki has no tier. "Freeze a
  version" / "promote to production" for the *wiki* is `git commit` /
  `git tag` / branches, not the ADR-0022 surface. (ADR-0022 Decision (c)
  *rejected* modeling versions as git commits/tags **for the
  project-canonical store** — it wanted file-native inspectability under
  `.memtomem/`. That is not in tension here: git is the host-global
  wiki's *native* history primitive per ADR-0008, so this ADR neither
  re-opens nor supersedes that decision.)
- It does **not** change fan-out, override **resolution** (ADR-0008
  Invariant 4 full-file replacement, no section merge — that v1 line
  stands), `mm context install`/`update`, the lockfile schema, or any
  read endpoint (E-1).
- It does **not** edit a skill's asset subtree (`scripts/`,
  `references/`). A skill's editable canonical is its `SKILL.md` text
  only; binary/asset management stays a git/CLI concern.
- **Threat-model delta vs E-2 (load-bearing for §8).** The access
  controls are the *same* as E-2 (dev-tier mount + CSRF/Origin/Host), but
  the consequence is strictly larger: E-2 can only write bytes *derived
  from canonical already in the repo*, whereas this editor writes
  **attacker-controllable bytes** that, once committed, propagate to every
  project pinned to the artifact and out through any push remote. The
  privacy question (§8 / D-E) therefore *cannot* be answered by E-2
  parity alone.
- **Non-normative on E-2's "never auto-commit" contract — preserved, not
  superseded.** ADR-0008 row E states the override seed "leaves it dirty
  for the user to commit (never auto-commits)". This ADR **keeps** that:
  *Save* writes the working tree and leaves it dirty; *Commit* is a
  separate, explicit, opt-in act (Decision 3 / D-A). Auto-commit-on-save
  is rejected. No prior decision is superseded.

## Background

### The gap the editor closes

After E-2 seeds `overrides/<vendor>.<ext>`, the seeded bytes are a
*starting point* the user is expected to refine. Today the UI tells them
to leave: the wiki section description reads "Read-only in prod; dev mode
can seed vendor overrides. **Edit canonical files with the `mm wiki`
CLI.**" (`en.json:431`). To tweak the seeded override, fix a typo in a
canonical `agent.md`, or correct a lint error, the user must drop to a
terminal (`$EDITOR` / hand-edit) and then `git add … && git commit`. The
CLI `mm wiki <type> override --editor` only opens `$EDITOR` on the file
and prints a manual commit hint (`cli/wiki_cmd.py`, `_run_seed_override`).
When this ADR was first drafted, no isolated commit verb existed in the CLI;
per-type `mm wiki <type> commit` verbs have since shipped (§3), while a flat
top-level `mm wiki commit` remains unbuilt. The in-browser editor closes this
loop for the dev-tier user.

### The working-tree vs committed-objects asymmetry (load-bearing)

This is the fact the "→commit" half of the deferred question exists for:

- **Reads of the live surface** — `diff`, `lint`, the web browser — read
  the **working tree, including uncommitted edits**
  (`wiki/inspect.py:113-114,145`; `is_dirty()` = `git status
  --porcelain`, `wiki/store.py:215-230`).
- **The install/pin direction** — `mm context install` / `update` — reads
  **only committed git objects** via `git show <commit>:<path>`; "the
  wiki working tree is never touched" (`wiki/store.py:259,293-308`).

So an edit that is **saved but not committed** is visible in the editor
and in `diff`/`lint`, but **does not propagate to any project** until it
is committed. The CLI already surfaces this (`_WIKI_DIRTY_WARN`: "warning:
wiki has uncommitted changes; using HEAD which doesn't include them",
`context_cmd.py:1830`). The editor must make the same gap legible — and a
Commit affordance is what lets the user close it without a terminal.

### What already exists to build on

- **Read tier (prod, `web/routes/wiki.py`):** `GET /api/wiki` (list +
  per-vendor `renderable` flag + `wiki_head` + `is_dirty`), `GET
  /api/wiki/{type}/{name}/{diff,lint}`. **There is no canonical-content
  GET and no write endpoint** — the editor needs new routes.
- **Write tier (dev only, `web/routes/wiki_mutations.py`):** the single
  existing wiki write verb, `POST /api/wiki/{type}/{name}/override`
  (seed), mounts only when `MEMTOMEM_WEB__MODE=dev`
  (`_DEV_ONLY_ROUTERS` at `app.py:115-124`; dev mount gate
  `app.py:246-248`). It runs the write under a bare `asyncio.to_thread`
  with **no** `_gateway_lock` and **no** `asyncio.timeout`
  (`wiki_mutations.py:69-76`) — i.e. the lock/timeout/503 machinery the
  editor needs does **not** yet exist on the wiki routes (Decision 5).
- **The per-project canonical editor (`context_skills.py` +
  `context-gateway.js`)** is a complete, tested template for the
  read-only→edit→save→conflict flow the wiki editor should mirror: GET
  returns `{content, mtime_ns}` (`context_skills.py:263-307`,
  `mtime_ns` a string at `:307`); PUT takes `{content, mtime_ns, force}`
  (`SkillUpdateRequest` at `:384`); a stale `mtime_ns` returns **409**
  `reason_code:"stale_mtime"` + fresh `mtime_ns`
  (`_mtime_conflict_response` at `:400-411`); the server holds
  `_gateway_lock` and re-checks mtime inside the lock; force-save emits a
  WARNING audit (`context_skills.py:438-472`); the client stashes the
  buffer to `sessionStorage` (`_ctxStashDraft`,
  `context-gateway.js:3943-4082`, key `m2m-ctx-conflict-buffer:`) and
  shows a Reload / Force / Open-diff modal + inline conflict banner.
- **Shared infrastructure the save reuses unchanged:** `atomic_write_bytes`
  (`context/_atomic.py:153`), `.bak`-on-overwrite + preconditions-before-
  mutation (`wiki/override.py:137-160`), the `_error(status, kind, msg,
  reason_code=…)` envelope (`_errors.py:102`) with **fixed messages / no
  `str(exc)` path leak** — note `_error` itself does **not** redact;
  `_redact_message` (`_errors.py:76`) is a separate helper that must be
  wired in deliberately (Decision 3) — and CSRF/Origin/Host enforcement
  on every unsafe `/api/*` method (`middleware/csrf.py`).

## Decision

### 1. What is edited — two artifacts, delivered in two phases

The editor operates on two artifact bytes, with very different blast
radii, so they ship in order (mirroring ADR-0008's read-only-first and
ADR-0026's phased delivery):

| Phase | Edits | Path | Blast radius | Versioned? |
|---|---|---|---|---|
| **Editor-A** (first) | a vendor **override** | `<type>/<name>/overrides/<vendor>.<ext>` | one vendor; full-file replacement (Inv 4); does not change what the base fans out | No (ADR-0022 inv 9 — overrides never versioned) |
| **Editor-B** (next) | the base **canonical** | `skills/<n>/SKILL.md`, `agents/<n>/agent.md`, `commands/<n>/command.md` | re-derives **every** vendor's `diff`/`lint` baseline (shared `render_seed_bytes`) and, on commit, every project pinned to that artifact | git history only |

Editor-A is the natural first step: E-2 already *creates* the override
file; Editor-A lets the user *refine* it in place. It is contained,
unversioned, and full-file (no parse gate needed beyond representability).
Editor-B is larger — a canonical that fails to parse breaks
`render_seed_bytes` and therefore fan-out — so it carries a parse gate
(Decision 6) and lands second.

**Each phase adds its own read pane.** The wiki detail today renders only
per-vendor `diff`/`lint`, with no content read pane
(`_renderWikiDetail`, `wiki.js:454`). So Editor-A adds an **override**
read pane (a `<pre>` seeded from `GET …/override?vendor=`) before its Edit
toggle; Editor-B adds a **canonical** read pane (seeded from
`GET …/canonical`). The "add a read pane first" step is per-artifact, not
a one-time Editor-B-only addition (Decision 7).

### 2. Save semantics — write-only + dirty badge, never auto-commit

A **Save** does exactly what E-2's seed does plus accept user bytes, in
this fixed precondition-before-mutation order so a refused save leaves the
prior bytes intact:

1. validate name / vendor / asset-type **before any disk touch**
   (`_wiki_common.py` validators; `AssetType` Literal → 422 on a hostile
   value before any path join);
2. **(Editor-B only)** parse the new canonical (Decision 6) — a parse
   failure returns 400 and writes nothing;
3. re-check `mtime_ns` **inside** the lock (Decision 5); a stale value →
   409 and writes nothing;
4. on overwrite, write a `.bak` sibling **first**, then write via
   `atomic_write_bytes` (temp + `os.replace`, `0o600`) — parity with
   `override.py:157-160`;
5. read `store.is_dirty()` **back after** the write (never assume) and
   return `wiki_dirty` so the UI repaints the HEAD/dirty badge **without
   re-rendering the list** (a11y: avoid focus loss — `wiki.js:283`);
6. **never** run `git add`/`commit`.

This preserves the shipped E-2 + ADR-0008:286 "never auto-commit"
contract bit-for-bit. The dirty→not-yet-fanned-out gap (§Background) is
surfaced by the dirty badge and a one-line hint reusing the
`_WIKI_DIRTY_WARN` wording.

### 3. Commit semantics — explicit, opt-in, dev-tier (the deferred core question)

`git commit` is a **separate affordance**, never folded into Save:

- A **Commit** action builds an **isolated commit** containing *only* the
  server-resolved target paths, independent of whatever else may already be
  staged in the wiki repo's index. It does **not** run a bare `git add … &&
  git commit` (which commits the whole index, sweeping in any pre-existing
  staged changes). Instead it commits through a **temporary index kept
  outside the worktree** (a private `GIT_INDEX_FILE`, removed afterward):
  `read-tree HEAD` → add *only* the resolved targets — staging each target's **saved blob**
  (the digest-verified bytes the editor wrote, e.g. `hash-object` +
  `update-index --cacheinfo`) rather than a fresh working-tree read, so an
  external same-path edit slipping in after the token check cannot be swept
  in; this also stages a new untracked override file without touching the
  real index → `write-tree` → `commit-tree` on HEAD → advance the wiki
  branch ref by **atomic compare-and-swap** (`git update-ref
  refs/heads/<branch> <new> <expected_head>`, ff-only) so a commit landing
  between the pre-check and the ref move makes the CAS fail → **409**, never
  a clobber (the sidecar lock cannot bind an external `$EDITOR`+git, so the
  CAS — not the lock — is the binding cross-process guard). Because that
  bypasses the real index, the real index still holds the *old* blob for the
  committed target, so a final **reconciliation** refreshes the real index
  for **only** the resolved targets from the new HEAD
  (`git reset -q HEAD -- <resolved-targets>`, leaving unrelated staged
  entries intact). Only then does it read `store.is_dirty()` **back** and
  repaint `wiki_head` + the dirty badge from that value (like Save) — so the
  badge stays dirty **only** for genuinely unrelated working-tree changes,
  never for the editor's just-committed file; a blanket "clear" would lie
  about repo state. (Acceptable fallback to the temp-index flow: refuse the
  commit if the real index already holds staged paths outside the target
  set, and re-verify the staged set immediately before committing.) This is
  the in-browser equivalent of the manual `git add … && git commit` hint the
  CLI prints today (`cli/wiki_cmd.py:86-89`).
- **Paths are server-resolved, never client-supplied (trust boundary):**
  the request carries **typed targets** — `{kind: canonical|override,
  vendor?}` + a per-target `mtime_ns`/digest — and the server resolves each
  to a validated wiki-relative path (`<type>/<name>/<asset>.md` or
  `overrides/<vendor>.<ext>`), reusing the same `AssetType` /
  `validate_name` / `override_vendors` validators as the PUT. A raw client
  path is **never** staged (it could traverse or capture unrelated files),
  and the isolated commit never includes anything beyond the resolved
  targets — so the commit contains exactly the editor's targets and nothing
  else (this, together with the temp-index isolation above, closes the
  bare-`git commit`-sweeps-the-whole-index hole).
- **Concurrency (see also Decision 5):** the commit runs under
  `_gateway_lock` **and** a new wiki-repo cross-process lock (the wiki
  routes have neither today), and carries **two** preconditions, both
  re-checked **inside the lock before staging into the temp index**: (a) an
  **expected-HEAD** SHA — the `wiki_head` the client last saw
  (`current_commit()`, `store.py:165`) — checked before staging *and*
  enforced as the **atomic compare-and-swap on the final ref update** (the
  binding guard, since neither the pre-check nor the sidecar lock can bind
  an external git), so a commit that landed underneath returns **409**
  instead of clobbering a moved HEAD; and
  (b) a **per-path token** — the `mtime_ns` (or content digest) each saved
  file had when the editor wrote it — re-`stat`ed before staging, so an
  *external same-path working-tree edit between Save and Commit* (which the
  HEAD check cannot see) also returns **409** instead of letting `git add`
  sweep in bytes the editor never saved.
- **Message:** user-supplied, with a generated default offered (parity
  with the only existing wiki commit, `WikiStore.init()`'s
  `_INITIAL_COMMIT_MESSAGE`, `store.py:24,148-149`).
- **Author/identity:** the wiki repo's own git config; memtomem does not
  inject an identity (none is configured anywhere except `init`).
- **Error hygiene (path-leak discipline):** `WikiStore._git` raises
  `RuntimeError(f"git … failed: {detail}")` where `detail` is **raw git
  stderr** (`store.py:100`) — which routinely embeds the absolute repo
  path (`$HOME/.memtomem-wiki`). `_error` does **not** redact
  (`_errors.py:102`). So the commit-failure path MUST return a **fixed**
  message (e.g. "git commit failed; check the wiki repo git config and
  state") with a `reason_code`, log `str(exc)` server-side only (or route
  it through `_redact_message`), and **never** pass raw git stderr into
  the envelope — the same rule `_wiki_absent` enforces for the wiki path.
- **Why separate, not auto:** auto-commit-on-save would (a) reverse the
  shipped E-2 contract, (b) produce noisy per-save history in a repo whose
  whole point is human-curated history, and (c) force an authorship policy
  memtomem currently has none of. Keeping Save and Commit as two acts also
  echoes ADR-0022's deliberate edit/deploy separation — here applied to
  git rather than to a label pointer.

### 4. Routes + tier

New endpoints, reusing the E-1/E-2 read/write split:

```
# read (see D-F for prod-vs-dev tier of the read side)
GET  /api/wiki/{type}/{name}/canonical              -> {content, mtime_ns, ...}
GET  /api/wiki/{type}/{name}/override?vendor=<v>     -> {content, mtime_ns, exists, ...}

# write — dev tier only (_DEV_ONLY_ROUTERS, mode==dev)
PUT  /api/wiki/{type}/{name}/canonical    {content, mtime_ns, force}            # Editor-B
PUT  /api/wiki/{type}/{name}/override      {vendor, content, mtime_ns, force}    # Editor-A
POST /api/wiki/{type}/{name}/commit  {message?, expected_head, targets:[{kind,vendor?,mtime_ns}]}  # Decision 3
```

- `PUT …/override` is the **edit** verb (replace existing bytes with user
  content); E-2's existing `POST …/override` stays the **seed** verb
  (render from canonical, no `content`) — the two are not merged, so E-2's
  contract is untouched (FastAPI dispatches on method, so there is no
  path collision).
- All write verbs mount **dev-only** (`_DEV_ONLY_ROUTERS`,
  `app.py:115-124`; dev gate `app.py:246-248`), matching E-2; CSRF/Origin/
  Host middleware auto-guards every unsafe `/api/*` method, so the SPA
  must thread `X-Memtomem-CSRF` (no parallel auth path). The access
  controls are **identical to E-2**, but per the §Scope threat-model
  delta the blast radius is strictly larger (user-controlled bytes →
  pinned projects + push remotes) — which is why §8/D-E exists.
- Each new handler is registered in
  `tests/test_web_invariants_registry.py` (`_CSRF_PROTECTED` + a redaction
  class — see D-E for why it is `_REDACTION_EXEMPT`, not
  `_REDACTION_PROTECTED`).
- **No** project-scope / `target_scope` / host-write confirm (host-global).

### 5. Concurrency — optimistic `mtime_ns`, adopt (not inherit) the ctx pattern

The wiki write routes currently have **no** in-process lock, timeout, or
503 path (E-2 seed runs a bare `asyncio.to_thread`,
`wiki_mutations.py:69-76`). This ADR therefore *adopts* the per-project
editor's optimistic-concurrency pattern as a **new** addition to the wiki
routes (importing `_gateway_lock` from `_locks.py`), so the existing
client 409 modal works unchanged:

- GET returns `mtime_ns` as a **string** (JS bigint-unsafe); PUT echoes
  it back with optional `force`.
- A stale `mtime_ns` → **409** `reason_code:"stale_mtime"` carrying the
  fresh `mtime_ns`; `force:true` overwrites with a WARNING audit logging
  both values (parity with `context_skills.py:438-472`).
- Server holds the lock and re-checks mtime **inside** the lock after the
  unlocked pre-check; the blocking write runs via `asyncio.to_thread`
  under `asyncio.timeout(N)`; a TimeoutError → typed **503 `busy`**.
- **The git step is locked too.** Because `_gateway_lock` is in-process
  only (`_locks.py`) and the wiki repo has no cross-process lock today, a
  concurrent CLI `mm wiki` / external `$EDITOR`+git / second browser tab
  can race a `git add`/`commit`. So `POST …/commit` (Decision 3) acquires
  `_gateway_lock` **and** a new wiki-root cross-process lock (a
  `portalocker` sidecar, the layer-2 pattern `context/_atomic.py` already
  provides for skills/settings) with a budget **strictly below** the
  handler `asyncio.timeout`, plus an **atomic compare-and-swap on the ref
  update** (`git update-ref … <expected_head>`) — the cross-process guard
  the lock cannot provide against an external git — so a racing commit
  returns 409 instead of committing on an advanced HEAD.
- The wiki HEAD/`is_dirty` state is shown as **context** (badge); the
  per-file conflict **token** is `mtime_ns`, and the commit carries
  **both** the expected HEAD SHA *and*, per server-resolved target, the
  `mtime_ns`/digest (re-checked under the lock before staging) so neither
  a racing commit nor a racing same-file edit can corrupt the commit; the
  dirty badge is repainted from `is_dirty()` read back after commit, not
  assumed clear (D-D).

### 6. Validation before write

- **Editor-B (canonical, agents/commands):** parse with
  `parse_canonical_agent` / `parse_canonical_command` **using
  `layout="dir"`** (their default is `"flat"`,
  `context/commands.py:174`; the wiki uses dir layout —
  `agents/<name>/agent.md` — exactly as the seed path calls them,
  `override.py:102,116`) **before** the write; a parse failure returns
  **400 `validation`** (mirrors E-2's unrenderable→400) and writes nothing
  — a canonical that doesn't parse would break `render_seed_bytes`/fan-out
  for every vendor. After a successful canonical save, the editor repaints
  `diff`/`lint` for the affected vendors (they all re-derive from the
  changed canonical).
- **Editor-A (override):** **no parse** — Invariant 4 is "give me exactly
  this output, do not transform". A representability/UTF-8 lint may run as
  a **non-blocking** warning. The non-renderable `("commands","codex")`
  slot stays disabled (no generator → 400), exactly like the seed button.
- Skills canonical is plain markdown (no structured parse); validation is
  name + UTF-8.

### 7. UI — reuse the ctx editor, host-global simplifications

```
┌─ Context Gateway › Wiki (dev mode) ─────────────────────────────┐
│ HEAD a1b2c3d  ● uncommitted changes        [ Commit… ]          │ ← wiki-head badge + Commit (Dec 3)
│ agents / code-review                                            │
│  Canonical  [ Edit ]            Override: [ codex ▾ ] [ Edit ]  │ ← Editor-B / Editor-A toggles
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ # code-review                                             │ │ ← per-artifact read pane (<pre>), new
│  │ …canonical (or override) source…                          │ │
│  └───────────────────────────────────────────────────────────┘ │
│  (Edit →) ┌───────────────────────────────────────────────┐    │
│           │ <textarea>                                     │    │ ← reuse ctx-edit-area
│           └───────────────────────────────────────────────┘    │
│           [ Cancel ]                            [ Save ]        │ ← Save = write-only (Dec 2)
└──────────────────────────────────────────────────────────────────┘
```

- **Mount** in the existing `#wiki-detail` pane (`index.html:687`),
  extending `_renderWikiDetail` (`wiki.js:454`). Each phase first adds its
  per-artifact read pane (Decision 1): a `<pre>` like ctx's
  `ctx-content-pre`, seeded from the new GET, then an Edit toggle.
- **Reuse** the ctx editor's read-only→Edit-toggle→textarea→Save/Cancel
  pattern (`context-gateway.js`, the edit handlers around the
  `ctx-edit-area`/conflict flow), its CSS, and its i18n keys
  (`settings.ctx.edit/save/cancel`, the `conflict_*` family,
  `canonical_source`, `diff_view`) — they are already at en/ko parity.
- **a11y + i18n + cache-bust contracts (P-gate, not deferred):** inline
  `t()` on injected nodes (never `data-i18n` — `applyDOM` clobbers
  re-rendered content); repaint cached `_wikiData` on `langchange`;
  conflict banner `role="alert"` on the **short heading only**, never the
  scrolling diff body; toast role per-element, never a live-region
  container; save-success repaints **only** the targeted nodes (no list
  re-render → no focus loss, as `wiki.js:283` already does for the dirty
  badge); participate in the `_wikiListSeq`/`_wikiDetailSeq`
  overlapping-fetch guard; bump `wiki.js?v=` (and any other touched `?v=`)
  in `index.html`.
- The `sessionStorage` draft-stash key may **drop the project component**
  (the wiki is host-global) — key on `(type, name, [vendor])`.

### 8. Privacy posture — soft, non-blocking warning (a valve, not a gate)

E-2 is `_REDACTION_EXEMPT` because it "renders existing wiki canonical
into overrides/; Gate A guards wiki→project install, not wiki→wiki
seeding" (`test_web_invariants_registry.py:235`). The editor **takes user
bytes**, so that justification does not carry over and the question must
be answered:

- ADR-0011 **Gate A** is keyed on `scope == project_shared` ("the bytes
  enter git history forever", `docs/adr/0011-...:214-229`). The
  host-global wiki **has no `project_shared` tier**, so Gate A does not
  literally apply.
- ADR-0011 also states **authoring-side privacy is out of scope** —
  "memtomem cannot gate" a user editing a shared markdown directly
  (`docs/adr/0011-...:286-296`); the CLI `$EDITOR` path is ungated, so
  parity argues against a hard web gate.
- But the wiki **can have a git remote** (ADR-0008: backup/sharing via git
  remotes), so user-authored secrets *can* leave the host.

**Lean:** surface a **soft, non-blocking** secret-shape warning on save,
using the scope-less primitive **`privacy.scan(content) -> list[Redaction
Hit]`** (`privacy.py:283`) — **not** `scan_text_content`. This matters:
`scan_text_content` (`context/privacy_scan.py:244`) requires a `scope:
TargetScope` (`config.py:751`) the wiki does not have and is built to feed
`enforce_write_guard`'s **refusal** path (`force_unsafe` hardcoded False);
it is the sync-side *gate*, the opposite of the intended valve.
Correspondingly, the handler is classified **`_REDACTION_EXEMPT` with a
justification**, **not** `_REDACTION_PROTECTED`. The registry's
`_REDACTION_PROTECTED` semantically means "scan **and refuse** on a hit"
(every current member raises on `decision == "blocked"` —
`system.py:1537`, `settings_sync.py:918`), and its AST test only checks
that the scanner is *called by name*
(`test_web_invariants_registry.py:391`). So classifying a deliberately
non-refusing handler `_REDACTION_PROTECTED` would be a **coverage-gate
false-pass** — CI green, protection absent. `_REDACTION_EXEMPT` with the
justification "host-global wiki, no `project_shared` tier; authoring-side
write at parity with the ungated CLI `$EDITOR` path; an advisory
`privacy.scan()` warning is surfaced but the write is never refused (a
valve, not a gate)" states the posture honestly. Kept alternatives in
D-E (including a hard gate if the wiki gains a configured push remote).

## Consequences

- The dev-tier wiki becomes self-service: refine an override or fix a
  canonical typo and commit, all in-browser. The `en.json:431` /
  `ko.json:431` wiki description must change ("edit canonical files with
  the `mm wiki` CLI" → in-UI editing available in dev mode).
- **No** change to fan-out, override resolution (Inv 4), install/update,
  the lockfile, or E-1's existing prod read endpoints — the new
  canonical/override content GETs (Decision 4) are dev-tier editor reads.
  E-1 (read-only browser) and E-2 (seed) behavior is unchanged.
- The wiki's history stays **git-native**; the ADR-0022 version store
  remains a project-canonical mechanism (no wiki version chips).
- New `WikiStore` working-tree **read** + **write** + **commit**
  primitives are needed (it currently exposes only at-commit reads,
  `read_asset_file_at_commit`, and `current_commit()`/`is_dirty()`) — a
  small, well-bounded addition (D-G), plus a wiki-root cross-process lock
  (none exists today).
- ADR-0008 cross-links updated in the **same PR**: name ADR-0027 in the
  row-E editor-deferral clause and **add** a new ADR-0027 bullet to
  ADR-0008's References (which today lists only ADR-0001 and ADR-0007,
  `0008-wiki-layer.md:325-338` — so this is an *addition*, not an edit of
  an existing entry). ADR-0008's Status line + row-E *E-3* clause are
  separately stale (E-3 shipped in #1357 but the ADR still lists it
  deferred) — left untouched here to keep this PR scoped to the editor.
- If the editor later graduates to prod, that promotion is governed by
  **ADR-0001 §5** (router move `_DEV_ONLY_ROUTERS`→`_PROD_ROUTERS` +
  removal of the `STATE.uiMode==='dev'` gate, gated on: ≥2 weeks no P0/P1,
  a round-trip integration test, en+ko i18n parity, and a conflict-path
  fixture). No env kill-switch; rollback is git-revert.

## Validation

- **Triple test gate** (the web/static convention): vitest (`tests-js`) +
  Playwright (`tests/web`) + `test_i18n.py` source-grep parity for every
  new key in both locales.
- **Conflict-path fixture** is mandatory (it is also one of the four
  ADR-0001 §5 prod-promotion criteria): a 409 `stale_mtime` round-trip for
  the PUT, the force-save audit, **and** a `POST …/commit` with a stale
  `expected_head` returning 409.
- **Editor-B parse gate** is fixture-pinned: a deliberately unparseable
  `agent.md` PUT must 400 and **write nothing** (assert the file is
  byte-unchanged on disk, not merely that the response is 400).
- **Privacy soft-warn:** a fixture seeding a secret-shape token asserts
  the save **succeeds** and the warning is present — i.e. it is a valve,
  not a gate (guards against a regression to hard-refusal of clean prose).
- **Commit error hygiene:** a fixture forcing a git failure asserts the
  response is a fixed message with no absolute path (no `$HOME` /
  `.memtomem-wiki` leak).

## Provisional decisions

These are the author's **recommended leans**, filled in so the ADR reads
as a concrete draft. Each records the chosen option **and keeps its
alternatives** so the maintainer can re-open any one without re-deriving
the analysis. Until confirmed, treat every D-x as a recommendation, not a
settled decision.

- **D-A. Save → commit model (the deferred core question).**
  - **Lean:** *Save* = write-only + dirty badge (preserve E-2's
    never-auto-commit contract); a **separate, explicit Commit** action
    (Decision 3). Two acts, never one.
  - **Alternatives (kept):** (i) Save also `git add`-stages the file
    (no commit) — rejected as low-value (`is_dirty()` counts staged and
    untracked identically, `store.py:215-230`); (ii) **auto-commit on
    every Save** — rejected (reverses E-2, noisy history, authorship
    policy); (iii) no in-browser commit at all, keep the CLI hint —
    rejected (leaves the "→commit" half of the deferred question unsolved).

- **D-B. Editor scope / phasing.**
  - **Lean:** **override-editing first** (Editor-A), **canonical-editing
    next** (Editor-B) — smallest blast radius first, matching ADR-0008
    read-only-first. Each phase adds its own per-artifact read pane
    (Decision 1).
  - **Alternatives (kept):** ship both together (larger first PR);
    canonical-first (rejected — larger blast radius and the parse gate).

- **D-C. Commit affordance details.**
  - **Lean:** message user-supplied with a generated default; author =
    the wiki repo's git config; commit errors return a **fixed** message +
    `reason_code` (raw git stderr logged server-side only — never in the
    envelope, Decision 3); Commit is dev-tier only and HEAD-precondition
    guarded.
  - **Alternatives (kept):** require a non-empty user message (no
    default); a dedicated `mm wiki commit` CLI verb for parity (since shipped
    as per-type `mm wiki <type> commit` in §3; a flat top-level verb remains
    unbuilt) — was recommended as a sibling follow-up, not blocking;
    forbid commit in web entirely and only mark dirty (rejected — defeats
    the loop-closing purpose).

- **D-D. Concurrency token.**
  - **Lean:** `mtime_ns` for the per-file PUT (verbatim parity with the
    ctx editor so the existing 409 client flow is reused); `POST …/commit`
    carries **both** the expected wiki HEAD SHA **and**, per *typed,
    server-resolved* target (never a raw client path), the `mtime_ns`/
    digest — both re-checked under the lock before staging (HEAD alone
    catches a racing commit but not a racing same-path working-tree edit
    between Save and Commit).
  - **Alternatives (kept):** use the wiki **HEAD commit** as the token for
    *both* PUT and commit (git-native, but diverges from the ctx client
    and does not catch working-tree edits between commits); a HEAD-only
    commit precondition with no per-path recheck (rejected — leaves the
    same-file Save→Commit race open).

- **D-E. Privacy posture for user-authored bytes.**
  - **Lean:** **soft, non-blocking** `privacy.scan(content)` warning on
    save (the scope-less primitive, `privacy.py:283`); classify the
    handler **`_REDACTION_EXEMPT`** with the justification in §8 — because
    `_REDACTION_PROTECTED` semantically means *refuse on hit* and a
    non-refusing handler in that set is a coverage false-pass.
  - **Alternatives (kept):** (i) a **hard** Gate-A-style refusal (act on a
    scan hit, classify `_REDACTION_PROTECTED`) — rejected as over-reach
    for a host-global single-curator store, but **reconsider if/when the
    wiki has a configured push remote** (that condition could flip the
    lean and is an independent trigger — see §"Deferred-question tracking");
    (ii) no scan at all (pure parity with the ungated CLI `$EDITOR` path) —
    simpler, but forgoes the cheap advisory signal.
  - **Constraint:** do **not** wire `scan_text_content` for this (it
    requires a `scope: TargetScope` the wiki lacks and is refusal-coupled);
    if a hard gate is ever chosen, the scope value and refusal branch must
    be designed explicitly, not inherited from the sync path.

- **D-F. Tier landing for the read side.**
  - **Lean:** the **whole editor (read pane + edit + save + commit) is
    dev-tier** for v1, matching E-2; prod promotion of any read-only
    "view canonical source" pane follows ADR-0001 §5 separately.
  - **Alternatives (kept):** add the canonical-content **GET to the prod
    read tier** now (a read-only source view is arguably a natural E-1
    affordance) while keeping writes dev-only — defer to avoid widening
    the prod surface before the §5 criteria are met.

- **D-G. `WikiStore` primitives + exact route shape.**
  - **Lean:** add working-tree `read_asset_bytes(type, name[, vendor])` +
    `write_asset_bytes(...)` + `commit(paths, message, expected_head, path_tokens)` (an **isolated** commit via an out-of-worktree temp index / `commit-tree`, then reconcile the real index for those paths from the new HEAD; never a bare `git commit` over the live index)
    helpers to `WikiStore` (alongside the existing at-commit reads) and a
    wiki-root cross-process lock; routes as in Decision 4.
  - **Alternatives (kept):** put the write helpers in a new
    `wiki/edit.py` leaf (mirroring the `override.py` writer / `inspect.py`
    reader split) rather than widening `WikiStore`; fold the override edit
    into the existing `POST …/override` with an optional `content` field
    (rejected — overloads E-2's deliberately content-free contract).

- **D-H. Accessibility & i18n of the editor (gate, not deferred).**
  - **Lean:** treat the a11y/i18n/cache-bust items in Decision 7 as **P0
    acceptance criteria**, not fast-follow — the conflict banner, focus
    preservation, and locale parity are load-bearing for an editing
    surface.
  - **Alternatives (kept):** none recommended; listed for symmetry.

### Deferred-question tracking

If this ADR is accepted with the commit and privacy UX left provisional,
add to `docs/adr/TRACKER.md` — **in the same PR that merges it**, per that
file's "Adding a row" rule (the recommended-leans status above already
qualifies as a row-triggering status) — **two** rows (one per independent
question, since their triggers differ):

```
| 0027 §"Provisional decisions" D-A | in-browser wiki editor save→commit model | first dev-tier dogfooding of the editor; criteria in ADR-0027 §"Provisional decisions" | (none — tracked in ADR) | (event-driven) |
| 0027 §"Provisional decisions" D-E | wiki editor privacy posture (soft-warn vs hard-gate) | wiki gains a configured push remote; criteria in ADR-0027 D-E | (none — tracked in ADR) | (event-driven) |
```

**Resolution (2026-06-15):** the maintainer accepted the leans and the §3
commit affordance shipped, so the *build* is settled (Status → *Accepted*).
The two rows above were **still added** to `docs/adr/TRACKER.md` because both
questions carry genuine post-acceptance, event-driven re-evaluation triggers
that outlive the build: D-A re-checks the two-act save→commit UX after the
first dev-tier dogfooding, and D-E flips the privacy posture to a hard gate
**if/when the wiki gains a configured push remote**. They track *when to
revisit a shipped decision*, not an unbuilt one — so the "no rows if settled"
note above is superseded for this ADR.

## References

**Issues**

- **(none)** — ADR-0008 row E cites no tracking issue for the editor
  deferral (`0008-wiki-layer.md:286`); it is tracked in this ADR's
  §"Provisional decisions". Open a tracking issue only if the
  commit/privacy decision needs aggregated contributor signal.

**ADRs**

- **ADR-0008** — wiki layer; PR Breakdown **row E** is the deferral this
  ADR resolves; this ADR back-links to it and ADR-0008's **row E +
  References** are updated to name ADR-0027 in the same PR (its Status line
  + row-E E-3 clause are separately stale — E-3 shipped in #1357 — and
  left untouched to keep this PR editor-scoped).
- **ADR-0007** — namespace CRUD prod exposure; the dev/prod tier pattern
  the editor mounts under (also the pattern E-2 reused).
- **ADR-0001 §5** — dev→prod phase-readiness criteria governing any future
  promotion of the editor (router move + `STATE.uiMode==='dev'` gate
  removal; no kill-switch; git-revert rollback).
- **ADR-0011** — canonical artifact scope hierarchy; Gate A keys on
  `project_shared` (which the host-global wiki lacks) and declares
  authoring-side privacy out of scope (load-bearing for D-E).
- **ADR-0016** — three-tier canonical store; the tier vocabulary
  (`user`/`project_shared`/`project_local`) that this ADR notes is **N/A**
  to the host-global wiki.
- **ADR-0022** — canonical artifact version snapshots; its `versions/`
  store is keyed on tier scope and is therefore **out of scope** for the
  wiki (whose versioning is git). Its Decision (c) rejected
  git-as-versions *for the project store* only; overrides are never
  versioned there either (its invariant 9).
- **ADR-0009** — Context Gateway dashboard info surface; the editor's
  save is a push-direction author act consistent with the push-only
  posture and does not add a reverse/import surface.

**Source files** — line numbers reflect the branch at draft time; grep by
symbol if they drift.

- `packages/memtomem/src/memtomem/web/routes/wiki.py` — E-1 read tier
  (`GET /api/wiki` + `diff`/`lint`); host-global (no scope machinery).
- `packages/memtomem/src/memtomem/web/routes/wiki_mutations.py` — E-2
  dev-tier seed `POST …/override` (`seed_wiki_override`); runs a bare
  `asyncio.to_thread` with no lock/timeout (`:69-76`); never commits;
  returns `wiki_dirty`.
- `packages/memtomem/src/memtomem/web/routes/_wiki_common.py` — shared
  `AssetType` Literal + name/vendor validators + `_wiki_absent`
  (fixed-message 404, no path leak, `:42-47`).
- `packages/memtomem/src/memtomem/web/routes/_errors.py` — `_error`
  envelope (`:102`, **no** redaction) + `_redact_message` (`:76`,
  separate, must be wired in).
- `packages/memtomem/src/memtomem/web/routes/context_skills.py` — the
  per-project canonical editor's GET (`:263-307`) / PUT
  (`SkillUpdateRequest :384`) / 409 (`_mtime_conflict_response :400`) /
  `_gateway_lock` + force-audit (`:438-472`) contract the wiki editor
  mirrors.
- `packages/memtomem/src/memtomem/privacy.py` — `scan(text) ->
  list[RedactionHit]` (`:283`), the scope-less soft-warn primitive D-E
  uses.
- `packages/memtomem/src/memtomem/context/privacy_scan.py` —
  `scan_text_content` (`:244`, requires `scope: TargetScope`,
  refusal-coupled) — the gate primitive D-E deliberately does **not** use.
- `packages/memtomem/src/memtomem/web/middleware/csrf.py` — CSRF/Origin/
  Host enforcement on unsafe `/api/*` methods.
- `packages/memtomem/src/memtomem/web/app.py` — `_PROD_ROUTERS` (`:91`)
  vs `_DEV_ONLY_ROUTERS` (`:115-124`); `mode=='dev'` mount gate
  (`:246-248`).
- `packages/memtomem/tests/test_web_invariants_registry.py` —
  `_REDACTION_PROTECTED` (`:147`, = "scan AND refuse") / `_REDACTION_EXEMPT`
  (`:160`, e.g. `seed_wiki_override` at `:235`) classification the new
  handlers join; the "calls `enforce_write_guard`" test at `:391`.
- `packages/memtomem/src/memtomem/wiki/store.py` — `WikiStore`
  (`current_commit :165`, `is_dirty :215`, `_git` raising raw stderr
  `:100`, `WikiNotFoundError` embedding the abs path `:128`; at-commit
  reads only — the new working-tree read/write/commit primitives land here
  or in a `wiki/edit.py` leaf, D-G).
- `packages/memtomem/src/memtomem/wiki/override.py` — `seed_override` /
  `render_seed_bytes` (atomic write + `.bak` + preconditions-before-
  mutation pattern, `:137-160`, the Save reuses).
- `packages/memtomem/src/memtomem/wiki/inspect.py` — `diff_override` /
  `lint_asset` (working-tree reads `:113-114,145`; re-painted after a
  canonical save).
- `packages/memtomem/src/memtomem/context/{agents,commands}.py` —
  `parse_canonical_agent` / `parse_canonical_command` (default
  `layout="flat"`, `commands.py:174`; the editor must pass `layout="dir"`).
- `packages/memtomem/src/memtomem/cli/wiki_cmd.py` — `mm wiki <type>
  override --editor` + the manual `git add && git commit` hint
  (`_run_seed_override`; the sibling per-type `mm wiki <type> commit` D-C
  alternative has since shipped — §3).
- `packages/memtomem/src/memtomem/web/static/context-gateway.js` — the ctx
  editor UI: `_ctxStashDraft` / conflict modal (`:3943-4082`) + the
  `ctx-edit-area` Save/Cancel handlers to reuse.
- `packages/memtomem/src/memtomem/web/static/wiki.js` — the wiki browser
  controller the editor extends (`_renderWikiDetail :454`, dirty-badge
  repaint `:283`, seq-guard, dev-mode toggle).
- `packages/memtomem/src/memtomem/web/static/index.html` — `#wiki-detail`
  mount (`:687`) + `?v=` cache-bust pins.
- `packages/memtomem/src/memtomem/web/static/locales/{en,ko}.json` —
  reusable editor/conflict keys; the `wiki_desc` string (`:431`) to update.
