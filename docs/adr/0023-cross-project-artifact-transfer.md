# ADR-0023: Cross-project artifact transfer engine (move|copy)

**Status:** Accepted
**Date:** 2026-06-12
**Context:** Context Gateway completion campaign, mechanism track #1270
item A-2 (#1273). ADR-0011 PR-E4 shipped a single-project scope move
(`mm context migrate <kind> <name> --to <tier>`), and ADR-0021 shipped
cross-project *reads* (discovery + portal). What the campaign's owner
request identified as missing is the general write-side primitive:
selectively moving or copying **one** canonical artifact between tiers
**and between projects**. `context/migrate.py` hardcoded a single
`project_root`, supported move only, and had no cross-project path.
This ADR pins the engine that fills that gap:
`memtomem.context.transfer.transfer_artifact`.

## Decision

### 1. One engine, one entry point, surfaces stay thin

`context/transfer.py` exposes exactly one orchestration entry point:

```
transfer_artifact(kind, name, *, src_project_root, from_scope,
                  dst_project_root, to_scope, mode: "move"|"copy",
                  apply_, surface, new_name=None) -> TransferResult
```

The staged-move primitives stay in `context/migrate.py` and are reused
verbatim — `_acquire_pair_lock`, `_stage_move`, `_promote_move`,
`_existing_fanout_targets`, `_remove_runtime_fanout_for` — so there is
exactly one implementation of staging, locking, and fan-out cleanup in
the tree. `migrate_scope` remains as a thin same-root wrapper with
byte-compatible results and error messages; every existing surface
(CLI `mm context migrate`, MCP `mem_context_artifact_migrate`, web)
keeps its contract without modification.

Planned consumers, each its own campaign item: CLI `mm context copy` /
`mm context move` (A-3 #1274), web `POST
/api/context/{kind}/{name}/transfer` (A-5 #1276), MCP
`mem_context_artifact_transfer` (A-13 #1283). The engine raises
`click.ClickException` / `MigratePartialError`; each surface translates
to its native error shape (the established `PrivacyScanError` pattern).

### 2. Support matrix

| artifact × capability | tier↔tier move (same project) | copy (same project) | move (project A → B) | copy (project A → B) | copy `--as` rename |
|---|---|---|---|---|---|
| agents | yes (PR-E4, now via engine) | yes | yes | yes | yes |
| commands | yes (PR-E4, now via engine) | yes | yes | yes | yes |
| skills | yes (dir tree) | yes | yes | yes | yes |
| settings hooks | `settings-migrate` path, NOT this engine | — | no (copy-only v1, #1281 scope) | yes — `settings-copy` per-hook (A-11 #1281, separate mechanism: §11) | — |
| mcp-servers | n/a (single-tier by design, ADR-0016 §3 note) | — | A-12 #1282 (separate mechanism) | A-12 #1282 | — |
| memory | ADR-0012 (cross-DB migration, deferred) | — | — | — | — |

Rejected pairings (hard `ClickException`, both modes):

- **same `(project, tier)`** — source and destination resolve to the
  same canonical store; a rename/duplicate verb is explicitly out of
  scope (#1270 non-goals).
- **cross-project `user → user`** — the user tier is global
  (`~/.memtomem/<kind>/`), so "project A's user tier" and "project B's
  user tier" are the same directory; there is nothing to move or copy.
  Both rules collapse to one check: `canonical_artifact_dir(kind,
  from_scope, src_root) == canonical_artifact_dir(kind, to_scope,
  dst_root)`.
- **rename outside copy mode** — `new_name` with `mode="move"` is
  refused; move preserves identity.

### 3. Bounded two-roots exception to ADR-0016 §5

ADR-0016 §5 pins "writes land in **exactly one tier per invocation**"
with a single project-root resolution per write. A cross-project move
necessarily touches two project roots in one invocation — the source
root (delete half) and the destination root (create half). This ADR
grants a **bounded** exception:

- exactly **one artifact** per invocation (no bulk; bulk cross-project
  sync is ADR-0025 / A-9 #1279's question, which will also own the
  ADR-0021 single-project-mutation supersession);
- exactly **two** roots, each playing a fixed role (source / destination);
  tier resolution per root stays ADR-0016 §5-conformant — `from_scope`
  binds to the source root, `to_scope` to the destination root, and
  neither resolution consults config defaults;
- the destination write itself still lands in exactly one tier.

Everything else in ADR-0016 §5 (no config-field default for the write
tier, project-root resolution rules per surface) is unchanged. Web/CLI
surfaces resolve the two roots via the shared selector from A-1
(`resolve_project_selector`, `mm context projects`).

### 4. Two-root fan-out cleanup contract (move)

`_remove_runtime_fanout_for`'s single `project_root` parameter used to
drive two different jobs that only coincide in a same-root move
(Codex design-gate finding for this campaign):

1. **stale fan-out discovery** — which runtime files
   (`<root>/.claude/agents/foo.md`, `~/.gemini/commands/foo.toml`, …)
   the *source* tier had materialized; anchored at the **source**
   project root.
2. **expected-render / override verification** (#1247 id 6) — what
   sync *would* write for this artifact, compared byte-for-byte before
   deleting; per-vendor overrides live inside the artifact dir
   (`<name>/overrides/<vendor>.<ext>`) and **travel with the
   artifact**, so post-move they resolve under the **destination**
   project root.

The signature now takes `src_project_root` (discovery) and
`dst_project_root` (verification) separately. A same-root move passes
the same path twice — byte-for-byte the historical behavior. The
regression pin is a `project_shared → project_shared` move across two
roots carrying a claude override: discovery must clean the *source*
project's runtime file, and the override must verify (no spurious
`.bak`) by resolving at the *destination* root.

Cleanup semantics are otherwise inherited unchanged from PR-E4 +
#1247 id 6: best-effort, **outside** the pair lock, byte-verified
deletes, `.bak` divergence snapshots, symlinks and generator-less
(kind, runtime) pairs left in place.

Move does **not** generate destination fan-out, and copy performs no
fan-out work at all (source is untouched; destination is new). Instead
`TransferResult` carries `needs_sync` plus the exact follow-up command
(`mm context sync --scope <tier>`, `cd`-prefixed for a project-tier
destination until A-9 lands a `--project` selector). `project_local`
destinations set `needs_sync=False` (no runtime fan-out by design,
ADR-0011 §3). Rationale: sync is the single writer of runtime trees;
an inline fan-out here would duplicate `_sync_atomic` Phase 2 and
inevitably drift from it.

### 5. Gate A / Gate B contract

**Gate A (secret scan) runs against the staged bytes iff the
destination tier is `project_shared` — in either project.** The scan
(`scan_artifact_tree`) walks the entire staged artifact: manifest,
`overrides/`, and frozen `versions/vN.md` snapshots included, so a
secret in an old version snapshot blocks a shared landing and the
error names the offending file (re-anchored from the transient staging
path onto the source artifact the user can actually edit). There is no
force valve for `project_shared` (ADR-0011 §5: git history is
forever); `user` / `project_local` destinations are not scanned at
transfer time, same as PR-E4. A Gate A block leaves **zero residue at
the destination**: move rolls staging back to the source ("staging is
deleted only when the bytes are verified safe elsewhere" — the PR-E4
rollback ladder is reused verbatim); copy just deletes staging (the
source was never consumed, by construction).

The audit `surface=` string is caller-supplied per surface
(`cli_context_transfer` default; A-3/A-5/A-13 pass their own), and the
scan's audit context carries the **destination** project root — that
is where the bytes land.

**Gate B (project-shared write confirmation) stays at the surface
layer.** The engine never prompts. CLI verbs own
`--confirm-project-shared`; the web route inherits the
disclose-then-confirm host-write helper shared by A-5/A-6. This is the
same layering migrate established (`migrate_scope` is prompt-free; the
CLI wrapper gates).

### 6. Collision policy

A destination collision (`dst_path` exists, checked pre-flight AND
re-checked inside the lock window) is a **hard fail with a remediation
hint** — `--force` does not overwrite transfer targets, in deliberate
parity with PR-E4 Row 15 (recorded in ADR-0016 §6's 2026-06 note: a
`replace`-style verb is the named follow-up, and #1270 lists
`replace`/`--force` overwrite verbs as campaign non-goals). The
engine's collision identity is the destination **path**
(`<dst_store>/<dst_name>`); `--as <new-name>` is the supported way to
land a copy next to an existing same-name artifact.

### 7. Copy mode and `--as` rename

Copy stages by **byte copy** (`_stage_copy`: `copytree(symlinks=True)`
/ `copy2(follow_symlinks=False)` — the same no-deref contract as the
EXDEV fallback), so the source is never consumed or mutated and
rollback is trivially safe. `versions/` + `versions.json` live inside
the artifact dir and travel implicitly in both modes (move and copy);
version history is content, not provenance.

`--as <new-name>` (copy only) re-validates the name and performs the
engine's **one deliberate content mutation**: the staged manifest's
frontmatter `name:` line is rewritten to the new name, before Gate A
scans the staged bytes. This is load-bearing, not cosmetic — sync fans
out under the *parsed* name (`_sync_atomic` keys on
`adapter.name_of`; dir/stem is only the omitted-`name` fallback), so a
renamed copy keeping `name: <old>` would fan out at the destination
under the old name and collide with whatever owns it there — exactly
the class of collision §6 hard-fails on. Boundaries of the rewrite:

- no frontmatter / no `name:` key → no-op (fallback already yields the
  new name);
- multiple `name:` keys → refuse loudly (the flat-YAML parser keeps
  the last; a partial rewrite could silently lose);
- detection tolerance matches the parser's, byte fidelity does not
  (Codex review fold): the canonical parsers strip one leading BOM and
  normalize CRLF before matching (#1229), so a BOM/CRLF manifest must
  not silently skip the rewrite — but the rewrite itself preserves the
  BOM and every line's original ending verbatim (bytes in, bytes out;
  no universal-newline translation);
- `versions/vN.md` snapshots are **not** rewritten (frozen history,
  ADR-0022) — restoring a pre-rename version resurrects the old name,
  which is versioning semantics, not a transfer bug;
- `overrides/<vendor>.*` are **not** rewritten (verbatim-by-contract);
  the result carries a `notes` entry telling the user to review them.

### 8. Lock ordering and `lock.json` bookkeeping

Both sidecar locks — source artifact and destination artifact, one
under each project root — are held **simultaneously**, acquired in
`sorted(key=str)` order over the absolute lock paths
(`_acquire_pair_lock`, unchanged). String sort over absolute paths is
a total order across any pair of roots, so every process system-wide
acquires in one global sequence and the PR-E4 deadlock-freedom
argument carries over to the cross-project case unchanged.

`lock.json` (wiki install provenance) bookkeeping stays **outside**
the artifact pair lock — `Lockfile` serializes on its own sidecar, and
a bookkeeping failure must never fail or roll back a committed move:

- **move out of `project_shared`** → drop the entry from the
  **source** project's `lock.json` (best-effort, loud warning on
  failure) — the #1123 B4-1 dangling-entry rule, now root-qualified;
- **copy** → the source's `lock.json` is never touched; the
  destination may gain an entry via the §9 carry-over.

### 9. Provenance carry-over (A-4 #1275)

A `project_shared → project_shared` transfer (move AND copy) of a
wiki-installed artifact carries the source's `lock.json` entry to the
destination so `mm context status` / `update` keep working there.
The design-gate finding from A-2 stands as the gating rule — carrying
a pin over locally-edited bytes would bless the edits as installed
state, letting a later `mm context update` clobber them without its
`--force` gate — so the carry is double-gated:

1. **Pre-stage (source still on disk):** the entry must have a full
   40-char SHA `wiki_commit` (the ADR-0008 stored-pin contract), a
   valid per-file digest map (`digests_from_entry`; pre-#1247 mtime
   entries never carry — mtime cleanliness is not byte evidence), and
   classify `clean` under `is_asset_dirty`.
2. **Post-promote (after the pair lock releases):** the promoted
   destination tree is rehashed and must equal the source entry's
   digest map exactly. Sidecar locks don't bind external writers, so
   this equality gate — not the pre-stage check — is what closes the
   classify→stage TOCTOU: the only digests ever written at the
   destination are byte-identical to what the wiki install recorded.

A clean carry upserts the destination entry with the carried
`wiki_commit`, `files` derived from the rehashed map, and a fresh
`installed_at`/`digests_installed_at` pair captured from the promoted
tree (the `lockfile.upsert_entry` paired-key contract requires
recompute, not verbatim copy). A copy-rename (`--as`) never carries:
entries are keyed by wiki asset name, so an entry under the new name
would point `update` at a different wiki asset. Failed or declined
carries land the artifact untracked, with a human reason plus a
stable `_skip_reasons.ProvenanceSkipCode` on the result
(`provenance` / `provenance_reason` / `provenance_reason_code`).

Bookkeeping order on a move: destination upsert first, then the
source entry drop of §8 (unconditional — even when the carry
declined, the source canonical is gone and a dangling entry is status
noise). All of it stays best-effort and outside the pair lock: a
corrupt destination lockfile warns loudly but never un-commits the
transfer.

### 10. Web route surface (A-5 #1276)

`POST /api/context/{kind}/{name}/transfer`
(`web/routes/context_transfer.py`) is the web face of the engine, and
the **narrow exception to ADR-0015 §4d** (mutators stay cwd-locked):
it accepts a destination project selector in the request body. The
exception is bounded the same way §3 bounds the engine — exactly one
artifact per invocation, exactly two roots — plus web-specific rails:
destinations are addressable **only** as discovered `project_scope_id`s
(no typed-path consent valve; that stays CLI-only), and the two risky
tiers require an explicit per-request opt-in flag. ADR-0015 §4d carries
a dated rider pointing here.

Request: source = path `{kind}/{name}` + the standard
`?project_scope_id=`/`?scope_id=` query selector (server cwd default) +
optional body `from_scope`; destination = body `to_target_scope` +
optional body `to_project_scope_id` (source project when omitted); body
`mode` (`move`|`copy`), `as_name` (copy-rename), and the two confirm
flags below. `?dry_run=true` returns the engine's dry-run plan
(`status="plan"`) without mutating and without demanding confirmation
(import-route precedent).

**Disclose-then-confirm** (the shared `_confirm.py` helper A-6 #1263
adopts next): a `project_shared` destination requires
`confirm_project_shared` (Gate B, §5); a `user`-tier destination
requires `allow_host_writes` (host path outside any project root,
disclosed via `host_targets` — the `generate_all_settings` refusal
shape). The first POST without the required flag performs no write and
returns HTTP 200 `{status: "needs_confirmation", confirm: <flag>,
reason, host_targets?, plan: {…dry-run result…}}`. `project_local`
destinations have no gate (no host write, no git-tracked write).

**Destination eligibility:** a project-tier destination whose
discovered scope is not sync-eligible is refused 409 with the existing
`resolve_writable_scope_root` reason-code shape (`sync_paused` /
`sync_not_enrolled`, message verbatim) — **including the implicit
destination** when `to_project_scope_id` is omitted but the *source*
selector names a non-cwd discovered scope, so the implicit spelling of
a destination can never write where the explicit spelling is refused
(Codex design-gate finding). A cross-root project-tier destination
without a `.memtomem/` store is refused 409 `no_memtomem_store` (the
A-3 CLI gate, web-shaped).

**Error envelope (campaign contract; B-1 #1284 retrofits old routes
onto it):** every route-raised non-2xx detail is an object
`{error_kind, message, reason_code?, …}`. `error_kind` vocabulary =
the overview classifier four (`parse` / `permission` / `missing` /
`internal`) plus three HTTP-semantic kinds: `validation` (bad
input/combination, 400), `conflict` (the 409 state-refusal family,
which also carries `reason_code` ∈ `sync_paused` / `sync_not_enrolled`
/ `destination_exists` / `no_memtomem_store`), and `busy` (503
lock/timeout). The one deliberate exception: `PrivacyScanError` keeps
the standard project_shared block envelope (422, string detail) every
sync surface emits. Engine errors map by TYPE, not message text — two
typed `ClickException` subclasses exist for exactly this
(`migrate.ArtifactNotFoundError` → 404, `transfer.TransferCollisionError`
→ 409; message literals unchanged, so CLI/MCP/`migrate_scope` consumers
are untouched).

**Concurrency:** the handler runs the engine in a worker thread under
the in-process `_gateway_lock` + `asyncio.timeout(60)`, passing
`lock_timeout=30.0` — a whole-call deadline shared across both
pair-lock acquisitions (`_acquire_pair_lock(timeout=…)`), so a
cross-process lock holder makes the worker self-abort
(`TimeoutError`, nothing acquired or committed) inside the request
window instead of writing after the 503 (#1145 orphan-thread shape;
`_SETTINGS_LOCK_BUDGET_S` / `_SKILLS_LOCK_BUDGET_S` precedent). The
outer timeout can still expire while the worker is mid-write —
`asyncio.to_thread` is un-cancellable — so the 503 wording makes no
no-commit claim. The response serializes `TransferResult` verbatim
(absolute paths; `src_project_scope_id` / `dst_project_scope_id`
computed for project tiers so the UI can offer one-click follow-up
sync; the §9 provenance triple on the wire for client matching).

### 11. Settings hooks: the A-11 per-hook copy mechanism (#1281)

Settings hooks are not `{kind}/{name}` artifacts, so their
cross-project copy is a **separate engine**
(`context/settings_copy.py`; CLI `mm context settings-copy`, web
`POST /api/context/settings/hooks/copy` in `settings_sync.py`) that
inherits this ADR's surface contracts (§10's error envelope,
disclose-then-confirm, destination eligibility, engine-offload shape)
with four mechanism-specific decisions A-12/A-13 should inherit where
applicable:

- **Dual write, canonical first (durability).** A stamped rule absent
  from the destination's canonical `.memtomem/settings.json` is
  garbage-collected by that project's next settings sync (ADR-0019
  owned-rule GC), so a tier-only copy would self-destruct. The copy
  writes the destination canonical (entry verbatim — the durable
  definition) and then the destination-tier Claude settings file
  (ADR-0019-stamped — live immediately); other runtimes ride the
  printed `cd <dst> && mm context sync --include=settings --scope
  <tier>` follow-up. Companion fix: `generate_all_settings` re-reads
  the canonical **under** the per-target lock so an in-flight sync
  holding a stale canonical cannot prune the freshly stamped rule;
  every no-write early exit stays pre-lock (host sidecars are never
  touched before consent).
- **Gate A always, `scope="project_shared"` hardcoded.** The
  destination canonical is git-tracked for every destination tier
  (the `promote_target_rule` precedent), so the fragment scan runs
  unconditionally, before the consent round-trip, with no force valve.
- **Pending-write-keyed gates.** `confirm_project_shared` is required
  whenever a git-tracked write is pending (the canonical leg always
  qualifies; `project_local` tier alone therefore still gates — unlike
  artifact transfers); `allow_host_writes` when the user-tier file
  would be written. No-op re-runs (`already_at_target` both legs)
  never prompt. Destination eligibility is evaluated as a project-tier
  destination for every tier — the canonical leg is a project write,
  so a paused destination refuses even user-tier copies.
- **Cross-leg conflict rule.** A canonical conflict skips both legs
  (a tier-only write would be replaced by the destination's own sync —
  the silent-evaporation failure); a tier conflict still writes the
  canonical leg and reports the half-apply. Conflicts never duplicate
  a matcher and the report names the colliding entry.

## Backward compatibility

- `migrate_scope` keeps its exact signature, result dataclass
  (`MigrateScopeResult`), error-message literals for every same-root
  case, Gate A audit attribution, and `MigratePartialError` semantics.
  Its body is now a delegation to `transfer_artifact(mode="move",
  src_project_root == dst_project_root)`.
- `_remove_runtime_fanout_for` / `_fanout_target_matches` /
  `_detect_source_scope` signature changes are module-private; no
  external callers existed.
- `context/override.py:resolve` widens `project_root` to
  `Path | None` (user-tier destinations without a project context);
  all existing call sites pass a `Path` and are unaffected.
- Two deliberate wording changes on invalid-scope inputs, both
  unreachable from every shipping surface (CLI flags are
  `click.Choice`-gated; the MCP tool validates before calling):
  a garbage `to_scope` now raises `ClickException` ("unsupported
  destination scope") instead of a raw `ContextScopeError`, and a
  garbage `from_scope` raises "unsupported source scope" instead of
  the old — and misleading — "`<kind>/<name>` not found at
  scope='<bogus>'." (Codex review chose documenting over replicating
  the misleading literal.)

## Consequences

- A-3 (CLI verbs), A-5 (web route), A-13 (MCP action) become thin
  argument-marshalling layers over one tested engine, mirroring how
  migrate's three surfaces share `migrate_scope` today.
- The fan-out cleanup two-root split makes the cross-project move
  leave no stale runtime entries in the source project — the #895
  orphan class cannot reappear at the cross-project boundary.
- Copy mode introduces the first supported way to have the same
  artifact bytes at two tiers/projects on purpose. Drift between the
  copies is the user's to manage (status surfaces show each project
  independently; cross-project drift aggregation is A-10 #1280).
- `TransferResult.needs_sync` + `sync_command` give every surface a
  uniform "what next" affordance instead of each one wording its own
  sync hint.

## Considered & rejected

- **Generating destination fan-out inline after promote.** Rejected:
  duplicates `_sync_atomic` Phase 2 (override resolution, skip codes,
  per-runtime suffixes) and would drift from it; sync stays the single
  writer of runtime trees. The result's `needs_sync` contract covers
  the UX instead.
- **`--force` destination overwrite.** Rejected — Row 15 parity
  (ADR-0016 §6 note); `replace` verb remains the named follow-up and a
  #1270 non-goal.
- **Rename in move mode.** Rejected: move preserves identity; rename
  semantics (fan-out cleanup under the old name, lock.json key change,
  version-history identity) compound badly with the move rollback
  ladder, and no campaign use case needs it.
- **Rewriting `overrides/` and `versions/` on rename.** Rejected:
  overrides are verbatim-by-contract (user-authored vendor bytes);
  version snapshots are frozen history (ADR-0022). A `notes` caveat
  plus documentation beats silent mutation of user content.
- **A distinct `.transfer-…` staging prefix.** Rejected: reusing the
  `.migrate-…` convention keeps every existing exclusion (internal-dir
  predicates, discovery skips, crash-leftover handling and tests)
  applying to both engines without a second pattern to maintain.
- **In-engine Gate B prompting.** Rejected: surfaces own confirmation
  UX (CLI flag, web disclose-then-confirm); the engine stays
  prompt-free and headless-safe, same as `migrate_scope`.
- **Unconditional `lock.json` provenance carry.** Rejected when A-4
  #1275 landed the carry-over (§9): copying the entry (or rehashing
  whatever bytes arrived) without the dirty/digest gates would bless
  locally-edited bytes as installed wiki state, letting a later
  `mm context update` clobber them without its `--force` gate. Same
  reasoning rejected carrying mtime-only (pre-#1247) entries and
  copy-renames.

## References

- Issues: #1270 (mechanism umbrella; non-goals list), #1273 (this
  engine, A-2), #1274 (A-3 CLI verbs), #1275 (A-4 provenance
  carry-over), #1276 (A-5 web route), #1279 (A-9 / ADR-0025 bulk +
  ADR-0021 supersession), #1280 (A-10 drift aggregation), #1281 /
  #1282 (settings hooks / mcp-servers transfer mechanisms), #1283
  (A-13 MCP action); #895 P2 (stale fan-out orphans), #1123 B4-1
  (dangling lock.json entry), #1247 id 6 (byte-verified fan-out
  cleanup).
- ADRs: ADR-0011 (§3 no project_local fan-out; §5 no force valve;
  PR-E4 scope move + Row 15), ADR-0015 / ADR-0016 (scope vocabulary;
  §5 write-target rule this ADR carves the bounded exception into;
  §6 conflict note), ADR-0021 (portal; its single-project-mutation
  clause stays in force — the planned ADR-0025 / A-9 #1279 owns that
  supersession, not this ADR), ADR-0022 (version snapshots, Gate A on
  frozen bytes).
- Source anchors (grep the symbol if line numbers drift):
  `packages/memtomem/src/memtomem/context/transfer.py`
  (`transfer_artifact`, `TransferResult`, `TransferCollisionError`,
  `_stage_copy`, `_rewrite_staged_manifest_name`),
  `packages/memtomem/src/memtomem/context/migrate.py`
  (`_acquire_pair_lock` — pair-shared `timeout`, `_stage_move`,
  `_promote_move`, `_existing_fanout_targets`,
  `_remove_runtime_fanout_for` — two-root split, `migrate_scope`
  wrapper, `ArtifactNotFoundError`),
  `packages/memtomem/src/memtomem/web/routes/context_transfer.py`
  (`transfer_context_artifact`, §10) and
  `packages/memtomem/src/memtomem/web/routes/_confirm.py`
  (`needs_confirmation_envelope`).
