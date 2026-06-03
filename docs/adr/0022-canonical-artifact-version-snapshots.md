# ADR-0022: Canonical artifact version snapshots + label pointers

**Status:** Proposed (deferred pending trigger — skills versioning)
**Date:** 2026-06-03
**Context:** A design review asked whether memtomem could manage canonical
context artifacts (`agents`, `commands`, `skills`) the way **Langfuse manages
prompts** — immutable versions plus movable labels (`production`, `staging`),
so editing the canonical and *deploying* it become two separate acts with
instant rollback. This ADR records the storage model, the resolve semantics
that `mm context sync` gains, and the scope boundaries (agents + commands in
v1; skills deferred).

## Background

Today the canonical → runtime fan-out is **edit-coupled-to-deploy**. The
unified atomic engine (`context/_sync_atomic.py`, ADR-0011 / #900) reads the
working canonical bytes inside its Phase-1 `(runtime-target, artifact)` loop —
once per pair, reusing that one buffer for both the privacy scan and the parse
(the TOCTOU-close) —

```
.memtomem/agents/<name>/agent.md      ← the single working canonical
        │  list_canonical_agents → (item_path, layout)
        ▼  for each (target, artifact): item_path.read_bytes()  (Phase 1, TOCTOU-close)
   render per runtime → .claude/agents/<name>.md, .codex/agents/<name>.toml, …
```

— then renders and writes the result to every runtime. There is no way to
**freeze** a known-good canonical, **promote** a specific frozen version to
"production", or **roll back** by moving a pointer. A bad edit to `agent.md` is
one `sync` away from every runtime; recovery means manually reconstructing the
prior text.

The substrate for versioning already exists:

- **Per-artifact directory layout** (ADR-0008) — `agents/<name>/agent.md` with
  a sibling `overrides/` dir. A `versions/` sibling is the natural home for
  snapshots, and `override.resolve()` already demonstrates the
  `base / <name> / <subdir>` addressing pattern.
- **Atomic file primitives** (`context/_atomic.py`) — `atomic_write_bytes`
  (`tempfile.mkstemp` + `os.replace`), and the sidecar `_file_lock` /
  `_lock_path_for` pair that `lockfile.py` and `projects.py` already use for
  `load → mutate → atomic_write_bytes` transactions.
- **A pluggable sync engine** — `AtomicSyncAdapter` bundles the per-artifact
  plug-in points; that single per-pair canonical read in Phase 1 is the one
  place a label-aware resolver intercepts. It is called once per
  `(target, artifact)`, so for N runtimes a label resolves N times for the same
  artifact (a cheap read-only dict lookup + file read — v1 does not refactor
  Phase 1 to resolve once-per-artifact-before-fan-out).

What is missing is the **edit/deploy split**: a place to store immutable
versions, a pointer layer (labels) over them, and a resolve step at sync time.

## Decision

Add a **per-artifact, file-native version store** with **label pointers**, and
teach the sync path to resolve a label to a frozen version's bytes instead of
the working file. The working file (`agent.md`) is never touched by versioning
operations.

### Storage model — explicit version files + JSON sidecar (not git)

```
.memtomem/agents/<name>/
├── agent.md            ← working canonical (unchanged; == label "latest")
├── overrides/
│   └── claude.md
├── versions/           ← immutable snapshots, write-once
│   ├── v1.md
│   └── v2.md
└── versions.json       ← version metadata + label pointers (the only mutable state)
```

```json
{
  "versions": {
    "v1": {"created_at": "2026-06-03T09:00:00Z", "note": ""},
    "v2": {"created_at": "2026-06-03T11:00:00Z", "note": "stable release"}
  },
  "labels": { "production": "v2", "staging": "v1" }
}
```

The store lives in a new `context/versioning.py` module with no awareness of
sync, CLI, MCP, or web — pure filesystem.

### Decision (a) — version unit is per-artifact, not store-snapshot

A version is a snapshot of **one** artifact (`agents/<name>`), mirroring
Langfuse's per-prompt versioning — **not** a whole-store commit. Rationale: the
fan-out, diff, and override surfaces are all per-artifact; a store-wide snapshot
would force a label like `production` to mean "this set of N artifacts at this
instant", which neither the UI rows nor the per-artifact sync results model.
Per-artifact keeps the new axis aligned with every existing axis.

### Decision (b) — artifact identity is `(scope, type, name)`; labels are orthogonal to runtime, uniform-vocabulary across scopes

**Artifact identity.** The version store (`versions/` + `versions.json`) lives
under the artifact's canonical directory, and that directory is **scope-
specific** (ADR-0011): `user` → `~/.memtomem/<type>/<name>/`, `project_shared`
→ `<root>/.memtomem/<type>/<name>/`, `project_local` →
`<root>/.memtomem/<type>.local/<name>/`. So the unit that owns a version store
and a label map is **`(scope, type, name)`**, not a single cross-tier store.
The user-tier `my-agent` and the project_shared `my-agent` have **independent**
version histories and label maps — a `production` pointer on one says nothing
about the other. There is intentionally **no** global/cross-tier label lookup.

**Orthogonal to runtime.** Within one `(scope, type, name)`, a label resolves
to **one** set of canonical bytes that fans out to **every** runtime (`claude`
/ `gemini` / `codex` / `kimi`) identically — a label is *not* per-runtime. The
label **vocabulary and semantics are uniform across scopes** (`production`
means "the promoted version" everywhere), but each `(scope, type, name)` stores
its own pointer.

**Out of v1.** Per-runtime labels (a different `production` per runtime) and a
genuine cross-tier label store are explicitly deferred — they would
combinatorially explode the UX (scope × runtime × label × version). v1 keeps
the label dimension flat *within* each `(scope, type, name)`.

### Decision (c) — explicit `versions/vN.md` files, NOT git commits/tags

Versions are plain files under `versions/`, indexed by `versions.json`. We
**reject** modeling versions as git commits/tags of the WikiStore even though
that substrate exists. Rationale:

- **Markdown-first is the trademark.** The whole gateway is file + git-native
  at the *working* layer; the versioning state must stay inspectable and
  diffable as plain files, not require a git plumbing call to read a label.
  This is held at the same level as the `privacy.py`/STM-decoupling invariants.
- **Git commits touch many artifacts at once.** A per-artifact label →
  per-artifact version mapping over shared commits would still need a sidecar
  to disambiguate "which commit is *this* artifact's v2", so the sidecar is
  unavoidable — at which point the commit adds nothing over a copied file.
- **No new dependency on git state for reads.** `resolve_label` is a dict
  lookup + a file read; it never shells out.

`versions.json` is the **only** mutable versioning state. Version `.md` files
are **immutable after creation** (write-once; `create_version` refuses to
overwrite an existing `vN.md`).

### Decision (d) — sync resolve semantics

- `latest` is a **reserved label**, never stored in `labels`, and always
  resolves to the **working file** (`agent.md`). It is artifact-aware by
  construction: the resolver is handed the real `item_path`, so the
  working-file name (`agent.md` vs `command.md`) is never hard-coded.
- `mm context sync` **without** `--label` is byte-for-byte the current
  behavior (== `--label latest` == working file). Existing CI/scripts do not
  change.
- `mm context sync --label production` resolves `production → v2 →
  versions/v2.md` and fans **that** out. `--label v2` (a bare version tag,
  matching `^v[1-9]\d*$`) resolves directly to the version, bypassing the label
  map.
- A labeled sync freezes only the **base canonical** bytes; per-vendor
  **overrides still apply live** (see invariant 9).
- `--label` only governs the **eligible kinds** (`agents`, `commands`). When an
  `mm context sync --include=…` run mixes eligible and ineligible kinds (e.g.
  `--include=agents,skills,settings`), the label applies to agents/commands and
  the ineligible kinds (`skills`, `settings`, project-memory) run **label-less
  as today**, with a one-line warning that `--label` does not apply to them. A
  label with **no** eligible kind in `--include` warns and is otherwise a no-op
  (does not error). (See invariant 10.)
- Resolution failures (unknown label, dangling tag, or a non-`latest` label on
  a flat-layout artifact with no version store) are **isolated per-artifact**
  into the engine's `skipped` list — consistent with how Phase 1 already
  isolates per-item read/parse failures — **not** raised to abort the whole
  fan-out. (`latest` / no-label on a flat artifact is unaffected — invariant 3.)

### Invariants pinned by this ADR

1. **File-only store.** No SQLite/Redis/external store for versioning. Files +
   one JSON sidecar, both under `.memtomem/<type>/<name>/`. **Per-artifact**,
   never a global `.memtomem/versions.json`.
2. **`latest` reserved** → always the working file; never written to `labels`.
3. **Directory-layout required (for versioning only).** Versioning needs the
   per-artifact directory (`agents/<name>/`). A flat-layout artifact
   (`agents/<name>.md`) has no per-artifact home. `latest` / no-label sync on a
   flat artifact is **unchanged** (current behavior — flat layout keeps
   working). Using a **non-`latest`** label or a bare version tag on a flat
   artifact is isolated as a per-artifact skip, and `mm context version create`
   on one refuses with a "run `mm context migrate` first" error. (Consistent
   with ADR-0008.)
4. **Immutable version files.** `vN.md` is write-once.
5. **Tag format** is `v` + a sequential integer starting at 1
   (`^v[1-9]\d*$` — `v0` is invalid), validated on create / load / resolve /
   promote so a hand-edited `versions.json` cannot point a label at a path-like
   tag (traversal guard).
6. **Concurrency.** `create_version` / `promote_label` / `delete_label` each
   hold a single non-reentrant `_file_lock` on the `versions.json` sidecar
   across their **entire** `load → mutate → write` transaction (the
   `lockfile.py` pattern). Two racing `create_version` calls cannot both
   allocate `v1`.
7. **Skills out of v1.** Skills are directory-based (a `SKILL.md` manifest plus
   assets), so a skill "version" is a directory-tree snapshot, not a single
   `.md` copy. v1 covers `agents` + `commands` only; skills versioning is a
   **deferred decision** with its own TRACKER row.
8. **`diff` is label-unaware in v1.** `mm context diff` always compares the
   working file (`latest`) against the runtimes; `--label` applies only to
   `sync` / `generate`. (Scope-out, see Open questions.)
9. **Versions snapshot the base canonical only, not overrides.** A `vN.md`
   freezes `agent.md`'s bytes. The sibling `overrides/<vendor>.<ext>` dir stays
   **mutable and live** — a labeled sync renders the frozen base canonical and
   then applies the *current* per-vendor override (overrides replace rendered
   output, ADR-0008). So a label freezes the base prompt, not the final
   per-vendor bytes. Freezing overrides too is a deliberate non-goal for v1
   (overrides are an escape hatch, not part of the versioned prompt).
10. **`--label` scopes to eligible kinds.** It governs only `agents` /
    `commands`; ineligible included kinds run label-less as today (with a
    warning), and a label with no eligible kind in `--include` is a warned
    no-op, never an error. (See Decision (d).)

## Consequences

- The engine change is minimal and additive: one optional
  `resolve_canonical_bytes` field on the frozen `AtomicSyncAdapter` (default
  `None` ⇒ today's `item_path.read_bytes()`), plus a `try/except` around the
  single Phase-1 read that maps the three versioning exceptions to typed skip
  codes. No reordering of the fixed search/sync pipeline. The TOCTOU-close
  (read once, reuse for scan + parse) is preserved because the resolved bytes
  are still captured once.
- Backward compatibility is total: no `--label` ⇒ working file. The module-
  level `_AGENT_ADAPTER` / `_COMMAND_ADAPTER` constants are never mutated;
  label-aware callers pass a `dataclasses.replace`-derived transient adapter.
- New per-artifact state (`versions/`, `versions.json`) lives only under
  `.memtomem/<type>/<name>/`; absence is a valid empty state (no migration of
  existing projects required).
- Three new typed skip codes (`label_not_found`, `version_not_found`,
  `versioning_requires_dir_layout`) join the closed `SkipCode` set so the web
  UI can match on them.
- Surfaces grow additively: a `mm context version` CLI group + a `--label`
  flag on `sync`/`generate`; two MCP tools (`mem_context_version`,
  `mem_context_promote`) registered via `@register("context")` (core-9 default
  mode unchanged); web `versions` routes + a version chip / promote action.

## Open questions & v1 scope-outs

**Tracked deferred decision** (§1) has a one-line `docs/adr/TRACKER.md` row.
**v1 scope-outs** (§2–§3) are intentionally left out, recorded here for context
but **not** tracked (no TRACKER row), per the TRACKER authoring rule.

1. **Skills versioning** — a directory-tree snapshot model
   (`versions/v1/<SKILL.md + assets>` rather than `versions/v1.md`), with the
   same label pointer layer. *Trigger:* a concrete need to freeze/rollback a
   skill, or user-reported parity-gap pain that agents/commands versioning
   alone does not cover. (TRACKER row.)
2. **Label-aware `diff`** — `mm context diff --label production` comparing a
   *labeled version* against the runtimes (not just the working file).
   *Revisit if:* repeated user friction reconciling "what is deployed
   (production) vs what the runtime has" without first promoting. (v1 scope-out
   — not tracked.)
3. **Version GC / retention** — pruning old `vN.md` snapshots (e.g. keep last
   N, or keep only label-referenced versions). v1 keeps every version forever;
   snapshots are small markdown files. *Revisit if:* a user reports
   `versions/` growth pain. (v1 scope-out — not tracked.)

## Alternatives considered

- **Git commits/tags as versions.** Rejected — see Decision (c): violates
  markdown-first inspectability, still needs a per-artifact sidecar, and adds a
  git-plumbing dependency to every label read.
- **Store-wide snapshots.** Rejected — see Decision (a): misaligns the new axis
  with the per-artifact fan-out / diff / override surfaces and makes a label
  mean "a set of artifacts at an instant".
- **Per-runtime labels / a cross-tier label store in v1.** Rejected — see
  Decision (b): scope × runtime × label × version is a UX explosion; v1 keeps
  the label dimension flat *within* each `(scope, type, name)` and orthogonal
  to runtime.
- **Skills in v1.** Rejected — directory-snapshot semantics differ enough to
  warrant their own design pass; bundling them violates "one focused change per
  PR" and delays the agents/commands slice.

## References

- ADR-0008 (wiki layer — directory layout, lockfile, overrides-replace),
  ADR-0011 (canonical artifact scope hierarchy — the tier axis this is
  orthogonal to), #900 (atomic sync engine extraction — the
  `AtomicSyncAdapter` / `sync_atomic_artifact` this intercepts).
- `context/_atomic.py` (`atomic_write_bytes`, `_file_lock`, `_lock_path_for`),
  `context/lockfile.py` (the `load → mutate → atomic_write_bytes` under-lock
  pattern this reuses), `context/override.py` (`base / <name> / <subdir>`
  addressing).
- Langfuse prompt management (versions + labels + get-by-label) — the
  conceptual model this adapts to a file-native substrate.
- Deferred-question row: `docs/adr/TRACKER.md`.
