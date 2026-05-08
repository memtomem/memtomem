# ADR-0009: Context Gateway dashboard — info surface & sync direction policy

**Status:** Proposed
**Date:** 2026-05-08
**Context:** Q-PR1..3 (#824/#827/#828) shipped i18n, copy, and visual parity
on the Context Gateway dashboard. Five information-density / direction-
visibility items remain (Info-1..5, tracked in #829..#834). They share a
single underlying design question that this ADR resolves before any of
the five implementation issues lands.

## Background

ADR-0001 fixed the **per-project sync pipeline**: canonical → multi-runtime
fan-out, on_drop severity, phase independence, GUI expansion order. It
deliberately did not address the **dashboard UX surface**, because at the
time the dashboard did not exist as a distinct concept — the dev-mode
Context Gateway page (Phase D) was a flat list of per-runtime cards.

PR #816 (2026-05-06) introduced the dashboard as the prod-tier entry
point under the Agent Integrations sidebar group. Q-PR1..3 polished its
i18n and visual layer. The remaining five items (Info-1..5) all touch a
question that ADR-0001 did not need to answer:

> What is the dashboard's *role* in the user's mental model — a thin
> summary that defers all detail to the leaf pages, or an authoritative
> cockpit that surfaces runtime/project/freshness state and owns
> bidirectional (push + pull) sync actions?

Without a coherent answer, the five items would land as five independent
PRs each making local UX trade-offs that drift away from one another.
This ADR proposes a unified direction across all five.

## Decision

The dashboard is an **informational cockpit, not an action cockpit**: it
surfaces enough context for the user to understand "what's in scope" and
"what's stale" at a glance, but the **mutation surface stays push-only**
and matches ADR-0001's existing sync semantics. Pull/import remains a
leaf-only action with an explicit pointer from the dashboard.

The five points below resolve Info-1..5 jointly.

### 1. Information density — surface read-only context (Info-1, Info-2, Info-3)

The dashboard renders three new read-only context elements above the
4-tile grid:

- **Detected runtimes chip strip** (Info-1, #830) — one chip per
  declared runtime; opacity / `badge-gray` for declared-but-undetected.
  Availability is **per-surface** and reuses the existing detection
  surface in `packages/memtomem/src/memtomem/context/detector.py`:
  `detect_skill_dirs()`, `detect_agent_dirs()`, `detect_command_dirs()`
  are **directory-probe based** (project-scope `SKILL_DIRS` /
  `AGENT_DIRS` / `COMMAND_DIRS` constants) because the
  `SkillGenerator` / `AgentGenerator` / `CommandGenerator` protocols
  expose `target_dir()` / `target_file()` only — no uniform
  `is_available()` API exists across these registries. `detect_
  settings_files()` is the sole `is_available()` caller because the
  `SettingsGenerator` protocol (`packages/memtomem/src/memtomem/
  context/settings.py:63,119`) is user-scope (`~/.claude/`, etc.) and
  needs the explicit availability probe. The aggregate per-runtime
  flag on `/api/context/overview` is **OR across declared surfaces**:
  a runtime counts as detected when *any* of its surfaces resolves a
  target on disk. Exposed via a new `detected_runtimes:
  list[{name, available}]` field.
- **Project root indicator** (Info-2, #831) — truncated path with
  `title="<full>"` tooltip in the dashboard header. Single
  current-project semantics (see §4 below). Sourced from the existing
  `project_root: Path` dependency
  (`packages/memtomem/src/memtomem/web/routes/context_gateway.py:112`),
  newly emitted as `project_root: str`.
- **Last-sync freshness indicator** (Info-3, #832) — `Last sync: 5 min
  ago` line in the header, tooltip with full ISO timestamp. Sourced
  from canonical-source mtime (cheapest option; matches Health Report
  semantics and avoids a new persisted log).

**Why read-only:** the three items above are *context*, not *actions*.
Surfacing them resolves the "0 skills — is anything registered?"
ambiguity without expanding the dashboard's mutation surface. Each is
diagnosable by reloading; none requires write access to anything.

**Why mtime over a sync-event log for Info-3:** an event log adds
persistence + a new write path; mtime reuses existing artifacts. The
loss is "we cannot tell apart edits from explicit syncs" — acceptable
for a freshness indicator. If a future need surfaces (e.g. "show me
syncs that succeeded vs ones I edited around"), revisit.

### 2. Sync direction — keep dashboard push-only, point to leaves for pull (Info-4)

`Sync All` on the dashboard remains push-only (canonical → runtime).
Pull/import stays leaf-only.

When a tile shows partial sync, the dashboard renders an inline pointer
derived from the existing per-status counts emitted by `_count_statuses`
(`packages/memtomem/src/memtomem/web/routes/context_gateway.py:33`) over
the 4-value enum produced by `diff_skills` / `diff_commands` /
`diff_agents`: `in sync`, `out of sync`, `missing target`,
`missing canonical`. **No new wire fields are required for direction** —
the signal is already carried by which counts are non-zero:

- `missing_target > 0` (canonical has it, runtime does not) → push is
  unambiguous → "Run Sync All to push N missing entries."
- `missing_canonical > 0` (runtime has it, canonical does not) → pull
  is unambiguous → "N runtime entries are not in canonical — open
  <leaf> to import." Hyperlink anchors directly to the leaf section.
- `out_of_sync > 0` (both sides exist and differ) → **direction-
  neutral** → "Open <leaf> to resolve N differences." The `out of
  sync` status carries no direction signal, and ADR-0001 §1 already
  rejected mtime-based "newer" detection as filesystem-fragile and
  CI-flaky — the dashboard does not attempt to guess. Per-entry
  direction is the user's call, surfaced on the leaf where both sides
  are renderable side-by-side.
- Mixed combinations render each applicable line in priority order:
  `missing_target`, then `out_of_sync`, then `missing_canonical`. Each
  carries its own count and link target.

The settings tile (Phase D) cannot produce `missing canonical` by
design — no `extract_settings_to_canonical` path exists because the
additive merge that owns settings sync cannot distinguish canonical-
authored from user-authored entries (ADR-0001 §5 unidirectional
readiness contract). Its inline pointer reduces to the first and third
rules above.

**Why not add `Import All`:** bidirectional bulk actions create a class
of UX-traps where the user clicks the wrong direction and silently
overwrites local edits or canonical content. ADR-0001's `extract_*` is
intentionally a single-direction primitive per leaf for this reason.
Concentrating bulk-pull on the dashboard would force the user to read
direction badges per tile before clicking — a regression in the
"glance" property the dashboard is supposed to have.

**Why an explicit pointer rather than nothing:** the original Info-4
catalog finding was "the dashboard cannot tell the user *which* action
to take." An inline pointer surfaces direction *intent* without owning
direction *action*. The user reads "open the Skills page to import"
and learns where to act, then acts there.

This decision is conservative: it preserves ADR-0001's invariants. If
prod-user feedback over the next 2 weeks (mirroring ADR-0001 §5's dwell
time) signals that the pointer is insufficient, a follow-up ADR can
revisit `Import All` as a peer action.

### 3. Deep-link semantics — URL query string carrier (Info-5)

Clicking an issue card on the dashboard adds `?section=<type>&filter=
<status>&artifact=<name>` to the URL and navigates to the leaf section.
Leaf pages read the query string on mount, apply the filter, scroll to
the named artifact, and visually highlight it for ~2 seconds.

**Why query string over app-state or hash anchor:**

- Bookmarkable + back-button-friendly. A user who opens an issue link
  in a new tab lands on the same filtered leaf state as the dashboard
  click.
- Shareable. "Open this URL to see the artifact I'm asking about" is a
  natural support-channel pattern; app-state object cannot carry that.
- No coupling between markup IDs and URL fragments. Hash anchors would
  force every artifact-name change to ripple through URL semantics.

A negative pin test (per `feedback_pin_invert_symmetric_assertion.md`)
verifies the leaf does NOT render its full list when the carrier
requests a single artifact, preventing silent regression to "filter
ignored, full list shown."

### 4. Project-scope semantics — single current-project (Info-2)

The dashboard reflects the **current cwd's** registered project. Multi-
project navigation happens via the existing sidebar's project-switcher
(or the registration UI), not by aggregating counts across projects on
the dashboard.

**Why single-project:** aggregate counts hide which project's edits a
sync would affect, undermining the trust model. A user looking at "27
skills" must know whether "Sync All" pushes to Project A's runtime or
Project B's. Per-project context keeps that question off the table.

**Empty state copy reflects this:** "No skills registered yet — visit
[Skills] to add some" replaces the ambiguous `0 skills` count.

### 5. Backend response shape

`/api/context/overview` gains three additive fields, all optional from
the client's perspective:

```json
{
  "skills": { ...existing... },
  "commands": { ...existing... },
  "agents": { ...existing... },
  "settings": { ...existing... },
  "project_root": "/abs/path/to/project",
  "detected_runtimes": [
    { "name": "claude", "available": true },
    { "name": "gemini", "available": false },
    { "name": "codex", "available": true }
  ],
  "last_synced_at": "2026-05-08T12:34:56Z"
}
```

**Why additive:** preserves the Q-PR3 envelope clients (Web UI v0.1.36+)
already understand. Older clients ignore unknown fields. The four
existing tile envelopes are unchanged.

**Direction signal source for §2.** The four-status enum produced by
`diff_skills` / `diff_commands` / `diff_agents` (`in sync`,
`out of sync`, `missing target`, `missing canonical`) already flows
through `_count_statuses` (`packages/memtomem/src/memtomem/web/routes/
context_gateway.py:33`) into the per-tile envelope as named count
fields (`in_sync`, `out_of_sync`, `missing_target`, `missing_canonical`
— the helper lifts every observed status into a count key). §2's
inline pointer logic reads those existing counts; **no new tile
fields, no mtime comparison, no diff-output extension is required**.
The settings tile is the only envelope that omits `missing_canonical`
by design (§2 last paragraph).

## Consequences

- **Five implementation issues unblock once this ADR reaches Accepted.**
  Each ships as its own PR, gated only on this ADR.
- **`/api/context/overview` grows three new top-level fields.** The
  Q-PR3 backwards-compat tests still pass; new test coverage required
  for each field's wire shape.
- **Settings tile rendering is unaffected.** Q-PR3's count envelope and
  the `_SETTINGS_STATUS_I18N` map continue as-is.
- **The "leaf-only Import" rule becomes load-bearing.** Future PRs that
  add bulk-pull primitives must either retire this ADR (with explicit
  rationale + ADR-0001 cross-reference) or land them as leaf-only
  group actions, not dashboard-level.
- **Single-project dashboard semantics become a gate for any future
  multi-project rollup work.** A "Cross-project overview" feature, if
  ever requested, would need its own surface (sidebar group entry, not
  a dashboard mode-flip) and its own ADR.

## Considered & rejected

- **Bidirectional bulk actions on the dashboard (`Import All`).**
  Rejected for the reasons in §2: glance-property loss, ADR-0001
  invariant erosion, accidental-overwrite UX trap. Revisitable if
  pointer-only proves insufficient.
- **App-state object as deep-link carrier.** Rejected for the reasons in
  §3: not bookmarkable, not shareable, marginal implementation savings.
- **Aggregate-all-registered project counts on the dashboard.** Rejected
  for the reasons in §4: undermines the per-project trust model.
- **Persisted sync-event log for Info-3.** Rejected for v1: mtime is
  cheaper and recovers the freshness signal. Revisitable if a real
  workflow demands sync-vs-edit disambiguation.

## RFC

This ADR is **Proposed**, not Accepted. The five proposed directions are
deliberately conservative — they preserve ADR-0001's invariants and the
current dashboard's "glance" property. Reviewers are encouraged to push
back on any of the five if a different direction better serves real
workflows. The discussion period is **≥ 1 week** (mirrors ADR-0008's
PR-A cadence) before promotion to Accepted.

## References

- ADR-0001 — context gateway sync policies (push-only invariant for §2).
- ADR-0008 — wiki layer (proposed → accepted cadence precedent).
- Tracker placeholder: #829.
- Implementation issues: #830 (Info-1), #831 (Info-2), #832 (Info-3),
  #833 (Info-4), #834 (Info-5).
- Q-PR series that preceded the split: #824, #827, #828.
- `packages/memtomem/src/memtomem/web/routes/context_gateway.py` — the
  `/api/context/overview` endpoint that gains §5's three new fields.
