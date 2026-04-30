# ADR-0004: Sources "+ 경로 추가" vs Index tab path-scan overlap

**Status:** Proposed (deferred pending trigger)
**Date:** 2026-04-30
**Context:** Issue #569 audited two web UI surfaces that look similar but
do different work. PR #571 made Sources `+ 경로 추가` register + index in
one step (`auto_index=true`); the Index tab still ships an "Index Files"
path-scan form. Whether the second surface still earns its slot is the
question this ADR answers.

## Existing surfaces (post-PR #571)

| Surface | Endpoint | Adds to `memory_dirs` | Indexes immediately | Custom NS | Force | Streaming |
|---------|----------|---|---|---|---|---|
| Sources `+ 경로 추가` | `POST /api/memory-dirs/add` (`auto_index=true` from Web UI) | ✅ | ✅ | ❌ | ❌ | ❌ |
| Sources card `[Reindex]` | `POST /api/index` | ❌ | ✅ | ❌ (default) | ❌ | ❌ |
| Index tab "Index Files" → `Index` | `POST /api/index` | ❌ | ✅ | ✅ | ✅ | ❌ |
| Index tab "Index Files" → `Index with Progress` | `GET /api/index/stream` (SSE) | ❌ | ✅ | ✅ | ✅ | ✅ |
| Index tab "Upload Files" | `POST /api/upload` | ❌ (writes into `~/.memtomem/uploads/`) | ✅ | ❌ | ❌ | ❌ |
| Index tab "Add Memory" | `POST /api/items` (or `mem_add`) | ❌ | ✅ (single chunk) | ✅ | n/a | n/a |

`/api/index` enforces a 403 guard if the path is not already inside a
configured `memory_dirs` entry (`packages/memtomem/src/memtomem/web/routes/system.py:809-813`).
That means the Index tab's "Index Files" form **cannot** be used to add a
new directory — despite the name suggesting it can. Pre-#571 the workflow
was "Sources `+ 경로 추가` registers, Index tab indexes". Post-#571 the
register-step indexes too, leaving "Index Files" with a narrow remit:
**reindex an already-registered dir with options** (Force, custom NS,
streaming progress).

## Real overlap, real differences

What overlaps between Sources card `[Reindex]` and Index tab "Index Files":

- Both index a single registered dir.
- Both call `/api/index` (the streaming variant aside).
- Both produce the same backend stats shape.

What's unique to Index tab "Index Files":

- **Force re-index** toggle (rebuild existing chunks even if content hash matches).
- **Custom namespace at index time** (override the per-dir / config default).
- **Streaming progress** with file-by-file feedback (`/api/index/stream`).
- **Recursive on/off** (in practice always on; the toggle is vestigial).

What's unique to "Upload Files" and "Add Memory":

- These are not duplicated anywhere. They write fresh content into the
  store rather than indexing existing files. They are out of scope for
  this ADR.

## Considered options

1. **Drop "Index Files" entirely.** Move Force re-index and Custom NS
   into the per-dir Sources card actions. Sources card grows two more
   options (likely behind a kebab menu). Streaming progress goes away or
   moves to the per-card Reindex button.
   - *Risk:* loses streaming progress UI; Force becomes per-card, which
     may hurt discoverability for users who want it as a "knob, not a
     per-row action".
2. **Reframe "Index Files" → "Reindex Options".** Keep the form, rename
   the heading and i18n keys, replace the free-text path input with a
   dropdown of registered dirs (eliminates the misleading "type any
   path" affordance). Force defaults off; streaming stays. Help-bar text
   updated to "Tweak reindex parameters on a registered directory."
   - *Effort:* small (HTML strings + dropdown wiring + i18n).
   - *Wins back:* clarity. The form's intent matches its capabilities.
3. **Keep both as-is, add help text.** Document the distinction inline
   ("To add a new directory, use the Sources tab"). Cheapest change.
   - *Risk:* the input still accepts arbitrary paths and 403s on
     unregistered ones — the trust-UX gap stays.
4. **Defer pending usage signal.** Track which surface users actually
   reach for. If "Index Files" is used mostly for force-reindex,
   Option 2 is the right call. If it's mostly used as a confused "first
   add" attempt (i.e., 403 errors are common), Option 1 is right.
   - *No code change yet.* This ADR records the analysis so the next
     touch of the Index tab has the prior art at hand.
5. **Index tab redesign.** Promote Index tab to a global "reindex
   dashboard" — bulk reindex across vendors, watchdog status, queued
   re-index jobs. Way larger than the question this ADR raised.
   - *Out of scope.* Tracked separately if it ever earns a trigger.

## Decision

**Option 4 (defer)** for now, with a leaning toward **Option 2 (reframe)**
when implementation is triggered. Option 1 (drop) is on the table only
if a trigger reveals "Index Files" is mostly used as a first-add
anti-pattern.

### Why hold instead of implement now

- Single signal (issue #569 audit). Below the "twice = pattern" bar.
- Post-#571 is fresh — the Web UI just shifted users toward the
  one-step register+index flow; usage patterns will need 1-2 release
  cycles to stabilize.
- The ambiguity is real but not blocking: nothing crashes, no data is
  lost. Users can still index dirs.
- A premature reframe risks churning the i18n keys + HTML markup before
  we know which capabilities deserve the screen real estate.

### Trigger criteria (any one promotes to "Accepted")

1. A user-facing report (issue, support thread, review comment) shows
   confusion between Sources `+ 경로 추가` and Index "Index Files" after
   PR #571 lands in a release.
2. Server logs / telemetry (if/when added) show the `/api/index`
   endpoint receives a meaningful share of 403s — i.e., users are
   typing unregistered paths into "Index Files" expecting it to act
   like Sources `+ 경로 추가`.
3. A second feature-redundancy report lands (e.g., "Sources sort and
   Index status overlap") — once is an oddity, twice is a pattern that
   warrants a wider Web UI surface review.
4. A change to `/api/index`'s contract (e.g., dropping the 403 guard,
   accepting unregistered paths) forces a UX refresh anyway. Bundle the
   reframe into that PR.

### Implementation breakdown (when triggered, Option 2 path)

Three small PRs, in order:

- **PR-A: rename + scope.** Heading "Index Files" → "Reindex Options"
  (i18n keys `index.title`, `index.help`). Help-bar text updated to
  describe reindex-of-registered-dir intent. No behavior change. ~30 LOC
  HTML/i18n.
- **PR-B: registered-dir dropdown.** Replace the free-text `index-path`
  input with a `<select>` populated from `STATE.memoryDirs`. Free-text
  fallback retained behind a "Custom path…" item for power users.
  Submit still hits `/api/index`; 403 stays the same but is now an
  edge case rather than the modal failure mode. ~80 LOC JS + HTML.
- **PR-C (optional): default Force off + safety prose.** Force toggle
  ships unchecked; a small inline note explains the cost ("Rebuilds
  existing chunks even if content is unchanged"). ~15 LOC.

If the trigger reveals Option 1 is actually right (drop the section),
the breakdown changes:

- **PR-A': move per-dir.** Add Force + custom-NS as options on the
  Sources card `[Reindex]` button (kebab menu or inline expander).
  ~60 LOC.
- **PR-B': remove the section.** Drop "Index Files" markup, keep
  Upload Files and Add Memory. Update Index tab help-bar copy to
  reflect the narrower scope. ~40 LOC removed.

## Consequences

- The status quo persists in the next release: both surfaces stay
  visible, with PR #571's auto-index toast nudging users toward Sources
  for first-add. Users seeking Force / custom-NS / streaming continue
  using Index tab "Index Files" as today.
- This ADR exists so the next contributor (or future self) doesn't
  redo the audit. When a trigger fires, the analysis above is the
  starting point, not the deliverable.
- Issue #569 stays open until PR2 (default flip on `auto_index`) lands;
  this ADR is the record for sub-question (c) and does not gate the
  issue's close.

## References

- Issue #569 — UX audit that motivated this ADR.
- PR #568 — single Sources panel (vendor grouping).
- PR #571 — `/api/memory-dirs/add` opt-in `auto_index`.
- PR #572 — Sources vendor sub-tabs (sub-question (b)/UX layout).
- ADR-0003 — same "deferred pending trigger" shape.
