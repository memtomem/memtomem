# ADR-0024: Backend Sync All — per-phase report under one gateway-lock window

**Status:** Accepted (narrowly supersedes ADR-0021 §"Sync orchestration";
resolves ADR-0021 §"Open questions" §2)
**Date:** 2026-06-12
**Context:** Context Gateway completion campaign, mechanism track #1270
item A-8 (#1278). ADR-0021 rejected a backend `POST /api/context/sync-all`
for v1 — the non-reentrant `_gateway_lock` would self-deadlock under
handler reuse, and no single engine models all five result shapes — and
deferred it behind an explicit trigger (`docs/adr/TRACKER.md` row): *a
need for cross-type all-or-nothing sync, or partial-failure pain from the
sequential front-end orchestration*. The campaign owner has signaled that
trigger: partial-failure reporting and cross-project orchestration both
want one backend entry point instead of five sequential per-type POSTs
orchestrated in `context-gateway.js`. This ADR resolves the deferral and
records the shipped endpoint's contracts.

## Decision

### 1. Per-phase report, NOT cross-type all-or-nothing

`POST /api/context/sync-all` runs the five phases (`skills`, `commands`,
`agents`, `mcp-servers`, `settings` — the front-end's order) and reports
each phase's own outcome. A failed phase is recorded and the run
**proceeds**; nothing is rolled back across types.

Cross-type atomicity was rejected, not deferred again:

- Skills fan out via staging-dir promotion, agents/commands via
  per-file atomic `os.replace` (`context._sync_atomic`), settings via
  per-target locked read-merge-write, MCP servers via a single
  `.mcp.json` merge. There is no shared snapshot invariant across those
  four write shapes — a cross-type "rollback" would mean un-promoting
  staged trees and un-merging settings files, i.e. new destructive
  writes, not a restore.
- Per-type `project_shared` atomicity already exists where it matters:
  the fail-fast Gate A phase inside `context._sync_atomic` aborts a
  type's fan-out before its first write.

The front-end orchestrator stops at the first failed phase and marks the
rest `not_run`; the backend deliberately diverges (proceed-and-report) —
phases are independent engines, so a commands failure says nothing about
skills, and the per-phase report preserves exactly the partial-state
visibility the front-end's stop policy approximated. **"Effect parity"
with the front-end orchestrator is therefore a success-path property**
(same writes for the same project/tier when every phase succeeds), pinned
by `tests/test_web_routes_context_sync_all.py`.

### 2. One outer lock window over lock-free per-type cores

`_gateway_lock` is a non-reentrant `_LoopLocalLock`, so the route never
calls the per-type *route handlers*. Each per-type sync handler is split
into:

- a **lock-free core** (`_sync_{skills,commands,agents,mcp_servers,
  settings}_core` in its existing route module) — engine call + error
  translation + response shaping, caller must hold the lock; and
- the standalone route — gate checks + `asyncio.timeout(60)` +
  `_gateway_lock` + core call, byte-identical behavior to before the
  split.

Sync-all acquires the lock ONCE (`asyncio.timeout(300)` — five phases ×
the standalone 60s budget) and runs the cores sequentially inside it, so
a concurrent per-type mutator cannot interleave between phases. Each
core keeps its standalone execution mode: skills/settings offload to a
worker thread because their engines take cross-process sidecar locks
(bounded by `_SKILLS_LOCK_BUDGET_S` / `_SETTINGS_LOCK_BUDGET_S`, far
below every caller's timeout — the #1145 orphaned-worker shape);
commands/agents/mcp-servers stay direct synchronous calls (no file lock,
no unbounded block to offload).

Engine errors leave the cores as `SyncPhaseError(HTTPException)`
(`web/routes/_sync_phase.py`): the standalone routes render the
historical status/detail pair untouched (the privacy 422 keeps its
**string** detail — issue-pinned; strict-drop keeps its dict detail),
while sync-all reads the extra `error_kind` / `reason_code` attributes
to build the embedded envelope without guessing from status codes.

### 3. Response contract

HTTP 200 whenever the phases ran — mixed results cannot map to one HTTP
code. Shape:

```jsonc
{
  "phases": [
    // native per-type body preserved verbatim, plus type/status:
    {"type": "skills", "status": "ok", "generated": [...], "skipped": [...],
     "canonical_root": "..."},
    {"type": "agents", "status": "failed",
     "error": {"error_kind": "validation", "reason_code": "privacy_blocked",
               "message": "...", "http_status": 422}},
    {"type": "settings", "status": "ok", "results": [...],
     "duplicate_tier_warnings": [...]}
  ],
  "summary": {"status": "ok|partial|failed", "ok": 4, "failed": 1,
              "needs_confirmation": 0, "generated_total": 12,
              "skipped_total": 3}
}
```

- **Phase entries embed the native per-type response body verbatim**
  (`generated` / `dropped` / `skipped` / `canonical_root`; settings'
  `results` + `duplicate_tier_warnings`), so the JS consumer's existing
  fragment parsing keeps working after the switchover.
- **Skip-row classification stays client-side (#1262).** The benign-code
  allowlist (`_CTX_BENIGN_SKIP_CODES`) is loud-by-default precisely
  because it lives in the consumer; the server reports raw
  `{runtime, reason, reason_code}` rows and never folds them into a
  verdict. `summary.generated_total` / `skipped_total` are counts, not
  classifications.
- **Failure carriers differ by phase kind.** Artifact phases fail by
  exception → `status: "failed"` + `error` envelope (ADR-0023 §10
  vocabulary, plus `http_status`; dict-detail extras such as
  strict-drop's partial `generated` merge into the envelope). The
  settings phase refuses **in-band** per result row (the `_confirm.py`
  hold-out), so its phase status is a severity roll-up of the rows —
  `error`/`aborted` → `failed` (rows embedded, no `error` key),
  `needs_confirmation` → `needs_confirmation` — mirroring the JS ladder.
- **`summary.status`**: `ok` (every phase ok), `failed` (every phase
  failed), else `partial` (a needs_confirmation-only run is `partial` —
  incomplete until confirmed).
- An unexpected non-HTTP engine error (`OSError` mid fan-out, …) fails
  only its phase, classified through the overview error taxonomy
  (`_classify_exception` + `_redact_message`); route-level non-2xx is
  reserved for the pre-run gates (409 eligibility, 400 tier, 403 CSRF)
  and the outer-timeout 503 (`error_kind: "busy"`, wording makes no
  no-commit claim).

### 4. Tier + selector policy

`?project_scope_id=` resolves through the shared
`resolve_writable_scope_root` dependency, so the sync-eligibility 409s
(`sync_paused` / `sync_not_enrolled`, resolver's pre-existing detail
shape — B-1 #1284 retrofits it onto the envelope) fire before the tier
gate, exactly as on the per-type routes.

`target_scope=project_shared` only:

- `project_local` → 400 — a draft tier with no runtime fan-out
  (ADR-0011 §3), same refusal as every per-type sync.
- `user` → 400 — Sync All stays a project-tier action (#1263): the
  dashboard blocks the button on the user tier, MCP-server sync is
  `project_shared`-only (a user-tier run would carry a permanently
  degenerate phase), and the per-type routes remain the user-tier path
  with their `host_write_gate` disclose-then-confirm round-trip. The
  rejection also means the per-type host-write confirmation can never
  be bypassed through this route.

### 5. No request body in v1

Phases run with engine defaults, mirroring the front-end's body-less
phase POSTs:

- `on_drop="warn"` — strict-drop (`on_drop="error"`) API callers use the
  per-type routes; aggregating an abort-mid-fan-out semantic into a
  per-phase report adds surface for no consumer.
- No `allow_host_writes` valve — on `project_shared` every settings
  generator's target resolves inside the project root
  (`resolve_scope_path` and siblings), so the in-band
  `needs_confirmation` gate cannot fire on the only tier this route
  accepts. The roll-up branch stays implemented as defense-in-depth for
  a future outside-root target; the standalone settings route (which
  does take the flag) is the completion path. The flag joins this route
  only if a reachable case appears.

## Consequences

- The front-end Sync All switchover (replacing the five sequential
  fetches with one `POST /api/context/sync-all`) is a **follow-up**, not
  part of this change (#1278 scope note). Until then both orchestrations
  coexist and produce the same success-path writes.
- MCP Sync-All parity (an `all` alias in the MCP `sync` contract)
  remains a v1 scope-out per ADR-0021 §"Open questions" §5 — unchanged
  by this ADR.
- ADR-0021 §"Sync orchestration — reuse, do not add a backend endpoint"
  is narrowly superseded: its two technical objections are answered by
  the lock-free-core split (deadlock) and the per-phase report (no
  single engine needed). Its TRACKER row is struck in this PR.
- New web mutator bookkeeping: `context_sync_all.sync_all_context` is
  classified in `_CSRF_PROTECTED` and `_REDACTION_EXEMPT`
  (`tests/test_web_invariants_registry.py`).
- Version snapshots taken during a sync-all run carry
  `surface="web_context_sync_all"` — the audit trail names the actual
  orchestrator instead of impersonating the per-type routes.

## Alternatives considered

- **Cross-type all-or-nothing transactionality.** Rejected — see §1: no
  shared snapshot invariant exists across the four write shapes;
  "rollback" would be new destructive writes.
- **Stop-at-first-failure (front-end parity in the failure path).**
  Rejected: the backend report preserves strictly more information; the
  switchover UI can still choose to render later phases however it
  wants, but the API should not discard their outcomes.
- **Per-phase host-write gate aggregation for `target_scope=user`.**
  Rejected for v1: it would re-implement three per-type disclosures plus
  a permanently degenerate mcp-servers phase for a tier the dashboard
  blocks anyway; revisit only with a concrete user-tier orchestration
  need.
- **Front-end-only status quo.** Rejected: the deferral trigger fired
  (campaign owner signal — partial-failure reporting + cross-project
  orchestration want one entry point).

## References

- ADR-0021 (Context Portal — §"Sync orchestration" superseded here,
  §"Open questions" §2 resolved here), ADR-0023 §10 (error envelope
  vocabulary), ADR-0011 §3 (project_local has no fan-out), ADR-0019 /
  ADR-0010 (settings merge semantics).
- Issue #1278 (A-8), umbrella #1270; #1262 (benign-skip allowlist
  contract), #1263 (user-tier write affordance), #1145 (worker-thread
  budget shape).
- Implementation: `web/routes/context_sync_all.py`,
  `web/routes/_sync_phase.py`, the five `_sync_*_core` helpers;
  `tests/test_web_routes_context_sync_all.py`.
