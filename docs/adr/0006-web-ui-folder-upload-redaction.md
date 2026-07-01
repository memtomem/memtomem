# ADR-0006: Web UI folder/upload privacy redaction trust-boundary

**Status:** Accepted (trigger fired 2026-06-11; amended to add Axis F — bundle import; amended 2026-06-30 to add Axis G — export/import path policy)
**Date:** 2026-04-30 (amended 2026-06-11, 2026-06-30)
**Context:** Issue #585 — PR #575 follow-up review surfaced that
`packages/memtomem/src/memtomem/privacy.py: DEFAULT_PATTERNS` is enforced
only on the MCP `mem_add` / `mem_batch_add` paths. The Web UI's
folder-index and upload surfaces accept content raw, bypassing the LTM
trust boundary that CLAUDE.md asserts ("STM-bypass must not be
safety-bypass").

> **Reading note (2026-06-11 amendment).** This ADR layers a 2026-06-11
> amendment over the original 2026-04-30 analysis. The **Background**, the
> axis tables (**A–E**), and the **Implementation outline** describe the
> original state and may cite line numbers or method names that have since
> drifted (e.g. `index_directory()` was never an `IndexEngine` method — the
> real entrypoints are `index_path()` / `index_file()` / `index_path_stream()`,
> all routing through the private `_index_file()`; and `upload_files` has
> since gained a route-layer guard). The
> **Implementation status** section is the authoritative current state. The
> amendment also adds **Axis F** (bundle import) and promotes the ADR to
> Accepted.

## Background

`privacy.py: DEFAULT_PATTERNS` is the LTM project's secret-pattern allowlist
— nine regexes covering API key / password assignments, provider tokens
(`sk-`, `ghp_`, `xox[bps]-`, `github_pat_`), Stripe / Clerk / Svix
(`(sk|pk|rk)_(live|test)_…` and `whsec_…`), npm `npm_`, AWS `AKIA|ASIA`,
JWT (`eyJ…`), and PEM private-key headers. The module docstring records
that this is **secret-class only** by intent — PII-class patterns from STM
do not auto-sync because they would force `force_unsafe=True` on most
legitimate prose.

The existing gate model is `mem_add()` in
`server/tools/memory_crud.py:78-104`:

```python
hits = privacy.scan(content)
if hits:
    if force_unsafe:
        privacy.record("bypassed", "mem_add")
        logger.warning("redaction bypass via force_unsafe=True ...")
        ...
    else:
        privacy.record("blocked", "mem_add")
        raise ToolError("write rejected. Retry with force_unsafe=True ...")
```

`privacy.record(...)` increments process-lifetime in-memory counters
exposed via the `mem_add_redaction_stats` MCP tool (a JSON snapshot of
`privacy.snapshot()`); the `logger.warning(...)` line is the only
persistent breadcrumb today (stderr / log sink, not a database row).

`mem_batch_add` (line 400, with the gate at line 445-465) follows the
same shape — `privacy.record("bypassed"|"blocked", "mem_batch_add")`
plus the same `logger.warning(...)` line, scoped per item-index. Note
that **`mem_edit` and `mem_delete` are unguarded today** (no
`privacy.scan()` call, no `force_unsafe` parameter); that is a related
but separate MCP-path gap and is out of scope for this ADR (which
addresses only the Web UI bulk surfaces). *(2026-06-11 update: this
paragraph reflects the 2026-04-30 state — `mem_edit` has since gained a
`force_unsafe` parameter and an `enforce_write_guard` call at
`server/tools/memory_crud.py:496`; treat "unguarded today" as historical
for `mem_edit`.)*

Compose-mode in the Web UI is covered separately by **#580 (CLOSED)** —
a client-side regex pre-check against `GET /api/privacy/patterns`
(`web/routes/system.py:278`) shows a confirm dialog before submission.
That handles the "user is the typist" case where per-input confirm is
meaningful.

The remaining gap is on bulk surfaces, where per-file confirm is not a
meaningful UX:

| Surface | Endpoint | Handler | Privacy gate (as of 2026-04-30)? |
|---------|----------|---------|---------------------------------|
| Index a registered dir | `POST /api/index` | `trigger_index` (`system.py:835`) | ❌ none |
| Index a registered dir (SSE) | `GET /api/index/stream` | `index_stream` (`system.py:795`) | ❌ none |
| Reindex all `memory_dirs` | `POST /api/reindex` | `system.py:688` | ❌ none |
| Add dir + auto-index | `POST /api/memory-dirs/add` (`auto_index=true`) | `system.py:416` | ❌ none |
| Upload + index | `POST /api/upload` | `upload_files` (`system.py:911`) | ❌ none |
| Compose textarea | client-side, `POST /api/add` | `mem_add` (MCP path) | ✅ via `mem_add` + #580 client warn |

All five bulk surfaces converge into `IndexEngine.index_file()`
(`indexing/engine.py`). At the 2026-04-30 baseline none of them called
`privacy.scan()` before persisting (upload has since gained a route-layer
guard — see "Implementation status"). The unspoken assumption — "Web UI =
local user, the boundary is at MCP" — breaks at the moment a user runs
`mm web` on a non-loopback bind, runs it on a shared workstation, or
indexes a folder that contains a `.env` they didn't realize was there.

## The decisions this ADR settles

The issue body enumerated five axes (A–E). Each is treated below with
options, leaning, and rationale. The 2026-06-11 amendment adds **Axis F —
bundle import** after the deferral trigger fired (see "Trigger record").

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
| C.1 — **same `DEFAULT_PATTERNS`** | nine secret-class regexes, identical to MCP |
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
- *Audit trail* — today MCP bypass produces (a) a counter increment via
  `privacy.record("bypassed", "<tool>")` (snapshot-readable through
  `mem_add_redaction_stats`; existing labels are `mem_add` at
  `memory_crud.py:88` and `mem_batch_add` at `memory_crud.py:463`) and
  (b) a `logger.warning(...)` line at the same sites that names tool /
  namespace / file / content_chars / hits. Bulk bypass should reuse the
  same two surfaces — adding a new ingress-tool label (e.g.
  `index_bulk` or `web_bulk_index`, exact name a PR-A detail) and
  emitting the same warning shape. **Open sub-question for the
  implementation PR**: whether a persistent audit table (chunk-id +
  matched-pattern hash + caller surface) is also warranted, or whether
  counters + structured logs remain enough. This ADR records the
  default as "match MCP's existing trail"; promotion to a real audit
  table is its own decision if the trigger conversation reveals the
  log line is too easy to lose.

E.2 (CLI only) is too narrow — the `mm web` user has no terminal in flow.
E.3 is too blunt — making bypass the persisted default flips the trust
semantics. E.4 (no override) breaks the "intentional debug note about an
old, rotated key" workflow that ADR-0005's force-reindex contract revealed
is real.

### Axis F — Bundle import (added 2026-06-11 amendment)

> Folder-index and upload (axes A–E) were the original scope. This amendment
> adds a sixth surface the ADR never covered: JSON **bundle import**.

Two ingresses share one unguarded code path:

| Surface | Handler | Privacy gate today? |
|---------|---------|---------------------|
| `POST /api/export/import` | `web/routes/export.py:import_memories` → `import_chunks` | ❌ none |
| MCP `mem_import` | `server/tools/export_import.py:mem_import` (`@mcp.tool`) → `import_chunks` | ❌ none |

Both call `tools/export_import.py:import_chunks`, which embeds each record and
calls `storage.upsert_chunks(...)` with **no** `privacy.enforce_write_guard`
call. The bypass is today an explicit but under-documented exemption — it
lives only as a comment in `tests/test_web_invariants_registry.py`
(`export.import_memories`: "import bypass: archived chunks already passed
redaction at original write time and re-scanning would corrupt deterministic
round-trip"). That rationale exists nowhere in an ADR or in production code.

**The flaw in that rationale.** "Archived chunks already passed redaction at
original write time" only holds for bundles **this instance exported**.
`import_chunks` accepts *any* JSON bundle — including hand-crafted or
third-party ones that never crossed our write boundary. The
round-trip-fidelity justification does not cover foreign bundles, which are
exactly the untrusted case the redaction invariant exists for.

| Option | Behavior | Round-trip | Closes invariant gap |
|--------|----------|------------|----------------------|
| F.1 — ratify exemption | import stays unguarded; document the round-trip rationale here | preserved for all bundles | ❌ foreign bundles still bypass |
| F.2 — gate all imports | scan each record's `content`; reject on hit unless `force_unsafe` (mirrors `mem_add` / upload: B.2 + E.1) | a secret-bearing self-export needs explicit `force_unsafe` | ✅ |
| **F.3 — provenance-aware** | exempt bundles carrying a verifiable **local-provenance** marker (this install's own export produced them — see caveat); route all others (absent/invalid marker) through the F.2 gate | preserved for self-exports; foreign bundles gated | ✅ for foreign bundles |

**Decision: F.3.** It keeps deterministic round-trip for the common, trusted
case (a bundle this instance produced) while closing the only case the
invariant actually protects (a bundle of unknown provenance). F.1 is rejected:
it leaves the untrusted case wide open while reading as "we decided this is
fine." Pure F.2 is the safe fallback if a provenance marker proves too heavy
for the bundle format, at the cost of forcing `force_unsafe` on legitimate
self-export round-trips that happen to carry a (rotated, intentional) secret.

**Caveat — what the marker proves.** A bundle-level marker proves *local
provenance* (this install exported it), **not** that every chunk passed
redaction. Legacy pre-guard rows, prior `force_unsafe` writes, and content
from the still-unguarded folder-index path can all sit in the DB and thus
appear in a self-export. F.3 is therefore an explicit **local-provenance
round-trip exemption**: we re-import our own export as-is, trusting the local
user's earlier storage decisions, rather than re-proving redaction on data
that already lives in this install. The stronger alternative — per-chunk
redaction provenance before skipping a scan — is heavier and deferred; if
same-install round-trip of `force_unsafe` / legacy content is judged
unacceptable, fall back to F.2 (gate everything).

**Provenance marker (implementation detail, deferred to the follow-up PR).**
`export_chunks` stamps the bundle with a marker proving **local provenance**
(this install's export produced it) — e.g. an HMAC over the chunk content
keyed by a per-install secret, or a signed `exported_by` + `redaction_version`
header. (Per the caveat above, this attests origin, not per-chunk redaction.) Import verifies it: valid → skip re-scan
(round-trip preserved); absent or invalid → treat as foreign and run the F.2
gate (`enforce_write_guard` per record, `force_unsafe` to override), across
**both** `POST /api/export/import` and MCP `mem_import`. The exact marker
(HMAC vs. signature vs. content-hash manifest) is a follow-up-PR decision;
this ADR fixes the *policy* (verify-or-gate), not the mechanism.

### Axis G — Export/import filesystem path authority (added 2026-06-30 amendment)

> Axes A–F govern *what content* may cross the write boundary (redaction). This
> amendment adds the orthogonal *path* question for the bundle export/import
> MCP tools: **which filesystem locations** `mem_export` may write to and
> `mem_import` may read from. Context: #1486 (security-audit follow-up).

**Current state (verified against source).**

- `mem_export(output_file=…)` writes the bundle to *any* resolved path —
  `server/tools/export_import.py:60`
  (`Path(output_file).expanduser().resolve()`, no allow-list).
- `mem_import(input_file=…)` reads *any* existing path —
  `server/tools/export_import.py:129-131` (existence-checked only).
- This is **local MCP tool authority**, not a traversal bug. The indexing read
  surfaces are, by contrast, root-bounded: `IndexEngine` rejects paths outside
  the configured `memory_dirs` / `project_memory_dirs` (`indexing/engine.py:719`
  `_is_within_memory_dirs`, enforced at `:446` / `:523`), with non-disableable
  secret excludes (`engine.py:50-56` `_BUILTIN_SECRET_PATTERNS`; dir-level
  `.aws` / `.ssh` / `.gnupg` at `:1220-1222`), and `mem_add(file=)` validates
  its path against the same bases (`server/tools/memory_crud.py:42-72`).
- The **import** ingress trust boundary is the Axis F.3 provenance-aware
  redaction gate (`tools/export_import.py:244` `enforce_write_guard` →
  `ImportPrivacyError`), which is *path-independent*: a foreign bundle is
  scanned and rejected on a secret hit regardless of where on disk it was read
  from.
- The **web** transport never accepts a server-side path: `import_memories`
  takes an `UploadFile` into a `tempfile` (`web/routes/export.py:60-109`) and
  `export_memories` returns a download — neither exposes an operator-named
  filesystem path. Axis G is therefore an **MCP-tool-only** question.

| Option | Behavior | Verdict |
|--------|----------|---------|
| **G.1 — leave unrestricted, document** | both tools keep arbitrary resolved paths; record the intentional read/write asymmetry, the accepted residual, and a reopen trigger here | ✅ **chosen** |
| G.2 — constrain to safe roots | restrict `output_file` / `input_file` to the export dir / `memory_dirs` | ❌ rejected |
| G.3 — warn / confirm / audit out-of-root | emit an advisory + audit-log entry when the target falls outside `all_index_roots()` | ❌ rejected |

**Decision: G.1.** The trust boundary for import ingress is the F.3 redaction
gate, not the filesystem path; import poisoning is path-independent and is
already gated, and the web transport never accepts a server-side path. The
read-side asymmetry with the root-bounded `mem_add(file=)` / `mem_index` is
**intentional**: backups are deliberately written *outside* the indexed tree
(you do not want a backup re-indexed), so `memory_dirs` is the wrong allowlist
for an export target — which is exactly why **G.2 is rejected** (it would break
the documented `~/backup.json` workflow). **G.3 is rejected** for the same
boundary reason: the canonical `~/backup.json` lives in `$HOME`, outside
`all_index_roots()`, so the advisory would fire on *every legitimate backup* —
alert fatigue that defeats its own "observable, not silent" purpose, and it
leans on the very `memory_dirs` boundary G.2 rejected. Import path policy: no
change (existence-check only).

**Accepted residual — core-mode export-write confused-deputy.** In the
most-locked-down deployment (an agent wired to *only* memtomem, core-9 tools, no
shell or generic file tool), `mem_export` is the agent's *only*
arbitrary-destination write. Under prompt injection that is a real
confused-deputy exfil / DoS channel: write the memory bundle to a
server-readable path, or clobber a dotfile. It is reachable over plain local
`stdio` in *every* deployment, and — since unauthenticated network transports
already exist as an opt-in (`--transport sse|http`, no first-party auth; see
ADR-0029 / #1485) — also reachable over the network whenever an operator opts
into one and widens `Host` / `Origin` or disables DNS-rebinding protection. No
network-auth control gates it. We **accept this as a documented residual**
rather than ship a pre-write control now: the no-shell core deployment is the
minority configuration, and a real fix is closer to the rejected G.2
`export_dir` work than a trivial guard. If the risk appetite shifts, the
follow-up is a *true* pre-write control — an opt-in, default-off `export_dir`
allowlist with a wide default preserving `~/backup.json`, or a
suspicious-destination pre-write gate — **not** after-the-fact audit.

**Reopen trigger (mirrors the ADR-0006 `--allow-remote-ui` precedent and
ADR-0029).** If memtomem ever blesses unauthenticated non-local MCP exposure as
a *supported / default* posture — beyond today's opt-in trusted-LAN /
authenticated-reverse-proxy stance (ADR-0029, #1485) — the unrestricted
export-write / import-read blast radius widens and Axis G reopens toward the G.2
`export_dir` control. The channel is already network-reachable today under the
operator misconfig ADR-0029 documents, and reachable over local `stdio`
regardless, so the accept-as-residual call stands on its own merits,
independent of ADR-0029's outcome.

## Decision

**Accepted (2026-06-11).** Axes **A.3 + B.2 + C.1 + D.2 + E.1** (bulk
index/upload) stand as the decision; implementation is **partial** — the
upload surface is guarded, the folder-index surfaces are not yet (see
"Implementation status"). Adds **Axis F → F.3** (provenance-aware import gate),
**built 2026-06-30 in #1490**. Originally deferred; promoted on the public `--allow-remote-ui`
trigger (see "Trigger record"). The 2026-06-30 amendment adds **Axis G → G.1**
— the MCP `mem_export` / `mem_import` filesystem paths stay unrestricted
(local-tool authority), documented rather than constrained, with an accepted
core-mode export-write residual and a reopen trigger (no code or behavior
change).

### Why this was originally held (historical)

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

### Trigger record (2026-06-11)

The public trigger #3 is met, so the ADR moves from deferred to Accepted:

- **#3 — `mm web` non-loopback bind documented (public).** `mm web` now ships
  `--allow-remote-ui` with `_validate_bind` at `cli/web.py:228-244`
  (RFC #787). The "Web UI = local user" assumption no longer holds
  universally — once a user opts into `--allow-remote-ui`, the boundary must
  move with it, exactly as this criterion anticipated. This public flag is
  sufficient on its own to promote; the amendment relies on it alone.

### Implementation status (2026-06-11)

Axes A–E are decided but only **partially built**:

- **Upload — guarded.** `web/routes/system.py` `upload_files` calls
  `privacy.enforce_write_guard(..., surface="web_api_upload")` (`:1411`); the
  Compose/add route guards similarly (`surface="web_api_add"`, `:1526`). These
  are route-layer guards, **not** the engine-layer
  `IndexEngine._index_file(force_unsafe=…)` + `PrivacyRejection` seam the
  "Implementation outline" describes.
- **Folder-index — guarded (PR-A, 2026-07-01).** The single-chokepoint design
  from A.3 / B.2 is now built: `IndexEngine._index_file()` resolves scope, then
  calls `privacy.enforce_write_guard(content, surface="index", scope=…,
  force_unsafe=…)` and raises `PrivacyRejection` on a hit without `force_unsafe`.
  Bulk entrypoints (`index_path` / `index_path_stream` — `trigger_index`,
  `reindex_all`, `memory-dirs/add` auto_index, `index_stream`, `mm reindex`,
  `mem_index`, the watcher, the `mm index --debounce-window` / `--flush`
  hook-drain path) catch it per file and aggregate into
  `IndexingStats.blocked_files` / `blocked_paths`, so one flagged file does not
  abort the run; single-file `index_file` lets it propagate so callers roll back
  or surface it rather than silently succeeding. Callers that already ran
  `enforce_write_guard` at their ingress layer pass `already_scanned=True` to
  skip the gate (see the outline note below). `mm index --force-unsafe` ships the
  bulk escape hatch.
- **Import (Axis F.3) — built (#1490).** Both ingresses — MCP `mem_import` and
  web `POST /api/export/import` — route through `tools/export_import.py`
  `import_chunks`, which verifies the local-provenance marker and, for an
  absent/invalid marker (a foreign bundle), runs `enforce_write_guard` per
  record (`:244`) and rejects the whole import on a secret hit (`:257`) unless
  `force_unsafe`. Self-exports skip the scan, preserving round-trip.

So the B.2 reject behavior is live for upload, import, **and folder-index**
(PR-A). The decision (A.3 + B.2 + C.1 + D.2 + E.1 + F.3) stands. **PR-B — the Web
UI override toggle — shipped 2026-07-01:** the Index-tab folder mode and the
Sources "+ Add path" flow now expose an "Index without privacy gate"
(`force_unsafe`) checkbox. Because bypassing the redaction gate is a security
*downgrade*, the bypass rides only CSRF-protected `POST`s — the Index folder
mode through `POST /api/index` (`trigger_index`), Sources through
`POST /api/memory-dirs/add` — **not** the token-exempt `GET /api/index/stream`
SSE surface (a safe method the CSRF middleware does not token-gate, and
`EventSource` cannot send the token header). A review flagged that threading
`force_unsafe` onto the GET stream would let a CSRF-exempt safe method disable
the safety boundary, so the folder bypass runs as a one-shot POST (a spinner,
not a per-file progress bar). Every bulk-index surface now *displays* the PR-A
`blocked_files` counts (result row + toast) — which PR-A added to the responses
but no frontend surface rendered, so a secret-bearing file dropped with a green
"success". `project_shared` blocks are messaged as non-bypassable (ADR-0011 §5),
mirroring the `mm index` CLI. The Axis E.1 **audit-surface panel** (a GUI view of
`privacy.snapshot()`) is **deferred to a follow-up PR** — no such panel exists in
the web UI (the counters are MCP-only via `mem_add_redaction_stats`), so building
it is its own focused change (a new `GET /api/privacy/stats` endpoint + a
Settings section).

## Implementation outline (when triggered)

In rough order, all in `packages/memtomem/src/memtomem/`:

- **PR-A — Engine gate + route wiring.**
  - Enforce at the private `IndexEngine._index_file()` (the per-file method
    that `index_path()`, `index_file()`, and `index_path_stream()` all funnel
    through), threading `force_unsafe` from those public entrypoints in
    `indexing/engine.py`. On entry, read file
    content and call `privacy.scan(content)`; on hit without
    `force_unsafe`, raise a typed `PrivacyRejection` (carrying file path
    + matched pattern indices) and abort that file's index.
  - Wire callers: `web/routes/system.py:trigger_index()` (835),
    `index_stream()` (795), `reindex` (688), `memory_dirs/add` with
    `auto_index=true` (416), `upload_files()` (911). Each handler
    catches `PrivacyRejection` and converts to the appropriate response
    shape (HTTPException for one-shot; SSE error event for stream).
  - Reuse `mem_add`'s bypass trail from `server/tools/memory_crud.py` —
    `privacy.record("bypassed", "<tool>")` for the in-memory counter
    and the `logger.warning("redaction bypass via force_unsafe=True ...")`
    shape — so MCP and bulk bypass land in the same `mem_add_redaction_stats`
    snapshot and the same log sink. (Whether to add a persistent audit
    table is the open sub-question called out in axis E.)
  - **Built as (2026-07-01), with deviations from the outline above** — a Codex
    design gate surfaced that the literal outline breaks existing contracts:
    - The gate calls `privacy.enforce_write_guard` (which records + audits) rather
      than raw `privacy.scan`, so bulk bypasses land in the `mem_add_redaction_stats`
      snapshot per the axis-E intent above — no separate `record(...)` call needed.
    - **Ingress-guarded callers skip the gate** (`_index_file(..., already_scanned=True)`)
      instead of threading `force_unsafe` through them. `mem_edit` / web chunk
      edit+delete re-index the *whole file* after a line-range mutation and rely on
      `index_file()` **raising** to roll back or delete stale chunks; a gate that
      re-scanned the whole file would (a) re-block content already adjudicated
      elsewhere in the file, (b) double-count/-audit, and (c) on `mem_delete`
      (which has no `force_unsafe` param) block a legitimate delete. Skipping is
      correct: the boundary is already enforced at their ingress `enforce_write_guard`.
      The guarded set: `mem_add` / `mem_edit` / `mem_batch_add` / `mem_delete`,
      `upload_files`, compose/`add_memory`, web chunk edit/delete, CLI `mm mem add`,
      CLI agent `share`, and LangGraph `add`.
    - **Blocked files are a return-path aggregate for bulk, a raised exception for
      single-file.** `PrivacyRejection` (path + hit count, never the matched bytes)
      is raised in `_index_file`; `index_path` / `index_path_stream` catch it per
      file and aggregate into `IndexingStats.blocked_files` / `blocked_paths`
      (surfaced in `IndexResponse`, the SSE `complete` event, `reindex_all` /
      `memory-dirs/add` JSON, `mem_index`, and the `mm index` summary); un-adjudicated
      single-file callers (`mem_fetch`, `mem_import_*`, session summary, `mm index <file>`,
      the watcher) catch it and surface an error instead of reporting false success.
      A `blocked_project_shared_files` counter travels alongside `blocked_files`
      so surfaces give scope-correct guidance — a `project_shared` block is
      hard-refused even with `force_unsafe`, so the CLI tells the user to move
      the file to `user`/`project_local` or remove the secret rather than to
      retry with `--force-unsafe`.
    - **PR-C's `mm index --force-unsafe` shipped with PR-A** (not deferred) so a
      false positive on bulk index has an escape hatch in the same release.
    - **Gap found and closed same-day: the debounce/flush drain path
      discarded `IndexingStats` entirely.** `cli/indexing.py`'s
      `_make_indexer` (predates PR-A, from `#548`) called `index_path` and
      never inspected the return value, so a `blocked_files` hit was reported
      to the hook caller as a plain `Indexed` success and the queue entry was
      deleted with no retry — silently defeating the "reject is observable"
      rationale Axis B leans on for bulk surfaces reached via the hook path.
      Fixed by raising when `stats.blocked_files` is nonzero so
      `debounce.drain_ready` / `drain_all`'s existing `except Exception` retry
      path keeps the entry for the next drain. The raise is **scoped to the
      redaction block, not to `stats.errors` generally**: `stats.errors` also
      carries terminal non-security skips (`file too large`, `binary file
      detected`) that can never succeed on retry, so raising on them would pin
      the file in the queue forever, re-erroring on every drain — a livelock
      the pre-fix silent-drop correctly avoided. A blocked file self-clears
      once the secret is removed; a too-large/binary file never would.
      Surfacing transient (embedding-backend) failures on the debounce path is
      a separate, deliberate call, left as-is. The LangGraph integration's
      `index()` tool had the identical reporting gap (no return value at all
      for `blocked_files` / `blocked_paths` / `errors`) and was fixed the
      same way.
    - **Known, lower-severity partial gap — tracked, not fixed in this
      pass.** `cli/shell.py`'s interactive `index` command and
      `indexing/watcher.py`'s startup backfill both log `stats.blocked_files`
      as a count, but neither logs `blocked_paths` (which files) nor
      inspects generic `stats.errors` (non-redaction per-file failures).
      Some signal already exists here — unlike the debounce/flush and
      LangGraph gaps above — so this is deferred alongside PR-B rather than
      bundled into this fix.
- **PR-B — Web UI override toggle (shipped 2026-07-01) + audit surface
  (deferred).**
  - **Toggle — built.** An "Index without privacy gate" checkbox on the Index
    tab (folder mode) and the Sources `+ 경로 추가` row threads `force_unsafe`
    into PR-A's `IndexEngine` gate. The bypass is a security *downgrade*, so it
    rides only CSRF-protected `POST`s — `POST /api/index` (`trigger_index`) for
    the Index folder mode and `POST /api/memory-dirs/add` for Sources — **not**
    the `GET /api/index/stream` SSE surface, which the CSRF middleware leaves
    token-exempt (safe method) and which `EventSource` can't add a token header
    to. A review flagged the GET-stream bypass as a CSRF hole; routing through
    the POST closes it, at the cost of per-file streaming for a bypass run (a
    one-shot spinner). Raw-body `force_unsafe` is parsed strictly (only a JSON
    literal `true`), so a string `"false"` cannot silently flip the bypass on.
    Paired with **blocked-file surfacing** (a result row + toast) across the
    folder-index, per-dir reindex, reindex-all, and add flows — PR-A put
    `blocked_files` / `blocked_paths` / `blocked_project_shared_files` on the
    responses, but no surface displayed them, so the toggle had no visible
    trigger. `project_shared` blocks are messaged as non-bypassable (they stay
    hard-refused even with `force_unsafe`), matching the `mm index` CLI guidance.
  - **Audit surface — deferred.** Surfacing the bypass trail as a GUI view of
    `privacy.snapshot()` was written into this outline as "extend the existing
    redaction-stats panel," but no such panel exists in the web UI — the counters
    are MCP-only (`mem_add_redaction_stats`). Building it needs a new
    `GET /api/privacy/stats` endpoint + a Settings section, so it is split into
    its own follow-up PR. The persistent-audit-table sub-question on axis E
    remains a separable decision on top of that.
- **PR-C — CLI parity. Shipped with PR-A (2026-07-01).**
  - `mm index --force-unsafe` reuses PR-A's `IndexEngine` `force_unsafe`
    parameter (threaded through `cli/_index_progress.run_with_progress`), and the
    `mm index` summary reports the blocked count + paths. Shipped together with
    PR-A rather than held, so a false positive on bulk index has a documented
    recourse in the same release.

## Consequences

- **New rejection mode for bulk surfaces.** Users indexing a folder that
  contains a real or look-alike secret will see an error toast / SSE
  event instead of the chunk silently appearing. This is the intended
  behavior; it should be telegraphed in the next minor's CHANGELOG as a
  behavior change.
- **Bypass observability extends.** `privacy.snapshot()` (surfaced
  through `mem_add_redaction_stats`) gains bulk-surface counter labels;
  log volume picks up one `logger.warning` line per bulk bypass at the
  same rate as MCP bypass. Both signals are process-lifetime / log-sink
  scoped, not persistent rows — promotion to a real audit table is the
  open sub-question in axis E and would carry its own storage
  implications (eviction policy etc.) only if taken.
- **`IndexEngine` API *will* gain a parameter (folder-index work, pending).**
  The outline's `force_unsafe` keyword belongs on the private
  `IndexEngine._index_file()` (the per-file method that `index_path` /
  `index_file` / `index_path_stream` all funnel through) and is **not yet
  built** — upload guards at the route layer instead (see "Implementation
  status"). When the folder-index gate lands,
  external callers (the engine is part of the public Python API) get the new
  keyword; default `False` preserves existing behavior.
- **Cross-repo sync invariant gets a hook.** STM's secret-class pattern
  additions now have a documented reason to ramp the LTM gap-close in
  the same release window — the asymmetric-sync rule in CLAUDE.md
  becomes an active sync trigger rather than a static comment.
- **Compose / bulk asymmetry resolved.** Today Compose warns (client),
  MCP rejects, bulk passes. After this ADR's implementation: Compose
  warns (client) + rejects (server, via `mem_add`), bulk rejects, MCP
  rejects. The boundary is uniform.
- **Bundle import gains a trust check (Axis F → F.3).** Self-exported bundles
  with a valid provenance marker import unchanged (round-trip preserved);
  bundles of unknown provenance are scanned per-record and rejected on a
  secret hit unless `force_unsafe` is passed, across both
  `POST /api/export/import` and MCP `mem_import`. The
  `tests/test_web_invariants_registry.py` exemption for
  `export.import_memories` narrows to the provenance-verified path. Importing a
  foreign bundle that contains a secret is a behavior change — telegraph it in
  the next minor's CHANGELOG.
  - *Scan surface (follow-up-PR refinement).* Import is the one write surface
    where **every** field — including metadata — arrives verbatim from an
    untrusted bundle and is then embedded (`retrieval_content` = heading +
    content), stored, and retrievable. So the foreign-bundle scan covers the
    full retrievable surface (`content` + `heading_hierarchy` + `source_file` +
    `tags`), a deliberate widening beyond the `content`-only scan that the
    locally-derived-metadata surfaces (`mem_add` / `mem_batch_add`) use. This is
    field coverage, not a new pattern set (Axis C's `DEFAULT_PATTERNS` is
    unchanged), and it never touches self-exports (their scan is skipped), so
    round-trip fidelity is unaffected.
- **Export/import filesystem paths stay unrestricted (Axis G → G.1).** The MCP
  `mem_export` / `mem_import` tools keep arbitrary resolved paths by design
  (local-tool authority); the import trust boundary remains the F.3 redaction
  gate, not the path. The read/write asymmetry with the root-bounded
  `mem_add(file=)` / `mem_index` is intentional (backups live outside the
  indexed tree) and is now documented — in the ADR and in a docstring + call-site
  note on both tools — so a future reviewer does not "fix" it as a bug. The
  core-mode export-write confused-deputy residual is accepted and carries a
  reopen trigger (see Axis G). No code or behavior change.

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

> *Line numbers in the original entries below reflect the 2026-04-30 draft and
> may have drifted; the "Added by the 2026-06-11 amendment" subsection uses
> current references.*

- Issue #585 — ADR placeholder, this document is the deliverable.
- Issue #580 (CLOSED) — Compose-mode client-side warning. Sibling, not
  superseded.
- ADR-0004 — same "deferred pending trigger" shape this ADR mirrors.
- CLAUDE.md (project root) — "STM-bypass must not be safety-bypass" trust
  boundary; `privacy.py` asymmetric-sync rule.
- `packages/memtomem/src/memtomem/privacy.py:42-57` — `DEFAULT_PATTERNS`
  (nine secret-class regexes).
- `packages/memtomem/src/memtomem/privacy.py:268` — `scan()` entry point.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:78-104` —
  existing gate model on `mem_add`.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:445-465` —
  same model on `mem_batch_add`. (As of 2026-06-11, `mem_edit` is also guarded
  — `enforce_write_guard` at `memory_crud.py:496`; `mem_delete` writes no
  content, so it needs no redaction gate.)
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

### Added by the 2026-06-11 amendment (Axis F)

- Axis F (bundle import) — as of the 2026-06-11 amendment both ingresses
  reached `import_chunks` with no `enforce_write_guard` call; Axis F → F.3
  closed that gap (built in #1490). The sources below are where it was
  verifiable.
- `packages/memtomem/tests/test_web_invariants_registry.py` — the
  `export.import_memories` exemption being narrowed by Axis F → F.3.
- `packages/memtomem/src/memtomem/web/routes/export.py` — `import_memories`
  (`POST /api/export/import`).
- `packages/memtomem/src/memtomem/server/tools/export_import.py` — MCP
  `mem_import`, the second ingress to the shared `import_chunks` path.
- `packages/memtomem/src/memtomem/tools/export_import.py` — `import_chunks`
  (embed + `upsert_chunks`; the F.3 redaction gate now lives here —
  `enforce_write_guard` at `:244`, atomic reject at `:257`) and `export_chunks`
  (the marker-stamping site for F.3).
- `packages/memtomem/src/memtomem/cli/web.py:228-244` — `_validate_bind` /
  `--allow-remote-ui` (RFC #787), the bind flag that fired trigger #3.
- ADR-0012 §"Closest existing prior art: bundle v2" — covers *what fields* a
  bundle serializes; orthogonal to Axis F (redaction *on ingest*). Cross-ref.

### Added by the 2026-06-30 amendment (Axis G)

- `packages/memtomem/src/memtomem/server/tools/export_import.py:60` —
  `mem_export` resolved `output_file` (no allow-list); `:129-131` —
  `mem_import` resolved `input_file` (existence-checked only). Both gained a
  docstring + call-site note recording the intentional asymmetry.
- `packages/memtomem/src/memtomem/tools/export_import.py:244` —
  `enforce_write_guard` (the Axis F.3 import redaction gate; the
  path-independent ingress trust boundary).
- `packages/memtomem/src/memtomem/indexing/engine.py:719`
  (`_is_within_memory_dirs`, enforced `:446` / `:523`), `:50-56`
  (`_BUILTIN_SECRET_PATTERNS`), `:1220-1222` (`_EXCLUDED_DIRS`
  `.aws` / `.ssh` / `.gnupg`) — the root-bounded read surfaces Axis G is
  asymmetric with.
- `packages/memtomem/src/memtomem/server/tools/memory_crud.py:42-72` —
  `mem_add(file=)` path validator (same root-binding).
- `packages/memtomem/src/memtomem/web/routes/export.py:60-109` —
  `import_memories` (`UploadFile` → `tempfile` → `import_chunks`); the web
  transport accepts no server-side path.
- ADR-0029 / issue #1485 — the network MCP transport stance (no first-party
  auth; opt-in trusted-LAN / authenticated-proxy) that bounds Axis G's network
  reachability and is named in the reopen trigger.
- Issue #1486 — the security-audit decision this amendment records.
