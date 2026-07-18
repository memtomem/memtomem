# ADR-0030: Explicit Preview/Pull model for the Context Gateway

**Status:** Proposed
**Date:** 2026-07-18
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

- **`content_status`** — the Store↔candidate content relation: `new`
  (Store has no copy), `differs`, `identical`, `error` (unreadable
  source, TOML parse failure — an error is a preview **row**, never a
  500), `not importable` (export-only runtimes: codex/kimi agents,
  codex commands; display-only rows).
- **`gate_status`** — the privacy-gate outcome for the destination
  tier: `ok`, **`blocked`** (hard-refusing tier, e.g.
  `project_shared`), **`requires unsafe confirmation`**
  (force-bypassable tiers).

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

- All candidates byte-identical (post-conversion): auto-select in the
  existing priority order and disclose the duplicates.
- More than one distinct landing content: CLI `--apply` and the Web
  dialog refuse until the user picks a source (`source_conflict`).
- **Fail closed on incomputable content**: if any content-bearing
  candidate's landing bytes cannot be computed (`content_status:
  error`), auto-selection is off — the Pull requires an explicit
  `--from` even if the remaining readable candidates agree. An
  unreadable copy might be the divergent one.
- `not importable` rows are display-only and never participate in the
  distinct-content count; `blocked` candidates' content **does**
  participate (the gate blocks the write, not the comparison).
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
first-party canonical mutation and for the snapshot read**, across CLI,
Web, and MCP surfaces. Lock ordering is normative: acquire the
canonical sidecar lock(s) first (sorted, when more than one), then the
`versions.json` lock; `create_version()`'s own lock guards only the
manifest transaction and is insufficient alone. Non-first-party writers
(editors, shells) remain outside the guarantee, as with fan-out today.

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

- **One shared skill-payload iterator** defines the artifact payload
  everywhere: it excludes top-level `overrides/`, top-level `versions/`
  and `versions.json`, lock/staging artifacts (`COPY_SKIP_NAMES`), and
  symlinks. Snapshot, tree digest, privacy scan, fan-out, and diff all
  consume this one iterator — today's per-caller walks
  (`_iter_scannable_skill_files`, the `_stage_skill` copier surface)
  converge on it, so version history can never fan out to runtimes and
  a snapshot can never contain a snapshot.
- **Tree digest = SHA-256 over (relative path, bytes)** of the payload
  iterator's files. The executable bit is **not** part of the digest:
  the current copier normalizes file modes (0o644) and preserving a bit
  the copier drops would make digests unreproducible. Campaign 2's
  snapshot CAS adopts this same definition — one digest, two consumers;
  the master plan's exec-bit mention is superseded here.
- Storage: `<canonical>/<name>/versions/vN/` directory snapshots beside
  today's `<canonical>/<name>/versions/vN.md` files, sharing
  `versions.json` with a per-entry `layout: "tree"` marker and a
  top-level `schema_version` bump.
- **Schema-compat prep ships first** (inside PR-G, before any tree
  manifest is ever written): today `load_manifest()` ignores unknown
  top-level fields and `_save_manifest()` rewrites only
  `versions`/`labels`, so an old mutator would silently strip
  `schema_version`/`layout`. The prep step makes readers refuse an
  unknown `schema_version` loudly and makes writers round-trip unknown
  fields, with preservation/refusal tests pinned — only then does the
  tree layout land.
- Atomic promotion: stage the snapshot dir, fsync, rename into place
  under the canonical sidecar lock (§6); a crash leaves either no entry
  or a complete one. Orphan `vN/` dirs without a manifest row are
  reaped on the next versioning operation (mirroring the staged-promote
  reaper pattern in `context/skills.py`).
- API compatibility: list/promote/delete keep their shapes; `enable`
  (flat→dir adoption) is a no-op for skills (always dir-layout).

This section is the design baseline; the implementing PR gets its own
design-gate review before code lands.

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
gate) → H: MCP parity (§8). Fixture-based E2E is mandatory; real-project
verification is read-only smoke only.

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
