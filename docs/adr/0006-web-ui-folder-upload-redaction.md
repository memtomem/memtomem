# ADR-0006: Web UI folder/upload privacy redaction trust-boundary

**Status:** Proposed (deferred pending trigger)
**Date:** 2026-04-30
**Context:** Issue #585 — PR #575 follow-up review surfaced that
`packages/memtomem/src/memtomem/privacy.py: DEFAULT_PATTERNS` is enforced
only on the MCP `mem_add` / `mem_edit` paths. The Web UI's folder-index and
upload surfaces accept content raw, bypassing the LTM trust boundary that
CLAUDE.md asserts ("STM-bypass must not be safety-bypass").

## Background

`privacy.py: DEFAULT_PATTERNS` is the LTM project's secret-pattern allowlist
— eight regexes covering API keys, password assignments, provider tokens
(`sk-`, `ghp_`, `xox[bps]-`, `github_pat_`, Stripe/Clerk/Svix `(sk|pk|rk)_(live|test)_`,
npm `npm_`, AWS `AKIA|ASIA`, JWT `eyJ…`, PEM private-key headers). The
module docstring records that this is **secret-class only** by intent —
PII-class patterns from STM do not auto-sync because they would force
`force_unsafe=True` on most legitimate prose.

The existing gate model is `mem_add()` in
`server/tools/memory_crud.py:78-104`:

```python
hits = privacy.scan(content)
if hits:
    if force_unsafe:
        # bypass + audit log via mem_add_redaction_stats
        ...
    else:
        raise ToolError("write rejected. Retry with force_unsafe=True ...")
```

`mem_edit` (line 445) follows the same shape. Compose-mode in the Web UI is
covered separately by **#580 (CLOSED)** — a client-side regex pre-check
against `GET /api/privacy/patterns` (`web/routes/system.py:278`) shows a
confirm dialog before submission. That handles the "user is the typist"
case where per-input confirm is meaningful.

The remaining gap is on bulk surfaces, where per-file confirm is not a
meaningful UX:

| Surface | Endpoint | Handler | Privacy gate today? |
|---------|----------|---------|---------------------|
| Index a registered dir | `POST /api/index` | `trigger_index` (`system.py:835`) | ❌ none |
| Index a registered dir (SSE) | `GET /api/index/stream` | `index_stream` (`system.py:795`) | ❌ none |
| Reindex all `memory_dirs` | `POST /api/reindex` | `system.py:688` | ❌ none |
| Add dir + auto-index | `POST /api/memory-dirs/add` (`auto_index=true`) | `system.py:416` | ❌ none |
| Upload + index | `POST /api/upload` | `upload_files` (`system.py:911`) | ❌ none |
| Compose textarea | client-side, `POST /api/add` | `mem_add` (MCP path) | ✅ via `mem_add` + #580 client warn |

All five bulk surfaces converge into `IndexEngine.index_file()` /
`index_directory()` (`indexing/engine.py`). None of them call
`privacy.scan()` before persisting. The unspoken assumption — "Web UI =
local user, the boundary is at MCP" — breaks at the moment a user runs
`mm web` on a non-loopback bind, runs it on a shared workstation, or
indexes a folder that contains a `.env` they didn't realize was there.

## The five decisions this ADR settles

The issue body enumerated five axes. Each is treated below with options,
leaning, and rationale.

### Axis A — Scope

> Apply to folder index? upload? both?

| Option | Behavior |
|--------|----------|
| A.1 — folder index only | leave upload raw; user is "actively pasting" |
| A.2 — upload only | folder is "the user's filesystem, their problem" |
| A.3 — **both bulk surfaces** | converge at `IndexEngine`, single gate |

**Leaning: A.3.** The trust-boundary argument doesn't bend at the
upload-vs-index seam. `IndexEngine.index_file()` is the natural single
chokepoint, and putting the gate there covers all five route surfaces in
one place. Splitting by route would force the same regex pass to live in
five handlers and would make `force_unsafe` plumbing five times more code.

### Axis B — Action

> Silent mask, hard reject, or warn-then-include?

| Option | Behavior |
|--------|----------|
| B.1 — silent mask | replace match → `[REDACTED]`, persist redacted version |
| B.2 — **hard reject** | refuse to index the file, surface error in toast / SSE event |
| B.3 — warn-then-include | persist + flag chunk for review |

**Leaning: B.2.** Three reasons:

1. *Sibling consistency.* `mem_add` rejects with `force_unsafe=True` as
   the only bypass. Diverging behavior on bulk surfaces ("MCP rejects, web
   masks") would create an inconsistent trust model for the same pattern
   set.
2. *User expectation.* Silent mask violates the "what I wrote is what I
   stored" contract that markdown-first memory implies — content drift
   between the file on disk and the chunk in storage is a debugging
   nightmare for the user.
3. *Operational simplicity.* Reject is observable (an error event); mask
   is invisible until the user goes hunting for the masked-out string and
   finds `[REDACTED]` instead.

The cost of B.2 is real: a single secret in one file blocks the whole
folder. The mitigation is in axis E (override).

### Axis C — Pattern set

> Same as `DEFAULT_PATTERNS`? Subset for folder mode (so debug-note secrets
> don't break the workflow)?

| Option | Behavior |
|--------|----------|
| C.1 — **same `DEFAULT_PATTERNS`** | eight secret-class regexes, identical to MCP |
| C.2 — folder-mode subset | drop e.g. JWT (high false-positive on docs) |
| C.3 — stricter set + PII | add email/phone/etc. for bulk surfaces |

**Leaning: C.1.** The patterns are already secret-class only by design;
dropping any of them for folder-mode would create two semantically distinct
"secret" definitions in one codebase and re-open the asymmetric-sync
question. Adding PII (C.3) was the explicit reject in
`privacy.py` module docstring — PII would force `force_unsafe=True` on
most prose. C.1 keeps the asymmetric-sync invariant from CLAUDE.md intact.

### Axis D — Retroactive

> Apply to existing chunks? Backfill? Leave as-is?

| Option | Behavior |
|--------|----------|
| D.1 — backfill | scan all existing chunks, reject (or mask) on hit |
| D.2 — **leave as-is** | new gate is forward-only |
| D.3 — user-trigger backfill | add a "scan storage for secrets" CLI / UI action |

**Leaning: D.2.** Forward-only is the cheap and correct default:

- *Cost.* Backfill at scale (tens of thousands of chunks) is a heavy
  reindex. The benefit is bounded — chunks already in storage are
  already in storage.
- *Boundary semantics.* The trust boundary is at *write*. A retroactive
  scan would be acting on data that already crossed the boundary; that's
  an audit feature, not a gate feature. D.3 is the correct shape if the
  audit feature is ever wanted, but it's separable from this ADR.

### Axis E — Override

> `force_unsafe=True` exposed in UI? CLI? Config? Audit log?

| Option | Behavior |
|--------|----------|
| E.1 — **UI toggle + audit log** | "Index unsafely" checkbox; bypass logged |
| E.2 — CLI flag only | `mm index --force-unsafe`; no GUI surface |
| E.3 — config-level always-on | `privacy.bulk_force_unsafe = true` in `config.json` |
| E.4 — no override | bulk surfaces have no escape hatch (rejection is final) |

**Leaning: E.1.** Two parts:

- *UI toggle* — `mem_add` already exposes `force_unsafe=True` over MCP. A
  Web UI checkbox at the same trust level is the consistent extension.
- *Audit log* — every bypass writes a row to `mem_add_redaction_stats`'s
  underlying audit table (`memory_crud.py:88, 465`) with chunk-id +
  matched-pattern hash + caller surface. This is the only condition
  under which "my secret got indexed" is debuggable after the fact.

E.2 (CLI only) is too narrow — the `mm web` user has no terminal in flow.
E.3 is too blunt — making bypass the persisted default flips the trust
semantics. E.4 (no override) breaks the "intentional debug note about an
old, rotated key" workflow that ADR-0005's force-reindex contract revealed
is real.

## Decision

**Defer.** Leaning toward **A.3 + B.2 + C.1 + D.2 + E.1** when implementation
is triggered.

### Why hold instead of implement now

- Single signal (PR #575 follow-up review). Below the "twice = pattern"
  bar that ADR-0004 also held to.
- Compose-mode (#580) just shipped. The product position right now is
  "client-side warn covers Compose; bulk surfaces are guarded by the
  user-is-local assumption". Promoting the bulk-surface fix needs a
  signal that the assumption broke (or is about to break) in practice.
- The implementation has non-trivial UX cost (toast/SSE error wiring,
  audit log surface, override toggle) that would land 5+ files of
  changes — too much to ship on a single follow-up review.

### Trigger criteria (any one promotes to "Accepted")

1. **Boundary breach reported.** Any external report — security review,
   user issue, mailing-list — that names the bulk surfaces as the entry
   point. Treat as immediate Accepted regardless of other signals.
2. **STM secret-class pattern added.** When `memtomem-stm/proxy/privacy.py:
   DEFAULT_PATTERNS` adds a new secret-class entry, the asymmetric-sync
   PR to `packages/memtomem/src/memtomem/privacy.py` is the natural
   moment to also close the bulk-surface gap, since the new pattern
   would otherwise be enforced only on MCP and explicitly bypassed on
   Web UI.
3. **`mm web` non-loopback bind documented.** If `mm web` adds a flag
   for `--host 0.0.0.0` (or equivalent — remote access, shared
   workstation, container deploy), the "Web UI = local user" assumption
   no longer holds and the boundary must move with it.

## Implementation outline (when triggered)

In rough order, all in `packages/memtomem/src/memtomem/`:

- **PR-A — Engine gate + route wiring.**
  - Add `force_unsafe: bool = False` to `IndexEngine.index_file()` and
    `index_directory()` in `indexing/engine.py`. On entry, read file
    content and call `privacy.scan(content)`; on hit without
    `force_unsafe`, raise a typed `PrivacyRejection` (carrying file path
    + matched pattern indices) and abort that file's index.
  - Wire callers: `web/routes/system.py:trigger_index()` (835),
    `index_stream()` (795), `reindex` (688), `memory_dirs/add` with
    `auto_index=true` (416), `upload_files()` (911). Each handler
    catches `PrivacyRejection` and converts to the appropriate response
    shape (HTTPException for one-shot; SSE error event for stream).
  - Reuse `mem_add`'s audit-log helper from `server/tools/memory_crud.py`
    so bulk bypass writes the same `mem_add_redaction_stats` rows as
    MCP bypass — single audit surface.
- **PR-B — Web UI override toggle + audit surface.**
  - Add an "Index without privacy gate (audit-logged)" checkbox to the
    Index tab and the Sources `+ 경로 추가` modal. On submit, pass
    `force_unsafe=true` query/body param to the relevant endpoint.
  - Surface the audit log: extend the existing redaction-stats panel
    (or add a new tab) so bulk bypasses are visible alongside MCP
    bypasses.
- **PR-C (optional, gated by separate signal) — CLI parity.**
  - `mm index --force-unsafe` plumbing reuses PR-A's `IndexEngine`
    parameter. Hold until a CLI user reports needing it; the bulk
    workflow is web-driven for now.

## Consequences

- **New rejection mode for bulk surfaces.** Users indexing a folder that
  contains a real or look-alike secret will see an error toast / SSE
  event instead of the chunk silently appearing. This is the intended
  behavior; it should be telegraphed in the next minor's CHANGELOG as a
  behavior change.
- **Audit log grows.** Every bulk bypass adds rows to the redaction-stats
  audit table at the same rate as MCP bypass. Storage cost is small
  (one row per bypass) but the table grows linearly — note for any
  future eviction policy.
- **`IndexEngine` API gains a parameter.** External callers (currently
  none outside this repo, but the engine is part of the public Python
  API) get a new keyword. Default `False` keeps the existing behavior
  for code that doesn't pass it.
- **Cross-repo sync invariant gets a hook.** STM's secret-class pattern
  additions now have a documented reason to ramp the LTM gap-close in
  the same release window — the asymmetric-sync rule in CLAUDE.md
  becomes an active sync trigger rather than a static comment.
- **Compose / bulk asymmetry resolved.** Today Compose warns (client),
  MCP rejects, bulk passes. After this ADR's implementation: Compose
  warns (client) + rejects (server, via `mem_add`), bulk rejects, MCP
  rejects. The boundary is uniform.

## Considered & rejected upstream

These were considered when drafting and folded into the leaning above:

- **Move the gate to `storage.upsert_chunks()` instead of `IndexEngine`.**
  Rejected: storage is below the chunking boundary; rejecting at storage
  means a half-chunked file partially commits. Engine is the right
  layer — pre-index, all-or-nothing per file.
- **Reuse `mem_add` for every bulk file.** Rejected: `mem_add` is
  document-shaped (one chunk per call); folder index is file-shaped
  (many chunks per file). The shapes don't match without unwrapping.
- **Skip `index_stream` for now.** Rejected: SSE is the
  high-throughput surface; skipping it leaves the largest hole open.

## References

- Issue #585 — ADR placeholder, this document is the deliverable.
- Issue #580 (CLOSED) — Compose-mode client-side warning. Sibling, not
  superseded.
- ADR-0004 — same "deferred pending trigger" shape this ADR mirrors.
- CLAUDE.md (project root) — "STM-bypass must not be safety-bypass" trust
  boundary; `privacy.py` asymmetric-sync rule.
- `packages/memtomem/src/memtomem/privacy.py:42-57` — `DEFAULT_PATTERNS`
  (eight secret-class regexes).
- `packages/memtomem/src/memtomem/privacy.py:268` — `scan()` entry point.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:78-104` —
  existing gate model on `mem_add`.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:445-465` —
  same model on `mem_edit`.
- `packages/memtomem/src/memtomem/web/routes/system.py:835` —
  `trigger_index` (POST `/api/index`).
- `packages/memtomem/src/memtomem/web/routes/system.py:795` —
  `index_stream` (GET `/api/index/stream`).
- `packages/memtomem/src/memtomem/web/routes/system.py:688` —
  `reindex` (POST `/api/reindex`, all `memory_dirs`).
- `packages/memtomem/src/memtomem/web/routes/system.py:416` —
  `memory_dirs/add` (with `auto_index=true`).
- `packages/memtomem/src/memtomem/web/routes/system.py:911` —
  `upload_files` (POST `/api/upload`).
- `packages/memtomem/src/memtomem/web/routes/system.py:278` —
  `GET /api/privacy/patterns` (introduced by #580; client-side regex
  source-of-truth endpoint, may be reused for bulk-surface UI hints).
