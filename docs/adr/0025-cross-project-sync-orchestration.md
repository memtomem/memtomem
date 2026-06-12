# ADR-0025: Cross-project sync orchestration — bulk sync over the project registry

**Status:** Accepted (narrowly supersedes ADR-0021 §"Relationship to
ADR-0009" item 1's "Mutations remain single active-project per
invocation" clause, for sync orchestration only; resolves that
section's "Cross-project *bulk* sync is explicitly deferred" note)
**Date:** 2026-06-12
**Context:** Context Gateway completion campaign, mechanism track #1270
item A-9 (#1279). Keeping several registered projects' runtime files
fresh after canonical edits required running sync once per project by
hand. ADR-0021 deferred cross-project bulk sync pending a need signal;
the campaign owner has signaled it. ADR-0023 §3 (bounded two-roots
transfer exception) explicitly delegated the ADR-0021
single-project-mutation supersession to this ADR.

## Decision

### 1. Bounded supersession — a batch is sequenced single-project syncs

A bulk-sync invocation (`mm context sync --all-projects`,
`POST /api/context/sync-all-projects`) may write N projects' runtimes.
The superseded ADR-0021 clause narrows only in the *per-invocation*
dimension; everything it protected per write is preserved by
construction:

- each per-project execution is the UNCHANGED single-project pipeline
  targeting exactly one `(project_root, target_scope=project_shared)` —
  ADR-0016 §5 ("writes land in exactly one tier per invocation") holds
  per write;
- no new cross-project write primitive exists — the batch surfaces loop
  the existing per-project entry points (the CLI sync legs; the five
  ADR-0024 `_sync_*_core` phases);
- registry-lifecycle mutations and every non-sync artifact mutation
  remain single-active-project per invocation — this supersession covers
  sync orchestration ONLY. (ADR-0023 §3's two-roots transfer exception
  is the only other bounded carve-out.)

### 2. Loop set + skip semantics — batch reports, never refuses

Both surfaces iterate the discovered project scopes
(`discover_project_scopes`, display order: server-cwd first, dedup by
resolved path) and execute a scope iff it passes
`context.projects.sync_skip_reason` — one shared derivation so the CLI
and web cannot drift on WHICH scopes execute. Non-executed scopes are
REPORTED as skipped rows with a reason code; where the single-project
route 409s on an ineligible explicit selector, the batch records and
proceeds (the `_run_update_all` precedent):

- `missing_root` — registered root no longer a directory (checked
  first: physical absence trumps enrollment state);
- `sync_paused` / `sync_not_enrolled` — `sync_eligible` is False
  (mirrors the web resolver's eligibility-409 split);
- `stale_project` — root exists but has no `.memtomem/` store.
  **Batch-only gate**: bulk-syncing a tree the user never initialized
  would at best no-op every phase and at worst seed `.memtomem/`
  bookkeeping; the per-type single routes stay ungated on stale.
  Remediation (run `mm context init` there) is in the row message.

Each surface owns its remediation prose (portal verbs on the web, `mm`
verbs on the CLI); the codes are the shared contract. Zero eligible
projects is a no-op success: CLI exits 0 informationally (cron safety),
web returns 200 with an all-skipped report.

CLI batch discovery anchors at `find_project_root()` — NOT raw
`Path.cwd()` like `mm context projects list` — so a run from a project
subdirectory treats that project as the cwd scope, matching both
single-sync semantics (which walks up) and the web lifespan anchor.

### 3. Per-project isolation; per-project lock window (web)

One project's failure — engine error, privacy refusal, lock timeout —
converts to a failed row and the batch proceeds. This extends ADR-0024
§1 (per-phase proceed-past-failure) one level up, and cross-project
rollback is rejected for the same reason cross-type rollback was:
project stores are independent; there is no cross-project snapshot
invariant to restore.

The web batch wraps EACH project's five phases in its own
`_gateway_lock` + `asyncio.timeout(300)` window, released between
projects:

- the invariant the ADR-0024 window protects — no per-type mutator
  interleaving between ONE project's phases — holds per window;
  cross-project interleaving by other mutators is harmless;
- a batch-wide window would starve every other gateway mutator for
  N×5 phases.

A project-level timeout / unexpected error yields a failed entry with
the `error` envelope attached and the completed phases kept (their
writes are real); `summary` is present iff the project's phase loop
completed. There is deliberately NO batch-level timeout: it would
discard completed projects' reports mid-flight — the failure shape the
per-phase report exists to avoid. The worst-case N×300s serial run is
the honest cost of a serial batch.

### 4. Tier policy — project_shared only

Web: the same two 400s as ADR-0024 §4, raised by the shared
`_reject_ineligible_tier` gate (one source for the issue-pinned
literals). CLI: `--all-projects` combined with `--scope user` or
`--scope project_local` is a usage error.

The CLI batch passes an explicit `scope_flag="project_shared"` into the
sync legs so the settings leg's `_resolve_cli_scope` cannot fall through
to `cfg.hooks.target_scope` — a config-pinned `user` value would fan the
SAME host files (`~/.claude/settings.json`, …) out once per project. On
project_shared every settings target resolves inside the project root,
so the host-write confirmation is vacuous (kept as defense in depth).
The user tier remains reachable only through the per-project, per-type
paths with their host-write confirmation — the batch cannot become a
confirmation-bypass multiplier.

### 5. Report shapes

**Web** — HTTP 200 whenever the loop ran (mixed results cannot map to
one HTTP code; ADR-0024 precedent). Non-2xx only pre-run: 400 tier
gate, 403 CSRF.

```jsonc
{
  "projects": [
    {"project_scope_id": "p-…", "label": "…", "root": "/abs/path",
     "status": "ok|partial|failed",          // = its ADR-0024 summary.status
     "phases": [...], "summary": {...}},      // ADR-0024 report, verbatim
    {"project_scope_id": "p-…", "label": "…", "root": "/abs/path",
     "status": "skipped", "reason_code": "sync_paused", "message": "…"}
  ],
  "summary": {"status": "ok|partial|failed", "projects_total": 4,
              "executed": 2, "ok": 1, "partial": 0, "failed": 1,
              "skipped": 2, "generated_total": 9, "skipped_rows_total": 3}
}
```

- Executed entries embed the ADR-0024 single-project report verbatim
  under the project identity; skip-row classification stays client-side
  (#1262) and the totals are counts, not classifications.
- Batch `summary.status`: `failed` = every EXECUTED project failed;
  `ok` = no executed project failed or partial — an all-skipped batch
  is `ok` with `executed: 0` visible (skipping paused projects is the
  designed outcome; unattended callers need the no-op run to read as
  success; a `failed == executed` check alone would mark 0/0 failed);
  else `partial`.
- Version snapshots carry `surface="web_context_sync_all_projects"` /
  `"cli_context_sync_all_projects"` — the audit trail names the batch
  orchestrator (ADR-0024 precedent).

**CLI** — cloned from the `_run_update_all` flow: discover → classify
(eligibility/health, NOT a dirty-diff preview — running four diff
engines × N projects for a preview is heavy and still racy) → preview
table → confirm (`--yes` skips) → serial execute printing the same
per-leg output single sync prints under a per-project header → summary
(`N synced, M failed, K skipped`) → exit 1 if any project failed.

Include semantics are UNCHANGED: the default include set stays the
project-memory leg only (each project's `context.md` → its agent
files); `--include skills,agents,commands,settings` adds the artifact
legs. The flag multiplies the existing command across projects — it
does not change what one project's sync means. A missing `context.md`
in a batch row is a yellow note, not a failure (a registered project
without project memory is normal at batch scope). `--strict` /
`--on-drop` / `--label` thread through per project; a label missing in
one project fails that row only.

## Consequences

- ADR-0021 §"Relationship to ADR-0009" item 1 gets an in-place
  superseded-by rider (this clause only; the Portal's read/management
  scope and registry-lifecycle rules are untouched). No TRACKER row —
  nothing is deferred here.
- The front-end Sync-All-Projects switchover (a dashboard affordance
  over the batch route) is a follow-up, B-track concern; this ADR ships
  the API + CLI only. MCP parity remains out of scope (ADR-0021
  §"Open questions" §5 v1 scope-out, unchanged).
- New web mutator bookkeeping: `context_sync_all.sync_all_projects_context`
  is classified in `_CSRF_PROTECTED` and `_REDACTION_EXEMPT`
  (`tests/test_web_invariants_registry.py`).
- `_run_phase` gained a `surface` keyword (default preserves the A-8
  attribution) and the CLI `_print_*_generate` helpers gained the same
  pass-through; the single-project surfaces are unchanged.

## Alternatives considered

- **One batch-wide lock window.** Rejected — starves every other
  mutator for N×5 phases and protects no cross-project invariant
  (stores are independent).
- **Batch-wide outer timeout.** Rejected — discards completed projects'
  reports mid-flight; the per-project 300s window bounds each unit.
- **Executing stale projects.** Rejected for the batch — would seed
  `.memtomem/` bookkeeping into never-initialized trees; cheap to flip
  later if a real workflow wants it (the per-type single routes already
  allow it deliberately).
- **`--all-projects` defaulting the include set to all four kinds.**
  Rejected — silently multiplies a default that today touches only the
  memory leg; explicit `--include` keeps single↔batch symmetry.
- **A separate `mm context sync-all` command.** Rejected — the flag
  form shares the include/strict/on-drop/label plumbing and keeps one
  sync surface.
- **Refusing (409/exit-1) on ineligible scopes instead of skip rows.**
  Rejected — a batch over a registry where pausing is a feature must
  treat paused projects as a reported skip, not an error
  (`_run_update_all` precedent).

## References

- ADR-0021 (Portal — the superseded clause carries the rider),
  ADR-0024 (per-phase sync-all this batch loops), ADR-0023 §3 (bounded
  two-roots transfer exception; delegated this supersession),
  ADR-0016 §5 (one tier per write — preserved per-write), ADR-0011 §3
  (project_local has no fan-out), ADR-0023 §10 (error envelope).
- Issue #1279 (A-9), umbrella #1270; #1262 (skip-row classification
  stays client-side); #1263 (user tier is the per-type confirm path).
- Implementation: `web/routes/context_sync_all.py`
  (`sync_all_projects_context`, `_project_skip`, `_summarize_projects`),
  `cli/context_cmd.py` (`_run_sync_all_projects`, `_run_sync_legs`),
  `context/projects.py` (`sync_skip_reason`);
  `tests/test_web_routes_context_sync_all_projects.py`,
  `tests/test_cli_context_sync_all_projects.py`.
