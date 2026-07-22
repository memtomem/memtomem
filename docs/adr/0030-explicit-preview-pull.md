# ADR-0030: Explicit Preview/Pull model for the Context Gateway

**Status:** Proposed
**Date:** 2026-07-18 (§10 amended 2026-07-19, post PR-G design-gate)
**Context:** The Context Gateway's import path is a fixed-order "first
runtime wins" scan (claude → gemini → codex → kimi;
`context/skills.py:extract_skills_to_canonical`, and the agents/commands
equivalents). When the same artifact name exists in more than one runtime
directory with different bytes, no surface — CLI, Web, or MCP — lets the
user choose which runtime to import from, and the import preview is
count-level only. A real multi-runtime project demonstrated the failure
shape: the freshest copy lived in `.agents/skills` (Codex) while stale
copies sat in `.claude/skills`, so a default import silently takes the
stale Claude bytes. This ADR records the decisions for completing the
gateway's **explicit Preview/Pull** model — campaign 1 of the
global/per-project portal master plan — on top of ADR-0009 (info
surface), ADR-0011 (scope hierarchy + Gate A), ADR-0022 (version
snapshots), ADR-0024/0025 (sync orchestration), and ADR-0026 (onboarding
IA). Campaign 2 (persistent Change Sets, plan-digest approval, deployment
bindings, multi-project batch apply) is out of scope here and will get
its own ADR.

## Vocabulary

- **Pull** — bring one runtime's copy of an artifact into the Store
  (today's "Import", made source-selectable and preview-first).
- **Push** — fan the Store out to runtimes (today's "Sync").
- **Pull preview** — a read-only, per-candidate report of what a Pull
  *would* land in the Store, including privacy-gate results.

## Decisions

### 1. Writes are explicit; detection is automatic (two stages)

Auto-application of runtime-directory changes (auto-pull / auto-sync) is
a **permanent non-goal**. Every Store or runtime write happens through an
explicit user-approved Pull or Push. Detection, however, is automatic:

- **Stage 1 (this campaign):** a pull-direction drift probe runs at
  portal open and alongside the existing fleet check, summarizing
  pull-preview statuses into a "runtime copy differs from Store —
  Preview/Pull" badge. No writes.
- **Stage 2 (post-campaign follow-up, separate design gate):** optional
  watchdog-based real-time watching of runtime directories. Its
  prerequisites are recorded now: self-write suppression (gateway
  fan-out must not bounce its own events; compare against
  lockfile/digest, not mtime), filtering of non-artifact churn in
  runtime dirs (e.g. `.claude/settings.local.json`), and a bound on
  watch count across enrolled projects × runtimes.

### 2. Push/Pull becomes the user-facing vocabulary (ADR-0026 P2 adopted)

UI labels, CLI help, and guides rename **Sync → Push** and **Import →
Pull**. Backend request/response identifiers, `reason_code` values, and
route paths are unchanged (exactly the ADR-0026 P2 cut). This explicitly
reverses the "#1353 P2 NO-GO — deferred pending a naive-user validation
gate" posture, recorded as a **narrow supersession of ADR-0026 §P2**
(the PR that ships the rename also annotates ADR-0026).

Reversibility is stated precisely, in two parts:

- **Reversible (the D-F precedent):** the relabeling of existing
  surfaces — locale strings, badges, CLI help copy, guide prose.
  Rollback is a locale/help revert; the §Validation kit remains
  available for post-rename evidence; no wire change is involved.
- **Additive and permanent, not part of the rename:** the new public
  interfaces this campaign introduces (`mm context pull`,
  `mem_context_pull`, `sync --runtime`). They are named "pull" from
  birth and would survive a label rollback as ordinary public API —
  their stability is governed by normal deprecation policy, not by
  this section's reversibility claim.

### 3. ADR-0009 relationship: leaf-only Pull stands

ADR-0009's "dashboard is an informational cockpit; import remains a
leaf-only action" survives the rename: the dashboard copy switches to
Push/Pull vocabulary, but **Pull (with its runtime picker) remains a
leaf action** on a single artifact's detail surface. Bulk pull stays out
of scope; the section-level batch import keeps its current first-wins
behavior for byte-compatibility.

### 4. Pull-preview status vocabulary (two axes; includes the privacy gate)

The pull preview has its own vocabulary, distinct from the push-diff
`DiffRow` contract (consumed positionally; not extended), and it is
**two orthogonal axes**, not one status set:

- **`content_status`** — the Store↔candidate content relation. The
  frozen wire tokens (PR-B, a `Literal` on the response model so a
  misspelling 500s) are `new` (Store has no copy), `differs`,
  `identical`, and two distinct error phases (an error is always a
  preview **row**, never a 500): `landing_error` — the would-land bytes
  could not be computed (unreadable source, TOML parse failure), and
  `store_error` — the would-land bytes WERE computed but the current
  Store copy could not be read. Plus `not_importable` (export-only
  runtimes: codex/kimi agents, codex commands; display-only rows). The
  two error phases are split because they participate in §5 differently
  (see there).
- **`gate_status`** — the privacy-gate outcome for the destination
  tier: `ok`, **`blocked`** (hard-refusing tier, e.g. `project_shared`),
  **`requires_unsafe_confirmation`** (force-bypassable tiers), or `null`
  for a `not_importable` or `landing_error` row (nothing scannable). For
  skills the gate scans the **full copier surface** (everything a Pull
  would land, including a runtime's top-level `overrides/`/`versions/`),
  not the payload subset used for `content_status` — else a secret under
  runtime metadata would preview `ok` yet be copied unscanned.

A candidate can be `differs` **and** `blocked` at the same time —
collapsing the axes would lose the drift information §1's probe needs.
To produce `gate_status`, the Gate A scan is extracted into a
side-effect-free check shared by the preview and the import engines, so
a preview can never show "appliable" for a Pull that the ingress scan
would then refuse (ADR-0011 §5 remains the enforcement point at write
time).

Direction framing follows ADR-0009's direction-neutral stance for the
both-exist case: **`ahead`/`behind` wording is reserved for
missing-copy cases** ("Store has it, runtime doesn't" / vice versa);
when both copies exist with different bytes the state renders as
`differs`/`diverged`, never as a direction claim. The subject of any
directional copy is always the Store, recorded in the locale glossary.

### 5. Ambiguity is judged on distinct landing content

When a Pull has multiple runtime candidates and no explicit source, the
refusal rule keys off the number of **distinct contents that would land
in the Store** — for gemini commands that is the converted canonical
Markdown, not the raw TOML — and not the runtime count:

  Distinctness is judged over the **Pull payload** — the bytes that would
  actually land — not the full copier surface. Store-owned metadata is
  either preserved from the Store (a skill's `overrides/` / `versions/`,
  §10) or absent from a runtime copy entirely, so two candidates with
  matching payload but differing top-level `versions.json` land the SAME
  bytes and must NOT be reported as ambiguous — forcing a source choice
  there would be spurious. For agents/commands the payload and the full
  surface are identical, so this is a skills-only refinement (added with
  the §10 overwrite-Pull, which introduced the payload/metadata split).
  **Gate A still scans the full copier surface** — a secret hiding under a
  non-payload path must be caught even though it never lands. PR-B computes
  this signal (an `ambiguous` flag + an `auto_source`); the refusal it
  drives is enforced by the CLI/Web at apply time (PR-C/PR-D), not by the
  preview.

- All candidates byte-identical (post-conversion): auto-select in the
  existing priority order and disclose the duplicates.
- More than one distinct landing content: CLI `--apply` and the Web
  dialog refuse until the user picks a source (`source_conflict`).
- **Fail closed on incomputable content**: if any content-bearing
  candidate's landing bytes cannot be computed (`content_status:
  landing_error`), auto-selection is off — the Pull requires an explicit
  `--from` even if the remaining readable candidates agree. An
  unreadable copy might be the divergent one. A `store_error` candidate,
  by contrast, **does** participate: its landing bytes WERE computed
  (only the Store side was unreadable), so it groups normally.
- `not_importable` rows are display-only and never participate in the
  distinct-content count; `blocked` / `requires_unsafe_confirmation`
  (gate) candidates' content **does** participate (the gate blocks the
  write, not the comparison).
- The batch surfaces (`mm context init --include`, the section-level
  Web import) keep first-wins unchanged.

### 6. Overwrite-Pull is snapshot-first and transactional (ADR-0022 amended)

ADR-0022 invariant 7 (skills excluded from versioning) is **lifted in
principle**; the skills tree-snapshot protocol (§10) is its
implementation. Until that ships, a Pull that would overwrite an
existing skills Store entry is **refused at the engine level** — only
`new` pulls are allowed for skills. For agents/commands, an
overwrite-Pull first snapshots the current canonical via the existing
versioning engine, and **snapshot + canonical replacement execute inside
one canonical-sidecar-lock transaction**.

The lock claim is not true of today's code and is therefore a **new
requirement this campaign introduces** (PR-B2), not a description of
current behavior: today only the skills import path holds a destination
sidecar lock, while agents/commands reverse imports
(`context/_atomic_reverse.py`), Web canonical updates (which rely on the
process-local gateway lock only), and atomic-file Push
(`context/_sync_atomic.py`) write canonicals without one — so another
process could write B after snapshot(A) and before replace(C), losing B.
PR-B2 therefore makes the **canonical sidecar lock mandatory for every
first-party mutation of a reverse-import canonical — skills, agents, and
commands — and for the snapshot read**, across CLI, Web, and MCP
surfaces. Lock ordering is normative: acquire the canonical sidecar
lock(s) first (sorted, when more than one), then the child sidecar the
operation needs — the `versions.json` lock (version/label ops) or the
wiki `lock.json` lock (install/update); `create_version()`'s own lock
guards only the manifest transaction and is insufficient alone.
Non-first-party writers (editors, shells) remain outside the guarantee,
as with fan-out today.

The lock **identity is name-keyed and layout-independent**
(`<canonical_root>/.{name}.lock`) so a Pull, a flat→dir migrate, a
cross-scope transfer, and a version op on one artifact all contend on a
single lock regardless of flat/dir layout; every writer resolves the
destination *inside* the lock so a layout conversion cannot strand a
stale-path write. The coverage is delivered in two PRs: **PR-B2a** makes
the lock authoritative across all those mutation sites (reverse import,
web CRUD, version create/enable/promote/delete-label, transfer, migrate,
wiki install/update, the first-party validation seeder) with behavior
otherwise unchanged; **PR-B2b** then layers the snapshot-first overwrite
and the skills/flat refusals on top.

**Out of scope (tracked follow-up):** MCP-server canonicals
(`.mcp.json`) are not a Pull target, and their web CRUD
(gateway-lock-only) versus cross-project copy (path-keyed
`.{name}.json.lock`) cross-process gap is **pre-existing** — not
introduced or worsened by this campaign. Unifying them onto the
name-keyed canonical lock is deferred to a separate change; this ADR's
mandatory-lock scope is the reverse-import kinds above.

Flat-layout canonicals (pre-ADR-0008 installs) are **refused** with a
remediation hint pointing at the existing `mm context migrate` flat→dir
conversion — no new flat-snapshot machinery.

### 7. Vendor overrides: pull warns, never silently bakes

A runtime copy that equals a vendor override
(`<canonical>/<name>/overrides/<vendor>.<ext>`) shows as "in sync" in
the push diff, but pulling it would bake the override into the **base**
canonical. The pull preview therefore compares raw-vs-raw (no override
substitution) and attaches an explicit warning note when the candidate
matches an override. The warning is the mitigation; Pull is not blocked.

### 8. MCP parity ships in this campaign

`mem_context_pull` (and a `runtimes=` parameter on `mem_context_sync`)
ship with the campaign, reversing the ADR-0025 "MCP parity out of scope"
precedent for this surface. MCP cannot prompt, so ambiguity (§5) returns
a typed "needs runtime: candidates are …" result, and `dry_run=True` is
the default (preview text with redacted paths, per the canonical-path
redaction rule).

### 9. User-tier portal status gets its own wire shape

`GET /api/context/status-all` stays `project_shared`-only; its response
presumes project discovery, enrollment, and per-project entries. The
user tier gets a **separate global-status endpoint** (single
`~/.memtomem` scope + runtime coverage + pull-drift summary) rather than
a mode-discriminated overload, freezing the existing wire shape. The
portal board gains a user-tier "global library" landing section fed by
it.

### 10. Skills tree-snapshot protocol (implementation gate for §6)

Skills versioning generalizes the flat model to trees. Version
artifacts live **inside the artifact directory** today
(`<canonical>/<name>/versions/vN.md` + `versions.json`), which creates
a recursion hazard for trees: a naive tree snapshot would include the
version store itself, v2 would contain v1, and Push/diff/Gate A would
leak internal metadata into runtimes. The protocol therefore starts
from a payload definition:

> **Amended 2026-07-19** after the PR-G design-gate (three review
> rounds + POSIX durability/portability research). The bullets below are
> the revised, code-ready baseline; they supersede the original where they
> differ (the two surfaces below replace the earlier "one iterator for
> everything" wording, orphan `vN/` dirs are **preserved not reaped**, and
> the skills version surface is **read-only** in this campaign).

- **Two surfaces, precisely scoped — not one.** A **payload iterator**
  defines the artifact *content*: it excludes top-level `overrides/`,
  top-level `versions/` and `versions.json`, lock/staging artifacts
  (`COPY_SKIP_NAMES`), and symlinks. It drives the **snapshot content,
  the tree digest, the Push diff, and the content that Push fans out**. The
  **ingress Gate-A privacy scan stays WIDE** — it scans the full
  would-land copier surface (payload **plus** `overrides/`), because a
  secret under runtime metadata must still be caught before it lands
  (§4). `_iter_scannable_skill_files` remains that wide scan; the payload
  iterator is the narrower content view. Both are pinned so that version
  history can never fan out to runtimes and a snapshot can never contain
  a snapshot, without collapsing the ingress gate.
- **Tree digest = SHA-256 over (relative path, bytes)** of the payload
  iterator's files, with length-prefixed framing so no `(rel, bytes)`
  pair can be confused with a different split (the existing
  `_payload_digest` shape is promoted as the canonical serialization).
  The digest is **file-only** — empty directories are not tracked — and
  the **executable bit is excluded** (the copier normalizes modes to
  0o644; preserving a bit it drops would make digests unreproducible).
  Campaign 2's snapshot CAS adopts this same definition — one digest, two
  consumers; the master plan's exec-bit mention is superseded here.
- Storage: `<canonical>/<name>/versions/vN/` directory snapshots beside
  today's `<canonical>/<name>/versions/vN.md` files, sharing
  `versions.json` with a per-entry `layout: "tree"` marker and a
  top-level `schema_version` bump.
- **Schema-compat prep ships first** (PR-G1, before any tree manifest is
  ever written): today `load_manifest()` ignores unknown top-level fields
  and `_save_manifest()` rewrites only `versions`/`labels`, so an old
  mutator would silently strip `schema_version`/`layout`. The prep step
  makes readers refuse an unknown/newer `schema_version` loudly
  (validating it is a positive int), makes writers round-trip unknown
  top-level **and** per-entry fields, with preservation/refusal tests
  pinned — only then does the tree layout land.
- **Snapshot creation copies bytes into new inodes** (like the flat
  `create_version`, from the captured pre-image) — it never hardlinks
  live payload, which an editor or a pre-swap crash could mutate through
  a shared inode. Atomic promotion: stage the snapshot dir, flush (fsync,
  plus `F_FULLFSYNC` on macOS), rename into place under the canonical
  sidecar lock (§6), then flush the parent directory; a crash leaves
  either no entry or a complete one. **Orphan `vN/` dirs without a
  manifest row are PRESERVED, never reaped** — tag allocation reconciles
  over on-disk `vN/` dirs (as it does over flat `vN.md` today), so an
  orphan bumps the next tag rather than being deleted. Only the
  `.staging-*`/`.old-*`/`.swap-*` transients are reaped, and an `.old-*`
  only while the canonical is present.
- **Overwrite-Pull transaction (the §6 mechanism, for skills).** The
  runtime **payload** replaces the Store payload; the Store's
  **`overrides/` is preserved via an independent copy** (mutable,
  un-versioned per ADR-0027 — a runtime copy has none, so "replace" would
  delete the user's edits) and the Store's **`versions/` via file
  hardlink** (immutable). Because a non-empty directory cannot be
  replaced atomically on POSIX, the swap is two renames guarded by a
  durable `.swap` intent marker whose identity is bound by name and
  transaction suffix, plus a recovery state machine with parent-directory
  fsync barriers on both the forward and recovery paths; a foreign
  out-of-band recreation of the destination fails closed rather than
  clobbering. Durability degrades to process-crash consistency on
  filesystems that reject directory fsync (network/tmpfs/Windows),
  matching the existing single-file overwrite.
- **API compatibility (read-only this campaign).** Skills gain a
  read-only `version list` across CLI/Web/MCP; `create`/`promote`/
  `delete-label` remain **refused for skills** (a skill version is
  created only internally by an overwrite-Pull), and labeled skill
  **fan-out** — which needs a `label` argument the skill generators do
  not have — is deferred to a follow-up. The flat `agents`/`commands`
  version table is unchanged; `enable` (flat→dir adoption) is a no-op for
  skills (always dir-layout).

This section is the design baseline; it has had its design-gate review
(three rounds) and is ready to implement as PR-G0..G4.

### 11. CLI shape

`mm context pull <kind> <name> [--from RUNTIME] [--scope SCOPE]
[--overwrite] [--diff] [--apply] [--yes] [--force-unsafe-import]` — a
new verb (not an `init --only` extension; `init` is already overloaded
with context.md seeding and Gate B prompts). Dry-run preview is the
default; `--apply` executes (the `migrate` precedent). `project_local`
is rejected (no runtime fan-out to pull from, ADR-0011 §3). Scope
handling honors ADR-0011's explicit-choice rule for the git-tracked
tier: the **preview** may run with an inferred scope, but an `--apply`
whose destination is `project_shared` requires the **explicit**
`--scope project_shared` (plus Gate B confirmation via `--yes` or
prompt) — a new command does not inherit `init`'s legacy implicit
default into a git-tracked write. `mm context sync` gains an additive
`--runtime` filter (default: all detected runtimes, unchanged).

### 12. Source-runtime vocabulary is a first-class table

Which runtimes are pull-eligible per artifact kind becomes a single
table (`IMPORT_SOURCE_RUNTIMES` in `context/_runtime_targets.py`):
skills = all known runtimes; agents = commands = claude + gemini
(codex/kimi are export-only renderers). The engines' hardcoded loops are
replaced by the table so pickers, validation, and preview can never
drift from the engines.

## Consequences

- The "stale claude beats fresh codex" failure shape becomes
  unrepresentable through the Pull surfaces: divergent candidates force
  an explicit choice (§5), and the preview shows the divergence (§4).
- The Store's authority is strengthened, not weakened: Pull is the only
  runtime→Store path, it is preview-first, snapshot-first on overwrite
  (§6), and privacy-gated identically to import today.
- Existing automation keeps working: no default behavior of
  `init`/`sync`/batch import changes; every new parameter is additive
  and keyword-only.
- The rename (§2) is copy-only and reversible; wire vocabulary is
  frozen, so API clients are unaffected.

## Rollout (campaign 1 PR map)

A: engine `source_runtime` + table (§12) → B: pull-preview engine +
route (§4, §7) → B2: snapshot/lock transaction + skills overwrite
refusal (§6) → C: CLI `pull` + `sync --runtime` (§11, §5) → D: Web Pull
flow (picker + preview) → E: Push/Pull rename (§2) → F: user-tier
portal + drift probe (§9, §1) → G: skills tree snapshots (§10, own
gate), split **G0** ADR amendment → **G1** manifest schema-compat prep →
**G2** shared payload iterator + tree digest → **G3** tree-snapshot
storage/engine + read-only skills `version list` → **G4** lift the skills
overwrite refusal (history-preserving transaction) → H: MCP parity (§8).
Fixture-based E2E is mandatory; real-project verification is read-only
smoke only.

## References

- ADR-0009 (info surface; §3), ADR-0011 (Gate A, tiers; §4, §6),
  ADR-0022 (versioning; §6, §10), ADR-0024/0025 (orchestration; §8),
  ADR-0026 (P2 rename; §2).
- Master plan: `docs/plans/context-gateway-global-project-portal-plan-2026-07-18.md`
  (local planning doc, untracked) — campaign structure and campaign 2
  scope.
- The 2026-07-10 hardening plan's "import winner disclosure" backlog
  item is superseded by §4/§5; its D1 (fail-closed **sync** preview)
  stays an independent pending decision — the `/sync*` best-effort
  preview contract is untouched by this ADR.
