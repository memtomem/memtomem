# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

### Added

- **Public synthetic retrieval benchmark** — a privacy-audited 48-file,
  192-chunk English/Korean corpus, 100-query portfolio, reproducible 0.3.8
  baseline manifest, and blocking quality-floor CI check.
- **LangGraph `BaseStore` adapter** — `MemtomemBaseStore` provides
  file-backed tuple-namespace JSON CRUD, filters, batching, namespace
  listing, and semantic/lexical search via the new `langgraph` extra.
- **Pinned Context** — `mm pinned` and `mem_do` actions manage small,
  privacy-gated Markdown blocks that are composed before retrieved memory
  with deterministic agent/scope shadowing and character budgets.
- **Review-first memory formation** — `mm review` and `mem_do` actions create
  evidence-linked candidates from exact session events and write durable
  memory only after approval. Additive temporal assertion tables preserve
  direction and multiple `supersedes`/`contradicts`/`supports` edges.
- **Interrupted approval recovery** — `mm review recover` and
  `mem_candidate_recover` atomically return stale `writing` candidates to the
  pending queue after a conservative threshold and persist an audited status
  transition without reclaiming fresh approvals. Pre-upgrade NULL claim
  timestamps receive a one-time grace-period backfill; recovery supports
  older SQLite versions without `UPDATE ... RETURNING`, and a completed write
  that loses finalization is quarantined as `write_uncertain` to prevent blind
  duplicate approval.

## [0.3.8] — 2026-07-13

### Added

- **Web search accepts exact source, type, and date filters** (#1729). The
  search endpoint gains `source_exact`, `chunk_type`, `created_from`, and
  `created_before` query parameters, applied as exact matches inside the
  backend query instead of post-filtering results. Date bounds must be
  timezone-aware and are rejected with a 422 when naive or when
  `created_from` is not before `created_before`.
- **Web startup exposes readiness and missing-state contracts** (#1729).
  Storage startup failures are now classified and partially initialized
  components are cleaned up; the server reports readiness, returns 503 while
  it is not yet ready or its state is missing, reconfigures the file watcher
  on change, and surfaces truthful initial-index outcomes — partial and
  failed indexing are reported as such rather than always as success.
- **Web export accepts a `namespace` filter** (#1729), matching the search
  endpoint's namespace scoping.

### Changed

- Claude plugin 0.2.4 now maps to the verified core 0.3.8 release.

### Fixed

- **Hash-based history navigation is repaired** in the web UI (#1729).

### Documentation

- **Public docs now lead with a deterministic first memory round trip.** The
  GitHub and PyPI READMEs, Getting Started guide, and guide index share the
  same `mm init` → `mm status` → `mm add` → `mm search` journey before
  branching into editor setup, existing-note indexing, Context Gateway, and
  operations. Configuration and MCP examples now reflect `config.json` / env
  precedence, recent privacy and Wiki lifecycle commands are discoverable,
  and docs tests pin public links, Quick Start parity, and the runnable smoke
  path.

## [0.3.7] — 2026-07-12

### Security

- **Web uploads are quarantined on disk before adjudication or promotion** (#1722, #1725). The upload route now invokes an explicit multipart parser capped at 32 files and zero text fields, streams every part into an owner-only temporary quarantine, and enforces per-file and aggregate byte limits while copying. Only a fully quarantined batch proceeds to UTF-8 decoding and the privacy guard; accepted files are atomically hard-linked into the upload directory without overwriting concurrent same-name uploads. Parser spools and quarantine artifacts are cleaned on limits, malformed input, decode or privacy rejection, indexing failure, cancellation, and unexpected errors.

- **Release supply-chain checks are now blocking and reproducible** (#1718, #1719). Release preflight validates tag, package, lock, changelog, dependency floors, and built artifacts; production releases publish separate CycloneDX core and all-extras SBOMs. CI enforces OSV exception schema and expiry, full-SHA GitHub Actions pins, plugin mapping consistency, Bandit findings, and dependency scans before release.

### Fixed

- **Historical managed-file rescans fail closed** (#1721). Unreadable files and scan failures are reported as violations instead of allowing a privacy audit to appear successful.

- **SBOM uploads resolve the repository explicitly** (#1723), so release assets are attached correctly when workflow context does not provide an implicit repository.

### Changed

- **Windows tests are split into deterministic shards** (#1724), including both pytest discovery patterns (`test_*.py` and `*_test.py`) so suffix-style tests cannot be silently omitted.

- **Claude plugin 0.2.2 maps to the verified core 0.3.6 release** (#1720). The plugin pin remains on the already-published core until the 0.3.7 production provenance is independently verified.

## [0.3.6] — 2026-07-12

### Security

- **All managed-ingress writers now pass the redaction chokepoint before persistence.** The Notion/Obsidian importers, URL fetcher, and MCP `mem_session_end` summary writer previously wrote to disk and relied on the downstream engine gate to catch secrets — leaving a plaintext file behind on a block. They now call `enforce_write_guard` (scope classified via `classify_scope`) *before* writing: on a `blocked` decision no file is created and the tool reports the block, and all four accept a `force_unsafe` valve. Adjudicated content is then written with `atomic_write_text`/`atomic_write_bytes` at mode `0o600` and indexed with `already_scanned=True` (no double scan). Notion ZIP import no longer extracts a temporary plaintext tree — it validates aggregate/member-size and compression-ratio caps, then reads `*.md` members directly from the archive.

- **Web upload endpoint hardened.** A new `UploadBodyLimitMiddleware` (mounted inside the CSRF boundary) rejects `POST /api/upload` bodies over 201 MiB with a `413` — via `Content-Length` and by counting streamed bytes — before multipart parsing completes. The handler adds a 32-file count cap, a 100 MiB per-file cap, and a 200 MiB aggregate cap, reading each file in 1 MiB chunks; accepted files are written with `atomic_write_bytes` at `0o600`, the upload dir is forced to `0o700`, and failures now log server-side and return a generic `"Upload processing failed"` instead of echoing the exception string.

- **Indexing SSE stream moved to a CSRF-protected POST.** `GET /api/index/stream` is retired (now returns `405`); `POST /api/index/stream` takes an `IndexRequest` body and threads `force_unsafe` through the token-gated path, closing the prior split where bypass runs used a separate route. The front end replaces `EventSource` with a `fetch`-based SSE reader (`fetchIndexStream`) that sends the `X-Memtomem-CSRF` header, aborts on the third consecutive malformed frame (a valid frame resets the count), and requires a terminal event. `GET /api/health` is split into a dependency-free liveness probe and a `POST /api/health` active check.

- **`mm mem rescan-files` — read-only privacy audit of historical managed files.** New CLI command scans `_imported/`, `_fetched/`, and `sessions/` under every index root against the redaction patterns, reports violations/read-errors (with `--json`), changes no files, and exits `1` on any hit.

- **Config secret masking centralized and broadened.** A new `secret_masking` module (`is_secret_key` / recursive `mask_secrets`) replaces the two hardcoded field checks in `mm config show/set` and the MCP `mem_config` tool; any `api_key`, `secret`, or `*_secret_key` field at any depth is now masked to `***`, not just `embedding.api_key` and `langfuse_secret_key`.

- **Session-trace JSONL written with restrictive permissions.** The trace writer now `os.open`s with `O_NOFOLLOW` at mode `0o600` inside a `0o700` parent directory (previously a plain append under the ambient umask).

- **Shipped dependency floors raised for supply-chain posture.** Runtime minimums added/bumped: `cryptography>=48.0.1`, `starlette>=1.3.1`, `idna>=3.15`, `pyjwt>=2.13.0`, `python-multipart>=0.0.27`, plus `urllib3>=2.7.0` on the `onnx`/`langfuse` extras.

- **Notion archive traversal & entry-count guards; asserts replaced with real guards.** Notion ZIP import now rejects archives exceeding an entry-count cap and any member with an absolute path or `..` component before reading. Defensive `assert` statements on the locked chunk edit/delete paths, artifact-diff routes, and `mem_agent_share` idempotency replay were converted to real `raise`/`HTTPException`/error-return branches so the checks survive `python -O` (which strips asserts) instead of silently passing.

### Fixed

- **Windows session traces no longer silently fail** (#1716) — the trace hardening added an unguarded `os.fchmod(fd, 0o600)`, which is POSIX-only and raises `AttributeError` on Windows Python, aborting the write and leaving an empty JSONL file. It is now guarded by `hasattr(os, "fchmod")` (mirroring `provenance.py` and `context/_atomic.py`); `os.open(..., 0o600)` already sets the mode, so nothing is lost on Windows, and POSIX behavior is unchanged.

### Changed

- **Dependency updates** — runtime `python-minor-patch` lockfile group refreshed across 5 packages (#1714); test/CI toolchain bumps for `jsdom` (dev-only, #1713) and the `astral-sh/setup-uv` GitHub Action (#1712).

## [0.3.5] — 2026-07-11

### Added

- **`mm wiki <kind> promote` — import a project canonical into the wiki**
  (#1686) — the wiki ↔ context-gateway lifecycle was one-way: `mm context
  install` / `update` snapshot wiki assets into a project, but nothing moved
  a project asset back into the wiki. `mm wiki {skill,agent,command} promote
  <name>` is the inbound verb for all three kinds: it reads the project's
  `project_shared` canonical (the `untracked` rows `mm context status`
  reports), runs the `enforce_write_guard` privacy chokepoint at
  `scope=project_shared` (a Gate A hit hard-refuses with no bypass — the wiki
  is host-global git history that can be pushed), then under the shared wiki
  commit lock re-checks the asset is absent, copies it in (preserving the
  exec bit), lints, and commits the in-memory scanned bytes so the scan set
  equals the commit set. Holding one lock across absent-check → copy → lint →
  commit serializes concurrent same-name promotes. The project copy is left
  untouched; `mm context install <kind> <name>` snapshots it back as a
  lockfile-tracked asset. Options: `--project`, `--message`.

- **`mm context adopt` — lockfile-track a canonical that already matches wiki
  HEAD** (#1687) — a `project_shared` canonical whose bytes already equal the
  wiki HEAD asset previously could only become lockfile-tracked by
  mv-aside + reinstall (deleting and rewriting identical bytes).
  `mm context adopt {skill,agent,command} <name>` is the explicit, verifying
  fill for install's dest-exists-no-lock refusal: it runs install's own
  reproducible-pin gates, compares dest bytes file-by-file against the HEAD
  manifest (with the copier's skip filters on both sides, including the
  all-skipped parity case), and refuses any difference with a per-file
  categorized report (`differs` / `only on disk` / `only at HEAD` /
  `unreadable`). It deliberately has **no `--force`** — no dest byte is ever
  written or moved — and runs Gate A over the pinned bytes only on full
  equality before recording the install-shaped lockfile entry.

- **The Claude Code plugin now bundles the MCP server** (#1680) — installing
  the plugin previously shipped skills, hooks, and the curator agent but
  still required a separate `claude mcp add`. A plugin-root `.mcp.json`
  (`uvx --from memtomem memtomem-server`, the supported bundle contract) now
  ships so one `/plugin install` activates everything, and the
  `.claude-plugin/` marketplace manifest is no longer gitignored. Every
  skill/agent allowed-tools list is dual-namespaced
  (`mcp__plugin_memtomem_memtomem__*` and `mcp__memtomem__*`) because Claude
  Code suppresses the plugin-managed server when a manually-configured server
  has the same command signature — a single-namespace allowlist would break
  skills for existing `claude mcp add` users. Also corrects the hook
  timeouts from `5000/10000/3000` (interpreted as ~83 minutes) to the
  intended `5/10/3` seconds. Plugin version `0.1.0` → `0.2.0`.

- **Empty-state hint on `mm search` / `mm recall`** (#1675) — a zero-result
  query in `table` or `plain` format now prints a yellow stderr hint
  pointing at `mm status` to confirm the index has chunks. JSON stdout stays
  byte-clean (`[]`), and the `context` format is unaffected.

- **`known_projects.json` read failures now surface on the projects wire**
  (#1699) — `GET /api/context/projects` gains additive `registry_status`
  (`"ok"` / `"unavailable"`, `"unavailable"` only when the registry itself
  can't be read) and a top-level `warnings` list, so a corrupt/unreadable
  registry is no longer wire-identical to "no projects." The web Portal
  renders the warning with a Retry affordance instead of a blank board.

- **Failed count/coverage probes are distinguished from a real zero** (#1692
  PR 5) — per-project entries on `GET /api/context/projects` gain additive
  `counts_unavailable` (the list of kind keys whose count probe raised;
  `counts` stays `null` for those, preserving the wire null convention) and
  `runtime_coverage_unavailable` (bool). A failed per-scope counts probe
  previously left `counts` null and rendered no chips at all — a silent
  variant of the state #1692 made visible; the Portal now stamps the
  unavailable badge + Retry instead.

- **`mm context update --force-head` — follow a deliberate wiki rollback**
  (#1689) — the forward-only guard (#1685) refuses to move a project's pin
  backward after a wiki reset / force-pull, and `--force` deliberately does
  not bypass it. `--force-head` is the explicit escape hatch: it records the
  current wiki HEAD even when it does not descend from the recorded pin,
  warning on stderr **before** anything is written (single asset and `--all`;
  the `--all` preview table tags affected rows `[moves pin BACKWARD]`).
  Orthogonal to `--force` (wiki-side history vs project-side edits — a
  backward move onto locally edited files needs both), and it never bypasses
  the wiki-dirty gate or a lockfile entry with no recorded pin. The web
  update route gains the matching `force_head` request field plus an additive
  `pin_moved_backward` response field (a client cannot derive direction from
  the two commit SHAs).

- **Typed response models on the Context Gateway wire** (#1692) — the
  Overview, Projects, Runtimes, Status All, Sync All (single + cross-project
  batch), and Import (all seven artifact-import routes) responses are now
  validated against Pydantic models (`Context*` components in
  `/openapi.json`) instead of returned as unchecked dicts. The wire shapes
  are unchanged — golden wire fixtures pin key sets, key order, and value
  types before/after, now including the two POST report endpoints — but
  shape drift in a handler now fails loudly (HTTP 500) instead of silently
  changing the wire. One key-order normalization ships along: the batch
  sync's crash-path `error` envelope now orders `error_kind, http_status,
  message` like its timeout sibling and the per-phase envelope (same keys,
  same values).

- **Runtime probe availability is now explicit** (#1692) — a failed
  provider-client probe was previously wire-identical to "no runtimes".
  `GET /api/context/runtimes` gains additive `runtimes_status`
  (`"ok"`/`"unavailable"`) and `warnings` (reason code `status_unavailable`,
  with a redacted message and `error_kind`); `GET /api/context/overview`
  gains additive `detected_runtimes_unavailable`. The web Portal renders an
  explicit "Runtime status unavailable" chip with Retry (and a single
  unknown-state row light) instead of four grey "uninstalled" chips, and the
  overview header shows a "Runtimes unavailable" badge instead of an empty
  chip row. Existing fields, types, and per-entry `error_kind` semantics are
  unchanged.

- **`mm mem init` — project memory tier opt-in** (#1701) — creates
  `<project>/.memtomem/memories.local/` (default; `--scope project_shared
  --confirm-project-shared` for the shared tier) and registers it in
  `indexing.project_memory_dirs`, which previously required hand-editing
  `~/.memtomem/config.json`. Requires a project marker (`.git` /
  `pyproject.toml`); `project_local` specifically requires a git repo (the
  `.gitignore` guard block is written **before** registration and a failed
  write aborts — a pyproject-only project is refused so a later `git init`
  can't start tracking the draft tier). The
  registration append runs inside the config write lock (concurrent
  registrations can't clobber each other) and persists the fragment-merged
  aggregate list. Unregistered project-tier write refusals now point at the
  command first (manual `config.json` editing remains the fallback).
  Deliberately CLI-only: no MCP twin, so an agent blocked by the
  registration gate cannot self-authorize; a running MCP server / `mm web`
  picks up the new tier after restart.

### Changed

- **`mm context update` is now forward-only** (#1685) — `mm context update
  <type> <name>` used to advance the lockfile pin to wiki HEAD whenever HEAD
  differed from the recorded pin, without checking HEAD descends from it, so
  a wiki `reset` / force-pull to older-or-divergent history silently moved
  the pin **backward** (a downgrade). Update now records the new commit as
  the pin only when the recorded pin is an ancestor of the exact commit being
  recorded (the ancestry target is that commit, not a freshly-resolved
  symbolic HEAD, closing a TOCTOU race); missing / unreachable / divergent
  pins all refuse, mirroring `classify_status`'s `stale-pin`. The single wet
  path and `--dry-run` exit non-zero; `--all` skips each stale-pin row
  (counted as a failure → non-zero exit) while forward siblings still update.
  The web update route maps to a fixed `409 pin_not_ancestor` envelope (no
  host-path leak). Deliberately not `--force`-able — the remedy is to fix the
  wiki, or use the `--force-head` escape hatch (#1689).

- **Refreshed `mm web` UI system** (#1698) — a unified visual refresh across
  the core memory, index, and settings workspaces, with hardened mobile
  workspace navigation. No wire changes; the primary flows keep their
  contracts.

- **Remaining UI and Context Gateway UX improvements** (#1704) — completes
  the UI refresh and exposes import provenance. Re-localizes the Simple-mode
  toggle/chip (they painted raw i18n keys when `_ctxApplySimpleMode` ran
  before `I18N.init()` resolved) by registering the renderer on `langchange`
  and re-rendering the active-store chip after project commit. Restores the
  client-side attention-skip demotion in the batch sync-all report path (a
  `parse_error` / `duplicate_name` skip had regressed to rendering as done +
  "Sync completed"), mirroring the legacy toast severity ladder. Splits the
  counts/runtimes portal fetches so one failure can't mask the other, and
  stamps `counts_unavailable` on probe failure so the Retry badge renders.

- **Missing-pin update refusals now report `pin_missing`, not
  `pin_not_ancestor`** (#1689) — a lockfile entry with no usable `wiki_commit`
  pin used to surface as the forward-only guard's 409 `pin_not_ancestor`,
  whose fixed message claims the wiki history "diverged" and the pin "would
  move backward" — neither is true when no pin exists. The engine now raises
  `PinMissingError` (a `PinNotAncestorError` subclass, so existing Python
  catch sites are unaffected) and the web route returns
  `409 reason_code="pin_missing"` with an accurate message (re-install or
  adopt; `force_head` cannot bypass). HTTP status and envelope shape are
  unchanged; clients branching on `pin_not_ancestor` for this rare case will
  observe the corrected discriminator. Both reason codes are pinned by route
  tests.

- **A failed status probe is now an error, not drift** (#1692) — in the
  Context Gateway fleet view (`GET /api/context/status-all`) and
  `mm context status --all-projects`, a per-kind diff scan that *raised* was
  previously folded into the project's `drift` flag. A failed probe cannot
  establish the sync state, so reporting it as (Sync-remediable) drift was
  misleading. Such a project is now classified `error`: the web entry status
  is `error` (its failing kind still carries the error envelope in
  `diff_counts`), and **the CLI now exits 1** for it (mixed drift+error and
  error-only both exit 1), where drift alone still exits 0. Scripts that keyed
  off the previous exit-0 behavior for probe failures should treat the new
  exit 1 as "could not determine sync state." A corrupt/unreadable `lock.json`
  already behaved this way; this aligns diff-probe failures with it.

### Fixed

- **`config.json` section overrides are re-validated against cross-field
  validators** (#1682) — overriding a config section via `config.json` took
  the `setattr` path, which skips the cross-field Pydantic validators
  (Langfuse key pairing, chunk-token range, rerank pool bounds) and does not
  surface user-triggered deprecations. Each overridden section is now
  re-validated; an invalid section reverts to its pre-override baseline. The
  check is validation-only (no coerced model assign-back), so `config.json`
  field types stay as-is and Windows `str()` output is unaffected.

- **Simple-view verdict is truthful for settings drift and local drafts**
  (#1691) — the Context Gateway Simple-view header could report an "all
  good" verdict while settings drift or a local draft was pending; the
  verdict now reflects those states.

- **Context Gateway projects whose status check failed are badged** (#1694) —
  a project whose status probe raised previously rendered indistinguishably
  from a clean one on the Portal board; it now carries an explicit
  failed-status badge instead of a misleading clean/drift state.

- **Unknown `known_projects.json` fields are preserved across rewrites**
  (#1695) — a rewrite of the project registry used to drop any field the
  current schema didn't recognize, silently discarding data written by a
  newer version or an external tool. Unknown keys are now round-tripped
  through the rewrite untouched.

- **Mobile primary workflows stabilized** (#1702) — layout and interaction
  fixes so the core `mm web` workflows are usable on narrow / mobile
  viewports.

- **`namespace_metadata` row is deleted even when the namespace has no
  chunks** (#1706) — deleting a namespace with zero chunks left its
  `namespace_metadata` row behind, so the namespace could still appear in
  listings / metadata after deletion. The metadata row is now removed
  regardless of chunk count.

- **`mm init` next-steps hint points at valid commands** (#1690) — when the
  setup wizard detected memory dirs it could not auto-register, its "next
  steps" output recommended `mm config set indexing.memory_dirs` — not a
  settable field, so following the hint failed. It now points at re-running
  `mm init`, setting `MEMTOMEM_INDEXING__MEMORY_DIRS`, or adding them in the
  Web UI.

## [0.3.4] — 2026-07-05

### Deprecations

- **`mem_context_migrate` MCP tool** (deprecated since #1147, timeline set in
  #1619) — alias for `mem_context_memory_migrate`; forwards every argument
  unchanged. **Removal target: v0.5.0.** Migrate callers to
  `mem_context_memory_migrate` (or the `mem_do` action
  `context_memory_migrate`); the `mem_do` action alias `context_migrate`
  follows the same timeline.
- **`mm init -y` as a wizard-skip alias** (deprecated in #1616, flag split +
  warning in #1631) — `-y` and `--non-interactive` are now separate flags;
  `-y` still implies `--non-interactive` but every use emits a stderr
  deprecation warning. **Behavior-change target: v0.5.0**, when `-y` on
  `mm init` becomes an accepted no-op (init has no confirmation prompt to
  skip). Scripts should use `--non-interactive`, ideally with an explicit
  `--preset`.

### Changed

- **`mm context install` (single asset) is commit-true** (#1643) — install now
  extracts the asset's bytes from the wiki's git objects at HEAD (the same
  mechanism as `install --all`'s pinned restore), so the recorded lockfile pin
  always reproduces the installed bytes. If the asset's wiki working tree
  differs from HEAD (modified/deleted tracked files, untracked files, or a
  never-committed asset), install refuses with `UncommittedAssetError` (CLI)
  / HTTP 409 `wiki_uncommitted` (web) and a runnable `mm wiki <type> commit`
  hint; dirt elsewhere in the wiki does not block. Previously install copied
  the dirty worktree bytes while pinning HEAD — silently recording a pin that
  never contained them. As part of the same fix, committed symlinks are now
  skipped (with a warning) during commit extraction — previously the pinned
  `install --all` restore path materialized the symlink's *target path* as
  file content.
- **`mm context update` is commit-true** (#1652) — update (single asset and
  `--all`) now extracts from the wiki's git objects at HEAD like install
  (#1643), closing the same unreproducible-pin defect on the last verb that
  had it: previously update copied the dirty worktree bytes while pinning
  HEAD. A write refuses with `UncommittedAssetError` (CLI, also on `--all`
  and `--dry-run`, which exit non-zero) / HTTP 409 `wiki_uncommitted` (web)
  when the asset's wiki working tree differs from HEAD; `--force` does NOT
  bypass this (it only overrides project-side edits), and dirt elsewhere in
  the wiki does not block. A no-op update (pin already at HEAD) still
  succeeds and prints an asset-scoped note when the asset has uncommitted
  wiki edits. The repo-wide `_WIKI_DIRTY_WARN` stderr warning is removed —
  its wording ("using HEAD which doesn't include them") described the
  opposite of what update actually did. Behavior change: a pin already at
  HEAD with the asset directory deleted from the wiki worktree now reports
  `unchanged` (exit 0) instead of "not in wiki" — the no-op short-circuit
  runs before the presence gate because nothing would be written. With both
  verbs commit-true, `install --all --force`'s data-safety no-op now
  protects legacy-lockfile rows only; no verb can mint pin≠bytes entries.
- **Catch-all CLI failures now carry a next-step hint** (#1617) — commands that
  ended in a bare `raise click.ClickException(str(e))` (status, add/recall,
  session, plus watchdog, ingest, search, mem, indexing) now append a one-line
  remediation hint for recognized failure classes (locked / "no such table"
  SQLite, embedding dim-mismatch, schema-downgrade, `EmbeddingError`,
  `ConfigError`) via the new `cli/_errors.raise_cli_error`. Already-tailored
  messages and unknown exceptions are unchanged, so no misleading advice is ever
  attached.
- **MMR-without-dense misconfiguration is now visible** (#1619) — enabling MMR
  diversity re-ranking on a BM25-only deployment (no dense vectors to diversify
  over) previously skipped silently. The search pipeline now logs the mismatch
  once per process (INFO, then DEBUG) and `mm status` carries a persistent
  `mmr_disabled_no_dense` warning. MMR defaults to disabled, so neither fires
  out of the box.
- **`mm context` help / status / empty-state wording** (#1647) — `mm context
  --help` is rewritten around the artifact Store + sync model (was the legacy
  `context.md` framing); `mm context status` empty states now hint a runnable
  next command (`mm context init …`, gated on the same project signal
  `init --scope=project_*` requires, with `--include=...` for the user tier);
  the guide documents both privacy valves (`--force-unsafe` for sync,
  `--force-unsafe-import` for init); and status prose standardizes on "tier"
  (the `--scope` flag name is unchanged).

### Added

- **Opt-in model warmup** (#1621) — `MEMTOMEM_WARMUP__ENABLED=true` makes the
  MCP server pre-load the local embedder/reranker models in a background
  task at startup, so the first query doesn't pay the model download/load
  cost; `mm warmup` runs the same preload one-shot for CLI users. Default
  off — the lazy handshake behaviour (#399) is unchanged unless explicitly
  opted in. Remote providers (ollama, openai, cohere) are skipped.
- **Schema-version downgrade fence** (#1614) — the database now records a
  monotonic `schema_version` in `_memtomem_meta`; opening a database written
  by a newer memtomem release fails fast with a typed `SchemaDowngradeError`
  naming both versions and the upgrade command, instead of running
  unknown-structure code paths. Same, older, and pre-versioning databases
  open unchanged — additive idempotent migrations remain the forward
  mechanism; this adds only the downgrade guard.
- **`mm status --format`/`--json`** (#1615) — `mm status`, the post-install
  sanity check, was the one read command with no machine-readable output. It now
  takes `--format [table|json]` with `--json` as the alias for `--format json`
  (mirroring `mm config show`). CLI-classified failures emit `{"error": ...}`
  with exit 0 so `mm status --json | jq` pipelines see them in-band; unexpected
  crashes keep the nonzero exit. The MCP `mem_status` text is byte-identical
  (single-source dict split).
- **`--json` write acks for `mm reset` / `mm purge` / `mm add`** (#1615) — the
  three write-shaped commands now emit the documented `{"ok": true, ...}` /
  `{"ok": false, "reason": ...}` shape under `--json`, so scripts no longer
  scrape styled text. All CLI-classified outcomes (reset's liveness/lock
  refusals, purge's no-match no-op, add's privacy-guard block, prompt
  cancellations) ride the JSON body with exit 0; unexpected exceptions keep the
  nonzero exit. Interactive prompt chrome moves to stderr under `--json` so
  stdout stays a single JSON document.
- **`mm wiki {skill,agent,command} new <name>`** (#1648) — scaffolds a canonical
  wiki asset from a minimal parse-clean template so first-time authoring no
  longer requires reading source; refuses to overwrite an existing asset and
  classifies a directory/file squatting the target path instead of leaking a raw
  OS error. Related behavior change: bare `mm wiki commit <name>` now defaults to
  `--canonical` when the asset has no registered vendor override (announced via a
  note line) — scripts that relied on the bare-invocation error as a guard
  should pass `--canonical` explicitly. Lint now warns when a canonical filename
  matches only case-insensitively (`AGENT.md` vs `agent.md`), which is invisible
  on case-sensitive clones.
- **`mem_context_version(action="enable")` MCP action** (#1650) — headless agents
  can now adopt a flat-layout context artifact into directory layout through the
  existing `mem_context_version` tool, closing the last CLI/web versioning parity
  gap (`mm context version enable` and `POST …/versions/enable` already shipped
  in #1549). It reuses the byte-identical same-scope `adopt_flat_to_dir` rename,
  honors the `scope` arg with the same Gate B as create/promote, and is
  idempotent on dir layout. Previously the only MCP remedy was the heavier
  `mem_context_artifact_migrate`, which skipped hand-authored/UI-created flats.
- **Per-project fleet-drift badges on the Projects portal** (#1649) — each row on
  the web Projects portal now shows a drift badge sourced from the
  `GET /api/context/status-all` cross-project aggregation, which previously had
  no web consumer. The badge is fetched fire-and-forget after the initial paint
  (the endpoint shells out to git per project), reset on every fresh load so a
  tier flip never shows a stale badge, gated on the `project_shared` tier, and
  skipped during inline rename; all fetch failures are swallowed silently.

### Fixed

- **Write-time Gate A block reads as localized, jargon-free copy in the web
  editors** (#1651) — saving a canonical skill/command/agent/MCP-server that
  trips the project_shared privacy gate (#1509) previously surfaced the
  raw-English engine string ("Gate A: … no force bypass available for
  project_shared (ADR-0011 §5) … target_scope=user") verbatim in a localized
  session. Every editor 422 now carries a top-level
  `reason_code: "privacy_blocked"` sibling (the #1409 hoist mechanism; the
  string `detail` is byte-identical), so the web UI shows a plain-language,
  translated hint (en/ko) and keeps the raw English detail in a hover tooltip
  for fidelity. MCP servers, which are project_shared-only, get a hint that
  omits the user-tier remediation. CLI/MCP-tool wording is unchanged.
- **MCP `initialize` instructions now match the exposed tool surface** (#1608) —
  the server instructions string walked every client through direct multi-agent
  tool calls (`mem_agent_register`, `mem_session_start`, `mem_agent_search`, …)
  that do not exist under the shipping default `MEMTOMEM_TOOL_MODE=core`
  (9 tools), so a client following it verbatim called missing tools.
  Instructions are now built per mode: `core` renders the workflow through
  `mem_do(action=..., params={...})` and points at `mem_do(action="help")` +
  `MEMTOMEM_TOOL_MODE`; `standard` shows session/crud tools directly but routes
  multi_agent actions; `full` keeps direct calls.
- **LangGraph `MemtomemStore.start_session()` stale agent binding** (#1620) —
  starting a low-level session after an agent-bound one left the previous
  `start_agent_session()` binding intact, so subsequent `add()` / `search()`
  kept defaulting to the old `agent-runtime:<id>` scope while events logged to
  the new session. `start_session()` now clears the agent binding under the same
  lock, mirroring `end_session()`.
- **`--json` stdout no longer leaks prompt text on Windows** (#1640) — under the
  new `--json` write acks (#1615), an interactive confirm prompt's tail (and,
  under the test runner, the echoed reply) leaked into stdout on Windows —
  click 8.4 only redirects the prompt to stderr on POSIX — breaking the
  single-JSON-document contract. A new `cli/_prompts.confirm` writes the full
  prompt to stderr and reads stdin directly when `err=True`; the reset, add
  Gate B, and upgrade confirm sites migrate to it.
- **`mm context projects add <path>` canonicalizes the root** (#1644) — the
  project root was stored with only `expanduser()` while every read surface
  (dedup, scope-id, display, `is_dir()`) resolves it against the reader's cwd, so
  a relative root like `.` meant a different directory per process and `add .`
  from a second project silently matched the first project's floating entry.
  `add_with_status` now stores `expanduser().resolve()` before dedup/persist, and
  `load()` heals legacy relative rows in-memory (persisted on the next mutation,
  no write on the load path). CLI/web responses now echo the resolved absolute
  root.
- **Wiki-behind badge tooltip recommends a runnable command** (#1645) — the web
  overview "Wiki updates available: N" tooltip told users to run
  `mm context update --all`, which exits 2 (`update` requires `ASSET_TYPE NAME`;
  `--all` is the cross-project axis, not "update the N behind assets in this
  project"). Both locales now lead with the tier-portable per-asset flow
  (`mm context status` → `mm context update <type> <name>`) and qualify the
  Wiki-section button as dev-mode; the same unrunnable spelling is fixed in
  `--help` text and the `context/status.py` module docstring.
- **Gateway display polish batch** (#1646) — five web display fixes from the
  post-campaign gateway audit: the overview all-clear badge no longer renders a
  cross-axis "4/1 synced" fraction (spells out both axes when they differ, keeps
  `N/N synced` otherwise); single-item import-skip toasts map the backend
  `reason_code` to localized copy instead of surfacing raw English; runtime
  chips/badges brand the full set (Claude Code, Antigravity, Codex, Kimi) and
  render the fan-out's internal `project_mcp` id as its `.mcp.json` target;
  sync-phase failure toasts use localized keys instead of English literal
  fallbacks; and Default Tab selector option labels and optgroup headings are
  localized.

## [0.3.3] — 2026-07-04

### Added

- **`mm context init --only NAME`** (#1520 item 4) imports a single named
  runtime artifact — the CLI twin of the web's per-artifact Import action.
  Requires exactly one `--include` kind (skills, agents, or commands) and
  runs import-only: `context.md`, sibling directory seeding, and the
  `.gitignore` append are skipped. Exits 1 with a clear message when no
  runtime artifact matches; `--scope=project_local` is rejected (the draft
  tier has no runtime fan-out to import from); Gate A/B apply unchanged. The
  engine already supported single-name narrowing (`only_name=`) for the
  web routes — this wires it into the CLI.
- **MCP memory-write tools accept an `idempotency_key` for safe retries;
  batch append is atomic** (#1596). `mem_add` / `mem_batch_add` / `mem_edit`
  / `mem_delete` take an optional `idempotency_key`: a retried or duplicated
  call with the same key replays the stored result (tagged `[idempotent
  replay]`) instead of double-writing, and a concurrent in-flight claim
  returns a retryable "already in progress" message. `mem_batch_add` now
  appends the whole batch atomically.
- **`mm context version delete-label` and `mm context version enable`**
  (#1549) close the web/MCP parity gaps — deleting a version label and
  enabling versioning for an artifact are now reachable from the CLI, not
  only the web UI.
- **`mm context update --dry-run`** (#1547) prints the same four-state
  (update / unchanged / refuse / error) preview as a real run and stops
  without writing anything.
- **`mm reset --backup`** (#1595) opt-in snapshots the DB to a timestamped
  `<db>.pre-reset-<ts>.bak` sibling (via the stdlib `sqlite3` backup API,
  never overwriting an existing snapshot) before wiping.
- **The web overview surfaces a "wiki update-available (behind)" badge**
  (#1546) for the lockfile ↔ wiki staleness axis, so a divergent pin no
  longer hides behind a green "synced" state.
- **In prod, the browse-only wiki renders an inert install affordance**
  (#1604) — disabled buttons plus a note naming the `--dev` remediation —
  instead of a silent dead-end where the install/update routes are not
  mounted.

### Security

- **The web canonical create/update editors for skills / commands / agents
  now run the write-time Gate A privacy scan** before any byte reaches the
  git-tracked `project_shared` canonical, matching the mcp-servers editor: a
  secret typed or pasted into the editor is refused with the path-free
  privacy 422 (count + artifact name only) instead of landing in git and
  only being caught at the next sync/import. `force: true` still bypasses
  only the mtime guard, never the scan; user-tier saves are unscanned by
  design (not git-tracked, `allow_host_writes`-gated, sync-time valve
  intact — ADR-0011 §5). The six handlers moved to `_REDACTION_PROTECTED`
  in the web invariants registry. (#1509)
- **Quoted-JSON credential labels and the `x-amz-security-token` wire label
  are redacted — forward-sync of memtomem-stm#562 / memtomem-stm#561.** Two
  more STM-origin secret-class rules mirrored forward in one pass so the sets
  move together. (1) *Quoted-label generalization* (memtomem-stm#562): the
  generic label rules end in `\s*[:=]`, and a quoted key's closing quote sits
  between the label and the colon, so `"password": "hunter2"`,
  `"api_key": "sk-…"`, camelCase `"accessToken": "ya29.…"`, and dict-repr
  `{'password': 'hunter2'}` — the exact shape of a pasted `docker inspect` /
  `kubectl get secret -o json` / DB-config note — crossed the write boundary
  unredacted. One general quoted-label rule reuses the #553 FP-guard shape
  (quote directly on both sides of the label, value must open as a string, so
  JSON-Schema object values, embedded labels, and prefixed keys never fire);
  `pwd` is deliberately excluded — shell/file tools legitimately emit
  `"pwd": "/home/user"` working-directory fields. (2) *AWS wire label*
  (memtomem-stm#561): botocore DEBUG logs emit the `x-amz-security-token`
  request header verbatim and every presigned URL generated with temporary
  credentials carries `X-Amz-Security-Token=…`, but `session[_-]?token`
  cannot cross the `security-token` spelling, so those notes scanned clean
  unless an `ASIA…` key ID co-occurred. The new rule's unquoted branch
  carries a separator-only left boundary (`(?<![_.\-])`): kebab/dotted
  compounds that merely name the header (`forward-x-amz-security-token:
  true`, `proxy.headers.x-amz-security-token`) stay negative, while
  bytes-repr wire dumps — which render the newline before the header line as
  a literal `\r\n`, putting an alphanumeric directly before the label — stay
  positive. The two sides are byte-identical again at 19 patterns, same
  order (STM pin memtomem-stm@`67689db`); both patterns translate cleanly to
  the Web UI's client-side JS scan (position-0 `(?i)` lift + fixed-width
  lookbehind, ES2018+), and each gets its paired JS-translation parity
  fixture. A content-hash pin over the shared subset is tracked in
  memtomem-stm#559.
- **AWS secret material is redacted by label (`SECRET_ACCESS_KEY` /
  `SESSION_TOKEN`) — forward-sync of memtomem-stm#553.** The redaction guard
  caught AWS key **IDs** (`AKIA`/`ASIA`) but not the secret **material** those
  IDs unlock: `secret[_-]?key` needs its two words adjacent
  (`secret_access_key` splits them) and `access[_-]?token` needs the literal
  `access` (`session_token` has neither), so an STS AssumeRole JSON or an
  `env`-dump note could cross the write boundary unredacted. One new
  `DEFAULT_PATTERNS` rule (STM-origin, mirrored forward — the inverse of the
  #1488→#1491 reverse-sync) with two alternatives: a quoted-key form
  (`"SessionToken": "…"` — STS JSON / dict repr / kebab-case serialized
  headers; the quote must sit directly on both sides of the label and the
  value must open as a string, so JSON-Schema properties and prefixed keys
  never fire) and an unquoted label form (`AWS_SECRET_ACCESS_KEY=`,
  `aws_session_token =`, TOML `aws.secret_access_key =`, namespaced
  `TF_VAR_aws_secret_access_key=`) carrying a left boundary so identifiers
  that merely embed the label (`get_session_token:`,
  `supports_session_token:`, `rotateSecretAccessKey:`) don't trip the guard.
  The two sides are byte-identical again at 17 patterns (STM pin
  memtomem-stm@`5ab5467`); the pattern translates cleanly to the Web UI's
  client-side JS scan (fixed-width lookbehinds, ES2018+).

- **Bulk folder indexing now enforces the secret-redaction trust boundary
  (ADR-0006 PR-A).** Previously `mm reindex`, the Web UI Index / Sources flows
  (`trigger_index`, `reindex_all`, `memory-dirs/add` auto-index, `index_stream`),
  the file watcher, `mem_index`, `mem_fetch`, and file import (`mem_import_*`) all
  pulled files straight into the store without the secret-class redaction scan
  that single-file upload, `mem_add` / `mem_edit`, and JSON import already ran —
  so a stray API key in a bulk-indexed folder crossed the write boundary
  silently. The gate now lives at the `IndexEngine._index_file` chokepoint that
  every indexing entrypoint funnels through: secret-bearing files are skipped
  (not indexed) and reported via a new `blocked_files` / `blocked_paths` count on
  the index response, the SSE `complete` event, and the `mm index` summary. Bulk
  indexing continues past a flagged file instead of aborting the whole run.
  Pass `mm index --force-unsafe` (audit-logged) to index flagged files anyway;
  the bypass is hard-refused for the git-tracked `project_shared` tier. Content
  that already passed a write-ingress guard (`mem_add` / `mem_edit`, upload,
  chunk edit, …) is not re-scanned, so no existing write path regresses.

- **`mm index --debounce-window` / `--flush` and the LangGraph `index()` tool
  now surface secret-blocked files instead of silently discarding them
  (ADR-0006 PR-A follow-up).** The debounce queue's indexer closure called
  `IndexEngine.index_path` but never inspected the returned `IndexingStats`,
  so a secret-bearing file correctly skipped by the redaction gate was
  reported as `Indexed` and dropped from the queue with no retry — the file
  was never stored, but the hook caller had no way to know. The drain result
  now surfaces a redaction-blocked file (`IndexingStats.blocked_files`) as an
  `Errors` entry and leaves the path queued for retry, matching `mm index`'s
  direct-run behavior. Terminal non-security skips (too-large / binary files)
  keep draining silently as before, so they don't accumulate in the queue. The
  LangGraph integration's `index()` tool had the same reporting gap — an agent
  calling it could never learn a file was blocked — and now returns
  `blocked_files` / `blocked_paths` / `errors` alongside the existing stats.

- **The Web UI now shows secret-blocked files and can override the bulk-index
  redaction gate (ADR-0006 PR-B).** PR-A skipped secret-bearing files during
  bulk indexing but the web frontend never displayed the `blocked_files` count,
  so a folder with a stray API key indexed as a green "success" with the file
  silently dropped. Folder indexing, per-directory reindex, "Reindex all", and
  Sources "+ Add path" now surface how many files were skipped (a result row on
  the Index tab plus a toast). An **"Index without privacy gate"** checkbox on
  the Index tab (folder mode) and the Sources "+ Add path" row brings flagged
  files in anyway (audit-logged) — except git-tracked `project_shared` files,
  which stay hard-refused; those are messaged to remove the secret or move the
  file to a local scope. Because bypassing the redaction gate is a security
  downgrade, the override runs through the CSRF-protected `POST /api/index` (a
  one-shot run, no live per-file progress) rather than the token-exempt indexing
  SSE stream. Mirrors the existing `mm index --force-unsafe` CLI escape hatch.

- **New Settings → Redaction panel surfaces the secret-redaction counters in the
  Web UI (ADR-0006 PR-B audit surface).** Previously the process-lifetime tally
  of how many writes the redaction gate passed, blocked, or bypassed
  (`force_unsafe`) was only reachable over MCP (`mem_add_redaction_stats`). A new
  read-only `GET /api/privacy/stats` endpoint and a Settings → Redaction section
  (Runtime group) now render the outcome totals plus a per-write-surface
  breakdown, so an operator can audit redaction activity without an MCP client.

- **The interactive shell `index` command, the watcher startup backfill, and the
  `mm init` wizard seed now name redaction-blocked files (ADR-0006 PR-A
  follow-up).** A sweep of the remaining `IndexEngine` bulk callers found three
  surfaces still swallowing the PR-A `blocked_files` aggregates: the shell and
  the backfill reported only a count — no paths, no scope guidance, and
  non-redaction per-file errors (too-large / binary / backend failures) not at
  all — and the `mm init` seed dropped the signal entirely, so a secret-bearing
  file in a seeded folder vanished behind the green "Seeded initial index" line
  (or, with every file blocked, behind a misleading "check logs for upsert
  errors" + embedding-reset hint). The shell and the seed now print the same
  blocked-file summary `mm index` shows — paths, `--force-unsafe` guidance where
  it applies, the `project_shared` hard-refusal note — plus the non-redaction
  ERROR lines, via reporters shared by all three CLI surfaces
  (`cli/_index_progress.py`) so that messaging cannot drift; the watcher
  backfill (a log surface, not a terminal one) now names the blocked paths in
  its warning and aggregates non-redaction errors to one line per directory.
  `mm index` output is unchanged.

- **Canonical-path disclosure sweep completed (#1412 follow-through).**
  Absolute filesystem paths are now redacted in MCP context-tool errors
  (#1539) and success-path messages (#1599), across all web kind routes
  (#1538), in the index SSE error event (#1527), and in settings-sync
  reason/target/dup-tier emissions (#1556) — so a server-side path can no
  longer leak to an MCP client or the browser.
- **Wiki `git show` reads go through the redaction boundary, and remote/ref
  values are pinned as positional args** (#1544), closing an argv-injection
  vector where a crafted URL or ref beginning with `-` could be read as a
  git flag.
- **The Gate-A blocked-file audit surface is threaded through wiki
  install/update to the web route** (#1528), giving wiki writes the same
  secret-redaction audit visibility as canonical writes.

### Changed

- **Claude-plugin skill `/memtomem:context` is now `/memtomem:recall`**
  (#1520 item 6). The skill injects relevant memories as context for the
  current task; its old name collided with the context-gateway subsystem
  (Skills / Commands / Agents / `mm context …`), which the plugin does not
  drive — `recall` says what it actually does. Invoke `/memtomem:recall
  [topic]` after updating the plugin; no gateway-driving plugin skill is
  planned for now.

- **Config sections no longer bind bare, unprefixed environment variables**
  (#1522). The 22 sub-config sections (`embedding`, `storage`, `llm`,
  `session_trace`, …) plus `NamespacePolicyRule` were `BaseSettings` classes
  without an `env_prefix`, so
  generic shell exports like `API_KEY`, `ENABLED`, `MODEL`, or `HOST` silently
  overrode memtomem configuration — including secret-bearing fields
  (`embedding.api_key`, `session_trace.langfuse_secret_key`) and validator
  guards — outside the documented `MEMTOMEM_` surface. They are now plain
  pydantic models: environment binding flows exclusively through
  `MEMTOMEM_<SECTION>__<FIELD>`. Validation strictness is unchanged — unknown
  keys (an env typo like `MEMTOMEM_EMBEDDING__TYPO`, or a stray key in
  `config.json`/`config.d`) still fail loudly, exactly as before.

  **Migration**: if you relied on a bare name, add the documented prefix —
  e.g. `API_KEY=sk-…` → `MEMTOMEM_EMBEDDING__API_KEY=sk-…` (or
  `MEMTOMEM_LLM__API_KEY=sk-…`). One deliberate exception: with
  `session_trace.langfuse_enabled=true`, the Langfuse SDK's own
  `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` (and `LANGFUSE_HOST`) still
  count as credentials — they are read by the SDK itself and are never copied
  into memtomem config. `LANGFUSE_ENABLED` alone never turns tracing on.

- **Web + MCP read-path work is offloaded off the event loop** (#1597) so
  context-gateway and project reads no longer block the async event loop
  under concurrent load; the "no await under lock" invariant is preserved.

### Removed

- **The reserved config flag `context_gateway.user_tier_enabled` is gone**
  (#1520 item 3). It was declared forward-compat for the multi-project
  context UI RFC's PR3, which never shipped — the flag had zero read sites,
  so it never gated anything. A stale key left in `~/.memtomem/config.json`
  or a `config.d` fragment is skipped silently (both loaders guard unknown
  field names); the one breaking edge is the env var
  `MEMTOMEM_CONTEXT_GATEWAY__USER_TIER_ENABLED`, which now fails startup
  validation — unset it. If PR3 lands, the flag returns wired to real
  read sites.

### Fixed

- **`mm upgrade` now stops a running `mm web` too, not just the MCP server**
  (#1569). The command exists to prevent the "on-disk bytes swapped but a live
  process keeps running the old version" split-brain, but its liveness probe
  only covered the server pid files — a backgrounded `mm web` (a live
  SQLite/WAL writer holding `web.pid` under the same flock contract) survived
  the reinstall and kept serving the previous version against the shared DB.
  The upgrade plan now probes `web.pid` alongside `server.pid`, stops the web
  UI with the same SIGTERM → grace → SIGKILL ladder, sweeps its `web.json`
  metadata sidecar on the SIGKILL path, and reports every stopped pid;
  `--dry-run --json` lists the web pid in `would_kill`/`would_remove`. On
  Windows the manual-stop warning now names `mm web` as well.
- **`mm init`'s `.mcp.json` / Kimi `mcp.json` writers are atomic and refuse
  invalid pre-existing files with an actionable message** (#1568). Both
  editor-config writers used truncate-then-write, so a crash mid-write could
  corrupt the editor's MCP config for *every* configured server, and they
  parsed a pre-existing file unguarded, so a hand-edited `.mcp.json`
  (trailing comma, comment) aborted `mm init` at its final step with a raw
  `JSONDecodeError` traceback — after `config.json` was already written,
  leaving the run half-applied with no hint how to recover. The merge now
  goes through the fsync-hardened `atomic_write_text` (tempfile +
  `os.replace`, preserving the existing file's permission bits and writing
  through a symlinked config to its resolved target instead of replacing
  the link), and an unparseable existing file — or one whose top level or
  `mcpServers` value is not a JSON object — is refused with a
  "fix or remove it and re-run mm init" error, leaving it byte-for-byte
  untouched.
- **Concurrent MCP memory-CRUD calls on the same file no longer lose updates or
  corrupt an unrelated entry** (#1570). `mem_edit` / `mem_delete` did their
  read → rewrite (`replace_chunk_body` / `remove_lines`) → re-index → rollback
  with nothing serializing the span: two overlapping calls on one file each
  captured the same pre-image and a stale `start_line`/`end_line`, so the
  second write clobbered the first, or — if the first changed the file's line
  count — spliced over a *different* entry; a failing re-index then rolled the
  file back to the caller's own pre-image, reverting a concurrent committed
  edit. Each markdown-mutating tool (`mem_edit`, `mem_delete`, `mem_add`,
  `mem_batch_add`) now holds a per-file `asyncio.Lock`
  (`AppContext.get_memory_file_lock`, keyed on the resolved *case-folded* path
  so two spellings of one file on a case-insensitive filesystem share one
  lock) across its whole span, and the edit/delete paths re-fetch the chunk
  *under* the lock so the line range is never stale. The bulk
  `mem_delete(source_file=)` / `mem_delete(namespace=)` branches take the same
  lock(s) — sorted acquisition across the namespace's file set — so an
  in-flight locked span's re-index can no longer resurrect the rows a bulk
  delete just removed; and the session-derived namespace for
  `mem_add`/`mem_batch_add` is resolved inside the lock, so an entry lands
  under the namespace active at write time rather than one captured before
  the lock wait.
  This serializes concurrent CRUD **within one MCP server process** — the
  issue's realistic vector (several agents sharing one server). The tool layer
  deliberately does not also take the engine's sidecar `_file_lock`: that lock
  is re-acquired inside `index_file` and `portalocker` contends between file
  descriptors even in one process, so nesting it would self-deadlock. Cross-
  process races (a second MCP server, the CLI, `mm web`) and
  CRUD-vs-`memory-migrate` are now serialized too — see the cross-process CRUD
  fix (#1588 / #1589 / #1591) below.
- **`mem_session_end` no longer double-writes the summary on a retried or
  concurrent call** (#1571). Only the final state reset was guarded by
  `_session_lock`; the effectful phase — `end_session`, the billable
  auto-summary LLM call, and the archive summary-chunk write+index — ran
  unlocked before it, so a client retry (or two agents sharing the session)
  re-read the same `current_session_id` and re-ran the whole phase. The session
  is now *claimed* under `_session_lock` at entry (its id recorded in
  `_ending_session_ids`), so a second/retried end returns "No active session."
  and the effectful phase runs at most once (matching the claim-then-run
  contract from #1564); the claim is not released on mid-phase failure, and an
  orphaned active row is reaped by the existing stale-session path. The public
  `current_session_id` / `current_agent_id` handles stay set through the phase
  and are cleared only when it completes — separating the claim from the handle
  so a concurrent session-bound write during the multi-second teardown still
  routes to `agent-runtime:<id>` and a concurrent `mem_scratch_set` still binds
  to the ending session (and is reaped by its `scratch_cleanup`) instead of
  silently falling back to the default/global scope.
- **Deleting or renaming a watched markdown file now removes its chunks from
  the index immediately** (#1566). The file watcher had no `on_deleted`
  handler and `on_moved` enqueued only the new path, so a deleted or
  renamed-away file's chunks lingered in `chunks`/`chunks_fts`/`chunks_vec`
  and stayed searchable until the opt-in orphan-compaction pass ran — with the
  scheduler disabled (the default), effectively forever. This has a privacy
  edge: "I deleted the file" did not mean "it left the memory index". The
  watcher now enqueues deletes and both sides of a move; `IndexEngine.index_file`
  treats a path that no longer exists — deleted, renamed away, or replaced by a
  directory — as delete-by-source, reusing the same primitive as the
  orphan-cleanup backstops. Cleanup is deliberately *not* blocked by exclude
  patterns (the orphan sweep already purges excluded orphans, so the live path
  matches — otherwise a deleted-and-newly-excluded file's content would persist
  forever). Safety brakes: the delete fires only on a genuine missing-file
  error, never a transient `EACCES`/`EIO` blip; a wholesale loss of a watched
  root/volume is left to the periodic two-pass mass-orphan brake rather than
  mass-deleted per event; and the delete pass never resurrects a removed parent
  directory via the sidecar lock. Orphan compaction remains the backstop for
  events missed while the watcher is down.
- **Concurrent memory-CRUD writes across separate processes no longer lose
  updates or clobber a concurrent edit** (#1589, #1591). A shared
  re-fetch-under-lock + rollback contract now covers the web chunk-PATCH and
  CLI edit paths (a second MCP server, `mm web`, and the CLI): a contended
  lock returns 503 / a `ClickException`, and a concurrent index migration
  returns 409 rather than corrupting the file.
- **`config.json` read-modify-write is serialized across processes** (#1588)
  via a `portalocker` sidecar lock, so overlapping `mm config` / `mm init` /
  web settings writes no longer lose each other's changes.
- **`upsert_entities` is atomic** (#1583) — a mid-write failure can no longer
  leave a chunk's entities DELETEd-but-not-replaced; the DELETE rolls back.
- **Indexing rejects a short/truncated embedding array instead of zipping it
  against the wrong chunks** (#1563), preventing silent embedding/text
  misalignment.
- **Ollama embeddings retry on a transient 5xx/429 (e.g. a 503 while a model
  reloads), namespace-meta upsert is atomic, and a scoped session-id error is
  swallowed only in-scope** (#1594).
- **A poison file can no longer stall the debounce indexing queue** (#1593):
  the watcher caps a poison debounce entry, logs a full queue, and locks the
  index stream.
- **The scheduler claims a schedule atomically before dispatch** (#1564), so
  an overlapping tick can't double-run the same job.
- **Orphan compaction uses a two-pass scan with a mass-delete brake** (#1565),
  so a transiently-missing watched mount can't wipe every chunk under it.
- **`.mcp.json` read-merge-write is locked and mtime-guarded** (#1532), so
  concurrent context installs no longer corrupt or drop editor MCP entries.
- **Settings copy aborts cleanly when a destination file is deleted mid-apply**
  (#1524) instead of leaving a partial write.
- **`mm context install --all --force` no longer silently swaps pre-digest
  legacy clean rows** (#1600).
- **Context transfer previews are honest, and the no-op partial-move sync hint
  is dropped** (#1551).
- **Editor `.bak` backups no longer dirty the wiki working tree** (#1552).
- **Wiki action buttons get a double-submit guard and install-scope pinning**
  (#1553).
- **The wiki commit stale-confirm dialog z-order is fixed and the
  privacy-block install error is localized** (#1554).
- **The `context_skills` routes close two non-UTF-8 parity gaps** (#1537).
- **`mm context generate` / `sync` exit non-zero when they refuse a missing
  `context.md`** (#1536) instead of exiting 0 silently.
- **Context status no longer labels a diverged wiki pin "behind"** (#1535).
- **Wiki edits preserve the executable bit on commit** (#1534).
- **The web and wiki degrade gracefully on an unborn-HEAD (no-commits) wiki**
  (#1543) instead of erroring.
- **A detached-HEAD wiki commit is classified as `WikiDetachedHeadError`**
  (#1525) — a clear error instead of a raw traceback.
- **Wiki `ls-tree` parsing preserves non-ASCII asset paths** (#1521).
- **The default wiki path resolves at call time, not import time** (#1507), so
  a HOME/config set after import is respected.
- **The version-route sidecar lock is bounded and its engine work is
  offloaded** (#1523), so a stuck lock no longer hangs the web request.
- **`mm upgrade` warns when a non-pid-file writer still holds the DB** (#1607)
  via a warn-only `BEGIN IMMEDIATE` probe after the pid-file holders are
  stopped, surfacing a split-brain the pid files can't see.
- **`mm reset` refuses to run while a server/web process is live or any writer
  holds the DB** (#1595), preventing a wipe under a live writer.
- **The enable-versioning route returns the same invalid-name error envelope
  as its sibling version routes** (#1558).
- **`mm context migrate` hints emit the plural asset type** (#1526).
- **The wiki dirty-dot tooltip points at a remediation that exists in prod**
  (#1548).

## [0.3.2] — 2026-06-30

A security release. The Web UI now validates `Host`/`Origin` on every `/api/*`
request — closing a DNS-rebinding read exposure (GHSA-2vm9-7v7j-hq68) — and the
import/redaction trust boundary is hardened with provenance-aware bundle import
(#1483), Notion-import ZIP resource caps (#1489), and an expanded secret-class
redaction pattern set (#1492). Plus a per-project agent-team search argument and
a round of Context Gateway / namespace UI wording clarity. One behavior change to
know up front: importing a *foreign* JSON bundle that contains a secret now fails
by default (override with `force_unsafe` after review).

### Added

- **`mem_agent_search` gains a `shared_namespace=` argument for per-project agent
  teams (ADR-0028).** With `include_shared=True` the read path still defaults to
  merging the single global `shared` bucket; passing
  `shared_namespace="shared:<project>"` re-points only the *shared* leg of the
  merge to a project-scoped bucket, so multiple agent teams running against one
  server no longer pool every project's shared notes together. The argument is
  validated through `validate_namespace` and is ignored when
  `include_shared=False`, and the private per-agent leg is untouched. The
  convention (project-prefixed `agent_id` + `shared:<project>`) is documented in
  the server instructions and the agent-memory example notebook (#1476).

- **The Namespaces tab now documents the provider → namespace prefix mapping
  in-app.** A collapsed-by-default help disclosure between the Namespaces header
  and the list decodes the labels users see — `claude:<project>`, `codex:*`,
  `gemini-memory:<slug>`, `agent-runtime:<id>`, and the exact, colon-less
  `shared` and `default` names — without leaving the app, localized in en/ko
  (#1449).

### Changed

- **Context Gateway section copy was standardized and the wiki-vs-Store boundary
  made explicit.** The per-section descriptions for Skills, Commands, Subagents,
  MCP Servers, and Hooks now follow one template (function + where it is stored +
  what it syncs to) so they read at consistent depth; the Overview's duplicated
  Store/Sync orientation was de-stacked (the always-on description is now
  action-first while the dismissible primer keeps sole ownership of the
  conceptual model); and new copy plus a help-tip distinguish the host-global
  wiki library (`~/.memtomem-wiki`) from the per-project Store (`~/.memtomem/`) —
  a wiki Install writes only the project `.memtomem/` Store, never
  `~/.memtomem/config.json` or the User tier (#1451, #1452).

- **Network MCP transports now surface their no-authentication posture at bind
  time.** Starting `memtomem-server` with `--transport sse|http` prints a "no
  first-party authentication on this transport" notice (treat as trusted-network
  only; front it with an authenticated reverse proxy before exposing it
  publicly), and the `--help` epilog states the same. No transport default or
  behavior changed (#1485).

### Fixed

- **`mm context install --all --force` no longer silently swaps clean rows back
  to committed-HEAD bytes.** A row installed from a dirty wiki working tree pins
  HEAD yet holds bytes the recorded pin never described; under `--all --force`
  such a row re-classified clean and was re-extracted from the pinned commit with
  *no* `.bak` — a silent destructive overwrite, and the only `--force` path that
  left no backup. A clean row whose recorded digests differ from the bytes the
  pin would extract is now left untouched (tallied as skipped, not installed);
  when they already match, `--force` still reconciles stale dest-only leftovers.
  Data-safety only — ADR-0008's working-tree-install behavior is intentionally
  unchanged (#1479).

- **The project-local Context tier badge shows the de-jargoned annotation even
  before translations load.** The badge's cold-boot fallback still rendered the
  old "(no runtime fan-out)" literal while the localized value reads "(not synced
  to runtimes)"; the fallback is now synced so the text is consistent whether or
  not i18n has resolved (#1465).

- **The keyboard-shortcut help reflects the "More" → "Settings" tab rename.** The
  shortcut list still read "Home=1 … More=8"; it now reads "Settings" to match
  the renamed 8th tab. Copy-only — the 8-tab count and digit range are unchanged
  (#1462).

### Security

- **The Web UI now validates `Host`/`Origin` on every `/api/*` request,
  including read-only `GET`/`HEAD`.** The Host/Origin guard previously ran only
  for unsafe methods, leaving GET read routes (`/api/export`, `/api/search`,
  `/api/sources/content`, …) with no DNS-rebinding defense: a malicious page open
  while `mm web` runs could read the indexed memory corpus via `GET /api/export`.
  The check now applies to all `/api/*` requests (OPTIONS excepted — CORS owns
  preflight); the CSRF *token* requirement stays scoped to unsafe methods. The
  standalone `memtomem-web` console entrypoint is now loopback-only, matching
  `mm web`'s bind policy. (GHSA-2vm9-7v7j-hq68)

- **JSON bundle import now enforces the secret-redaction trust boundary
  (ADR-0006 Axis F.3).** `POST /api/export/import` and the MCP `mem_import`
  tool previously upserted every bundle record without the redaction scan that
  guards all other write surfaces — the sole batch-write ingress with no gate.
  Imports are now provenance-aware: a bundle this install exported (carrying a
  valid local-provenance HMAC marker, written by `mem_export` / the export
  endpoint) round-trips unchanged, while a foreign or unverifiable bundle is
  scanned per-record — across its full retrievable surface (content, heading,
  source path, and tags, since every field of a foreign bundle is
  attacker-controlled) — and the whole import is rejected if any record contains
  a secret-shaped value. Pass `force_unsafe=true` (the MCP argument or the import
  form field) to override after review — the bypass is audit-logged.
  **Behavior change:** importing a foreign bundle that contains a secret now
  fails by default where it previously imported silently; the web surface
  returns HTTP 403 `redaction_blocked`. (#1483)

- **Notion ZIP import now enforces resource bounds (decompression-bomb guard).**
  `import_notion()` previously called a bare `extractall`, which enforces no
  aggregate-size, per-member-size, file-count, or compression-ratio limit, so a
  few-KB crafted archive could expand to gigabytes and fill the disk; the path is
  MCP-reachable via `mem_import_notion`. A new `safe_extract_zip()` validates
  archive metadata up front (entry count, per-member and aggregate uncompressed
  size, compression ratio) and rejects any member whose name resolves outside the
  extraction root, failing closed on traversal / absolute / root-entry names
  rather than silently normalizing them. A `member_filter` lets the Notion
  importer extract only `*.md`, so large attachments are neither extracted (the
  bomb vector) nor cause a false rejection of the whole export (#1489).

- **The secret-class redaction guard at the LTM trust boundary now covers
  currently-issued provider-token formats.** The legacy `sk-[A-Za-z0-9]{20,}`
  rule did not match modern OpenAI keys (`sk-proj-` / `sk-svcacct-` /
  `sk-admin-`) or any Anthropic key (`sk-ant-`) — the hyphen after the class word
  halts the alphanumeric run before the minimum length — so the most common keys
  issued today slipped through. Seven prefix-anchored, near-zero-false-positive
  patterns were added (modern OpenAI, Anthropic, the `gho`/`ghu`/`ghs`/`ghr`
  GitHub token family, Google API keys, GitLab PATs, Hugging Face tokens, and
  PyPI/TestPyPI upload tokens), each bounded so `scan()` stays linear-time.
  **Behavior change:** content carrying one of these token shapes is now blocked
  at ingress where it previously passed (#1492).

## [0.3.1] — 2026-06-24

A first-time-user experience pass over the web UI, plus a non-ASCII tag-storage
fix. No CLI, MCP, or HTTP API surface changed: every default flip is reversible
(a saved choice always wins) and the full power-user surface stays one
**Advanced** toggle away.

- **A brand-new install now opens on an orientation-first Home instead of an
  empty Search box.** The first no-hash visit lands on the Home tab (#1440),
  which leads with a collapsible "Getting started" block walking the
  add → search → connect journey (#1439), and a one-time three-step wizard
  guides that same path over Home on a genuine first run (#1444). First-run is
  detected by scanning `localStorage` for any app-owned key, so deep-links and a
  saved default tab still win and a returning user is never misread as fresh; the
  Search empty state also gains a dismissible welcome card that explains
  Sources / Chunks / Memories (#1437).

- **A Simple view is the default; the power-user surface is one toggle away.** A
  global-header Simple/Advanced toggle (persisted per browser, Simple by default)
  demotes the Tags and Timeline tabs and the Settings → Data group behind
  Advanced, with a single visibility predicate gating every navigation path (hash
  dispatch, popstate, arrow-keys, command palette, default-tab clamp) so a
  demoted surface can never strand the user (#1441). A subtle 1px divider marks
  the core / advanced split in the tab row, vanishing with the advanced tabs in
  Simple mode (#1446).

- **The Index tab defaults to the intuitive "New memory" (compose) mode for fresh
  installs** rather than the more technical Folder scan; a saved
  `memtomem.index.mode` is still honored, so the flip is reversible (#1438).

- **The search surface no longer greets first-time users with retrieval-engine
  jargon.** The "BM25 (keyword) + Dense (semantic) → RRF fusion" help banner and
  the "Top-K · BM25+Dense · RRF k=60" status line are replaced with
  plain-language summaries ("Up to 10 results · Hybrid search"); the raw score,
  retrieval-source badge, and RRF-math tooltip move behind the existing "Advanced
  details" expander, and long auto-namespaces render as friendly
  `provider: …/tail` labels with the full id preserved in `title`/`aria-label`
  (#1434, #1435). The acronyms now live single-source in a glossary (namespace,
  token, MCP, TTL, search pipeline) surfaced as in-context help tips where each
  term first appears, guarded by a test that keeps the search copy jargon-free
  (#1445).

- **More of the UI speaks Korean.** The Config panel is fully localized — field
  labels, section titles, status lines, and the right-hand guide — with en/ko
  parity enforced by a test (the deep `MEMTOMEM_*` env snippets stay English by
  design, under a localized one-line summary) (#1443), and high-frequency
  search/sources micro-copy (result counts, tag hints, the chunk browser, editor
  word counts) now routes through the translation layer (#1436).

- **The Home orientation's Gateway step now says what the Gateway actually
  does** — syncs your skills, subagents, and commands out to coding agents —
  rather than implying it reuses your memories (that is Search) (#1442).

- **Fixed: non-ASCII tags (Korean, emoji, …) are stored and displayed correctly
  instead of as literal `\uXXXX` escape text.** The write path serialized the
  `> tags:` line with `ensure_ascii=True` while the reader hand-split the JSON
  array without decoding it, so a Korean tag round-tripped as its escape sequence
  into the database. The writer now uses `ensure_ascii=False`, the parser
  JSON-decodes first (with a lenient fallback for legacy shapes), and a one-shot,
  idempotent migration repairs already-indexed escaped tags in place —
  recomposing emoji surrogate pairs via UTF-16 and skipping un-encodable rows
  rather than crashing startup — so existing databases self-heal on the next
  start without a re-embed (#1433).

- **Fixed: corrected the stale `rerank.top_k` deprecation notice.** The field
  comment and both `DeprecationWarning` messages still predicted removal "in 0.3"
  even though the field remains in 0.3.x (migrated to `min_pool` with a warning);
  they now read "a future release". No behavior change (#1430).

- **Maintenance: bumped runtime dependencies** — pydantic-settings
  2.14.1 → 2.14.2, openai 2.41.1 → 2.43.0, fastapi 0.137.0 → 0.138.0, and
  langfuse 4.7.1 → 4.9.1 (#1432).

## [0.3.0] — 2026-06-20

- **Security: bump vendored DOMPurify 3.4.10 → 3.4.11 (GHSA-cmwh-pvxp-8882).**
  The pinned `web/static/vendor/purify.min.js` carried a MODERATE advisory —
  permanent `ALLOWED_ATTR` pollution via `setConfig()` bypassing the hook
  clone-guard, fixed upstream in 3.4.11. Refreshed the file, its SHA-256 pin in
  `THIRD_PARTY_LICENSES.md`, the README fetch recipe, and the `?v=` cache-bust.
  memtomem uses DOMPurify with the default config (no `setConfig()` attribute
  hooks), so exposure was low, but this clears the `vendored-assets` OSV gate.

- **Web UI: the Context Gateway now opens in a comprehension-first Simple
  view by default (ADR-0026, headline of 0.3.0).** First-run no longer drops
  you into the four-axis (artifact / tier / runtime / scope) control grid.
  Simple renders a one-line verdict for the active project — "Everything is in
  your tools.", "Some items aren't in your tools yet — sync to push them out.",
  "Some items need your attention — open Advanced to review.", or "Nothing is
  stored for this project yet." — above a per-type row list where each fixable
  row carries a single inline action (Sync to push a stored copy out, Import to
  pull a runtime's copy back in) that runs the same confirm flow as Advanced,
  clean rows show a check, rows with no safe one-click fix keep a **Manage**
  deep-link, and copies living in another tier are summarized as "Stored
  elsewhere". An onboarding layer frames it: a plain-language primer ("memtomem
  keeps your master copies in one **Store** (`.memtomem/`) and Syncs them out to
  your **Runtimes**… Import pulls a runtime's copy back in. It's one-way: edit
  in the Store, then Sync."), a **Store ── Sync → Runtimes** flow diagram, a
  status legend, and glossary help-tips on the Runtimes and tier vocabulary.
  The flip is **reversible** by construction: Advanced (today's full UI,
  byte-identical) is one **Simple view** toggle away and the choice persists
  per browser, and the global default can be set back with a single
  `_CTX_SIMPLE_DEFAULT` constant — the deferred P2 Push/Pull re-frame is
  *not* part of this. Alongside it, the gateway's first-run vocabulary was
  de-jargoned: the user-facing **Enroll** action is now **Activate** ("Project
  activated for sync"), raw scope IDs and "fan-out" no longer leak into
  tooltips, storage-vs-hooks-target wording is split, friendly tier labels
  (User / Project (shared) / Project (local)) replace the internal scope names,
  and the nav glossary is harmonized (e.g. "canonical" is now defined and
  "Custom" is dropped from the commands type label).

- **Context Gateway: cross-project / cross-tier artifact transfer engine —
  `move` and `copy` across all four surfaces (ADR-0023; #1273–#1276, #1283,
  #1289, #1314).** One engine (`transfer_artifact`) now moves or copies a
  single canonical artifact (`agents` / `commands` / `skills`) between tiers
  (`user` / `project_shared` / `project_local`) and/or between projects, faced
  by the CLI verbs `mm context copy` and `mm context move`, the web
  `POST /api/context/{kind}/{name}/transfer` route behind a per-artifact
  "Move / Copy" destination modal, and the headless `mem_context_artifact_transfer`
  MCP action — all sharing one gate contract. `move` consumes the source and
  cleans its stale runtime fan-out; `copy` never touches the source and takes
  `--as NEW_NAME` for a renamed copy. Destination projects are restricted to
  the registered discovery set and selected by `p-<sha12>` scope_id (a typed
  filesystem path is consent and is CLI-only); paused or never-enrolled
  destinations refuse. Destination collisions always refuse with no `--force`
  valve, a `project_shared` landing runs the privacy scan (Gate A) and demands
  `--confirm-project-shared` with `--apply`, and the default is a dry-run
  preview. On a shared→shared transfer the install-provenance `lock.json` pin
  carries over so `mm context status` / `update` keep working at the
  destination (gated — a missing or pre-digest pin is skipped, not minted).
  `asset_type=mcp-servers` rides the same surfaces through a constrained
  copy-only modal (cross-project only, no `--as`, tiers fixed at
  `project_shared`), and destination runtime fan-out is never generated — every
  successful transfer prints the follow-up `mm context sync` command to run.

- **Context Gateway: cross-project multi-project operations — registry,
  bulk sync, and fleet drift (ADR-0025, #1272/#1278/#1279/#1280/#1292).**
  The multi-project registry (`known_projects.json`) now has a first-class
  CLI face and two cross-project batch verbs, all gated to the
  `project_shared` tier:
  - **`mm context projects` group** — `list` (add `--json` for the
    `GET /api/context/projects` payload), `add <path>`, `remove <selector>`,
    and `pause`/`resume <selector>` flip the per-project sync-enrollment flag
    that the batch verbs and the web Sync gate honor. A selector is either a
    `p-<sha12>` scope_id (from `list`) or a filesystem path; `add` is
    idempotent and reports `Already registered:` on a re-add.
  - **Bulk sync** — `mm context sync --all-projects` and
    `POST /api/context/sync-all-projects` fan every per-type phase into every
    eligible project under one gateway-lock window. Ineligible projects are
    reported and skipped (missing root, paused enrollment, discovery-only, or
    no `.memtomem/` store), an all-skipped run still reads as success, and a
    failed project's already-written phases still count. `--force-unsafe`
    does **not** apply to `--all-projects`.
  - **Fleet drift** — `mm context status --all-projects` and
    `GET /api/context/status-all` answer "which of my projects drifted" in one
    read-only call (no gateway lock); each entry is `ok`/`drift`/`skipped`/
    `error` and the summary is counts only, no roll-up status string.
  - **`POST /api/context/sync-all`** runs every per-type sync phase for ONE
    project under a single gateway-lock window, returning HTTP 200 with
    `{phases, summary}` so mixed per-phase outcomes live in the report.
  - **Add-Project transparency** — `POST /api/context/known-projects` returns
    a `created` flag; the Add Project UI branches its toast so a no-op re-add
    reads as "Project already tracked" rather than a fresh "added" success.
  None of these touch any tier other than `project_shared`: the user tier is
  one global store and `project_local` has no runtime fan-out to drift.

- **Context Gateway: manage user-scoped skills/agents/commands, with a
  host-write confirm and a user-tier force-unsafe valve (#1263, #1379,
  #1386, #1380, ADR-0011 §3/§5).** The skills/agents/commands write,
  sync, and import routes now accept `target_scope=user` alongside
  `project_shared`, so the gateway can create, update, delete, fan-out, and
  import canonical artifacts under the host user store (`~/.memtomem/<kind>/`
  and the `~/.claude`-family runtime roots). Because those paths live outside
  any project root, every user-tier write goes through a disclose-then-confirm
  round-trip: the first request writes nothing and returns HTTP 200 with
  `status: "needs_confirmation"`, `confirm: "allow_host_writes"`, and the exact
  `host_targets` list, which the dashboard surfaces as a "Write outside the
  project?" dialog before re-sending with `allow_host_writes=true` — idempotent
  no-ops (nothing to import, empty canonical sets, missing-name deletes) never
  prompt, and cheap 404/409 conflicts are refused before the gate. A reviewed-
  bypass valve mirrors the CLI's `--force-unsafe-import` for the same tier:
  `force_unsafe_import` on the import routes (#1379) and `force_unsafe_sync` on
  the sync routes (#1386) let a confirmed false-positive secret (e.g. an
  `api_key: str` annotation) proceed — but **only on the `user` tier**;
  `project_local` is rejected outright and `project_shared` hard-refuses
  regardless of the flag (git history is permanent, ADR-0011 §5). For the one
  artifact a shared import can't accept, `POST /context/skills/{name}/import-to-user`
  (skills only) reads the project runtime and writes the user library, so a
  reusable skill flagged by the shared-tier gate has a home; the dialog and the
  shared-import "Import to user library" hint route there.

- **CLI: `mm context seed-validation <dir>` — hidden first-run seeder
  (ADR-0026 §Validation).** A hidden QA helper that seeds a fresh project with
  the six Context Gateway first-run affordances (out-of-sync, not-imported,
  empty, MCP orphan, MCP parse-error, in-sync) so the onboarding user test is
  reproducible from an installed wheel — previously the seeder lived in
  `tests/fixtures/` and was unreachable by anyone who only `pip install`-ed
  memtomem (the prerequisite for an unmoderated async run). The seeder logic
  moved to `memtomem.context._validation_seed` (shipped under `src/`); the
  command refuses a non-empty target directory unless `--force`, so it can never
  overwrite a real project. No change to any user-facing behavior — the command
  is hidden and writes only into the directory you point it at.

- **Web UI: wiki "uncommitted changes" badge at the nav/glance level
  (ADR-0008).** A saved-but-not-committed wiki edit is invisible to `mm context
  install`, which installs committed git objects only — yet from the Context
  Gateway sidebar there was no signal that the wiki had edits that won't reach
  projects yet (the in-section `#wiki-head` badge only shows once you open the
  **Wiki** section). The Wiki nav entry now carries a small dirty dot driven by
  the same `is_dirty` state, kept live by the existing save/seed/commit
  responses and refreshed by a lightweight `GET /api/wiki/status` probe on
  Context Gateway open (HEAD + `git status` only — so a cold reload with a
  wiki left dirty in a prior session flags it without first opening the
  section). Legibility only — no change to install/sync behavior. The dot is a
  screen-reader-labelled indicator with no live region (it is never announced as
  it appears).

- **CLI: `mm wiki remote` / `push` / `pull` — wiki backup & cross-device sync
  (ADR-0008).** The wiki (`~/.memtomem-wiki`) has always been a normal git repo,
  but until now the only product affordance was the one-time `mm wiki init --from
  <url>` clone — backup and sync meant dropping to raw `git`. `mm wiki remote
  [<url>]` shows or sets the `origin` backup remote; `mm wiki push` and `mm wiki
  pull` are **thin pass-through** wrappers over `git push`/`git pull origin
  <branch>`. They surface git's own errors verbatim (non-fast-forward, merge
  conflict, dirty tree) and own no merge/conflict resolution or fast-forward
  policy — divergent histories follow your own `git pull` config, exactly like
  any private repo (no new sync protocol). Credentials embedded in a remote URL
  are redacted from all displayed output and error messages (they still persist
  in `.git/config` — prefer SSH keys or a git credential helper). Restoring onto
  a fresh machine stays `mm wiki init --from <url>`.

- **CLI: `mm wiki {skill,agent,command} commit` (ADR-0027 §3).** The terminal
  parity of the in-browser wiki **Commit** affordance. After editing a canonical
  asset or a vendor override (e.g. with `mm wiki skill override <name> --vendor
  <v> --editor`), `mm wiki skill commit <name> --canonical --vendor claude` writes
  **one isolated git commit** of only the selected paths layered onto HEAD — never
  a bare `git add . && git commit`, so unrelated staged changes in the wiki are
  left untouched. Select targets with `--canonical`/`-c` and repeatable
  `--vendor`/`-v`; `--message`/`-m` overrides the generated default. Bytes already
  matching HEAD are a no-op (nothing committed); a concurrent `mm web` commit, a
  moved HEAD, or a file that changed mid-commit aborts cleanly so you can re-run.
  The commit message is privacy-scanned (a soft, non-blocking warning). The web
  route and CLI now share one commit engine (`memtomem.wiki.commit.commit_targets`,
  including the cross-process wiki lock) so the two surfaces cannot drift.

- **Web UI: dev-tier override-seed in the wiki browser (ADR-0008 PR-E E-2).**
  The Context Gateway **Wiki** section gains a per-vendor **Seed override** action
  (dev mode only) — the web parity of `mm wiki <type> override`. It renders the
  canonical baseline into `~/.memtomem-wiki/<type>/<name>/overrides/<vendor>.<ext>`
  for you to edit and commit; it never auto-commits, so the new file is left in
  the working tree (the HEAD badge flips to "uncommitted changes"). Re-seeding an
  existing override is gated behind a confirm and keeps the previous content as a
  `.bak` sibling. The new `POST /api/wiki/{type}/{name}/override` mounts only in
  `mode=dev` (`web/routes/wiki_mutations.py`); a non-renderable vendor (the
  `commands`/`codex` placeholder) → 400, a missing canonical → 404, and an
  existing override without `force` → 409 — never a traceback. The read-only
  browser stays prod-tier and unchanged.

- **Web UI: read-only wiki browser (ADR-0008 PR-E).** The Context Gateway gains
  a **Wiki** section that browses the global `~/.memtomem-wiki` repo — the
  canonical skills, agents, and commands shared across projects. Pick an asset
  to see, per vendor, its override **diff** against the freshly rendered
  canonical and a **lint** report (canonical parse, stray override files,
  per-vendor representability). It is read-only: seeding overrides and editing
  stay on the `mm wiki` CLI. Because the wiki is host-global (not per-project),
  the section carries no project/tier control bar, and a vendor with no
  generator (the `commands`/`codex` placeholder) is shown disabled rather than
  offered as a control that would fail at render time. New routes `GET
  /api/wiki`, `.../{type}/{name}/diff`, `.../{type}/{name}/lint` are prod-tier;
  an absent wiki returns a structured onboarding state, never a traceback.

- **Web: the Context Gateway Wiki section is now editable in the browser
  (ADR-0027 Editor-A/Editor-B/§3, ADR-0008 PR-E E-3).** In dev mode
  (`MEMTOMEM_WEB__MODE=dev`) the global wiki (`~/.memtomem-wiki/`) pane gains
  the full author loop without leaving the browser: edit the base **canonical**
  (`GET`/`PUT /api/wiki/{type}/{name}/canonical`, Editor-B) or a vendor
  **override** (`GET`/`PUT /api/wiki/{type}/{name}/override`, Editor-A), seed an
  override from canonical, then **Commit…** (`POST
  /api/wiki/{type}/{name}/commit`, web parity of `mm wiki … commit`). Save and
  Commit stay two acts — Save writes the file and leaves the wiki dirty but
  **never commits**, and the Commit button only appears after a Save. Commit
  builds an *isolated* git commit of only the server-resolved typed targets
  (canonical / named vendor overrides), guarded by an `expected_head`
  compare-and-swap and a per-target `mtime_ns` token, so a commit landing
  underneath or an external same-path edit is refused with a precise 409 rather
  than clobbering. A separate **Install** / **Update** affordance (`POST
  /api/context/{type}/{name}/install` and `…/update`, parity of `mm context
  install`/`update`) snapshots a wiki asset into the current project's
  `.memtomem/` at wiki HEAD; install refuses an already-installed asset (use
  Update), and a force-update keeps each locally edited file as a `.bak`
  sibling. The editors, commit, seed, and install/update all mount **dev-tier
  only** — the **read-only wiki browser stays prod-tier** and is unchanged. The
  pane is reached via the new **"Global wiki"** scope chip (#1406) and a global
  Wiki nav section separated from the per-project context groups (#1414); a
  nav-level badge flags uncommitted wiki edits that `mm context install` (which
  reads committed git objects only) would not yet reach. Canonical edits to
  subagents/commands must still parse or the save is rejected; override and
  canonical content is privacy-scanned as a non-blocking warning, never refused
  (single-curator host-global store).

- **`mm wiki` exposes the Kimi vendor; `--vendor` Choices derive from the
  matrix (ADR-0008).** `mm wiki skill|agent {override,diff,lint} --vendor kimi`
  now works. Kimi skills/agents have had `OVERRIDE_FORMATS` rows and renderers
  (`kimi_skills` / `kimi_agents`) since PR-D, but the `mm wiki` CLI hard-coded
  every `--vendor` Choice to `claude|gemini|codex` and silently dropped kimi.
  Each verb's Choice is now derived per asset type from `OVERRIDE_FORMATS`
  (`override_vendors`), so it can never drift from the matrix again: kimi is
  offered for skills and agents but not commands (Kimi has no commands surface),
  and codex stays the documented placeholder for commands.

- **`mm wiki <type> {diff, lint}` — inspect wiki overrides (ADR-0008 PR-D; #1332).**
  Two read-only verbs round out the per-asset `mm wiki skill|agent|command`
  group. `diff <name> --vendor <vendor>` renders the canonical the way
  `override` would seed it and prints a unified diff against the committed
  `overrides/<vendor>.<ext>`, surfacing both your hand-edits and any canonical
  drift since seeding (always exits 0; canonical fields the vendor format cannot
  carry are noted on stderr). `lint <name> [--vendor <vendor>]` validates the
  asset is well-formed and installable — name, canonical presence + parse, and
  per-vendor representability + override UTF-8 — exiting non-zero on any error so
  it is usable as a CI gate while dropped-field warnings stay exit 0. Both reuse
  the PR-C `render_seed_bytes` / `OVERRIDE_FORMATS` machinery, so
  `diff` / `lint` / `override` never disagree about what the runtime sees.

- **`mm context sync --include=mcp-servers` — CLI mcp-servers fan-out (#1311).**
  `mm context sync` (and `--all-projects`) gained an opt-in mcp-servers phase:
  `--include=mcp-servers` fans canonical `.memtomem/mcp-servers/*.json`
  definitions into the project's `.mcp.json` via the same canonical-wins-per-name
  merge the web Sync runs (reusing `generate_all_mcp_servers`, so CLI and web
  stay byte-parallel). It is **opt-in** — a bare `mm context sync` never touches
  `.mcp.json` — and **sync-only**: `mcp-servers` is rejected by
  detect/init/generate/diff, which have no mcp-servers engine. Because
  `.mcp.json` holds executable MCP-server config, the `--all-projects` batch
  confirms per target before each rewrite (the count-only "Sync N projects?"
  gate does not disclose it); a single-project run treats the explicit
  `--include` as consent. `--scope` is a no-op note (mcp-servers is single-tier
  project_shared). The cross-project `mm context copy mcp-servers` follow-up now
  prints this as a runnable `cd <dst> && mm context sync --include=mcp-servers`
  command instead of web-only prose. The MCP `sync` contract (`mem_context_sync`)
  still rejects `include=mcp-servers` — that parity and an `all` alias remain an
  ADR-0021 §"Open questions" §5 scope-out.

- **Cross-project copy for MCP server definitions (ADR-0023 §12).**
  `mm context copy mcp-servers <name> --to-project <scope_id|path>` and the
  existing transfer route (`POST /api/context/mcp-servers/{name}/transfer`)
  copy one canonical `.memtomem/mcp-servers/<name>.json` into another project
  through the transfer engine's stage → Gate A → promote path — the privacy
  scan always runs (the destination is the git-tracked project_shared tier;
  `env` blocks are the usual hotspot), destination collisions hard-fail, and
  the promote refuses to clobber atomically even against writers outside the
  sidecar locks. The copy is stricter than artifact transfers in two ways:
  the staged bytes must parse as a valid stdio server definition (one broken
  canonical aborts the destination's entire mcp-servers sync phase), and
  symlinked canonicals are refused (the destination must be a regular
  git-tracked file).
  Results carry a runnable `sync_command` (and disclosure notes for a
  same-name `.mcp.json` runtime entry the destination's next sync would
  overwrite) that fans the copied canonical into the destination's
  `.mcp.json` — `cd <dst> && mm context sync --include=mcp-servers` (the CLI
  sync phase landed in #1311, below; #1282).

- **Cross-project per-hook copy for settings hooks (ADR-0023 §11).** New
  `mm context settings-copy --event … --matcher … --to-project <scope_id|path>`
  and `POST /api/context/settings/hooks/copy` copy ONE canonical-matched hook
  entry into another project. The copy is durable by construction — it writes
  the destination's canonical `.memtomem/settings.json` (so the destination's
  own syncs maintain the rule) and the destination-tier Claude settings file
  (ownership-stamped, live immediately); other runtimes pick the entry up on
  the destination's next settings sync (the exact command is printed).
  Idempotent re-runs are no-ops, same-matcher conflicts are skipped with the
  colliding entry named, and the privacy scan (Gate A) runs for every
  destination tier with no force valve. Companion fix: settings sync now
  re-reads the canonical under the per-target lock, so a concurrently
  in-flight sync can no longer prune a rule another gateway writer just
  landed (#1281).

- **Fixed: Context Gateway, wiki, and export/import correctness &
  robustness.** A roundup of user-notable fixes not implied by a feature entry
  above.
  - *Privacy gates fail closed on an unreadable subtree.* The skills-import
    Gate A scan (#1381) and the sync/transfer `scan_artifact_tree` scan (#1393)
    enumerated the staged tree with `Path.rglob`, which silently swallows a
    per-directory `OSError` — an unreadable subdirectory vanished from the file
    list yet its bytes were still promoted **unscanned**. Both now walk with a
    fail-closed iterator that raises `PrivacyScanReadError`, so the caller's
    rollback runs instead of leaking an unscanned subtree.
  - *Privacy-block 422s no longer leak host paths.* The sync and import
    `project_shared` Gate A blocks returned a 422 whose `detail` echoed an
    absolute `.resolve()`d path (host `$HOME` + OS username) over the loopback
    dashboard; they now emit fixed, path-free constants (#1387), as does the
    mcp-servers parse-error 422 (#1413) and the settings-copy block (#1395). The
    install/update project-root guard maps to a 404 envelope instead of a raw
    error (#1391).
  - *`mm context status` survives an unreadable installed subtree.* An
    unenumerable subdirectory in a project's installed tree used to crash the
    whole multi-project status walk; the dirty walk now classifies that asset
    DIRTY (with a warning) rather than 500-ing, while update/`--all` apply still
    refuse before any `.bak`/copy so no partial, backup-less mutation occurs
    (#1383).
  - *Context Gateway "Sync All" isolates per-phase failures.* A recoverable
    per-phase HTTP error (e.g. a first-phase privacy 422) no longer aborts the
    remaining artifact types — each phase reports independently and only a
    transport failure stops the run (#1399). The force-unsafe sync confirm now
    counts **unique files**, not per-runtime skip tuples, so it no longer claims
    "4 files contain secrets" when one file across four runtimes is affected
    (#1400). The import 422 toast shows the localized user-tier hint alone, and a
    Korean josa resolver renders the correct particle for interpolated names
    (#1401).
  - *Two data-loss / drift fixes.* `mm wiki init` now rolls back a wedged
    half-created `.git/` when the bootstrap commit fails (e.g. no resolvable git
    identity) so re-running is not blocked (#1392); the all-"exact"
    `settings-migrate` batch rechecks the target's mtime before stripping the
    source, so a concurrent external edit can no longer drop a hook entry from
    both tiers (#1382); and export→import now embeds `retrieval_content` (the
    heading-hierarchy-prefixed text the index engine embeds) so a roundtripped
    chunk retrieves identically to a natively-indexed one (#1394).

## [0.2.4] — 2026-06-04

Patch release on top of 0.2.3: a multi-project **Context Portal**,
Langfuse-style **artifact version snapshots + label pointers** for agents and
commands (ADR-0022), per-project **sync enrollment**, a new `mm tags` CLI, CLI
session tracing on Langfuse v4, and broad Web UI / i18n / accessibility
hardening — alongside a session-trace secret-redaction fix and retry/webhook
reliability fixes.

- **Secrets are now redacted in session-trace metadata sinks.** Session command
  tracing wrote span/trace metadata to its observability sinks without running
  the redaction pass, so a secret-shaped value captured in command context could
  reach the trace backend in clear text. The metadata now goes through the same
  redaction funnel as the rest of the trace payload.

- **Context Portal — multi-project state board (ADR-0021).** The Context Gateway
  gains a read-only runtime/client registration registry, a multi-project board
  section, per-CLI runtime chips with a provider filter, project health + label
  rename + lazy artifact counts, and a "Sync All" flow with per-phase progress, a
  result summary, and (project, tier) pinning so each phase re-resolves against
  the intended scope. The gateway now lands on the Projects Portal by default
  (#1191–#1193, #1195–#1197, #1200–#1202).

- **Artifact version snapshots + label pointers for agents and commands
  (ADR-0022).** Canonical agents/commands can keep Langfuse-style
  `versions/vN.md` snapshots with movable label pointers (e.g. `production`,
  `staging`) instead of a single flat file. New MCP tools `mem_context_version`
  and `mem_context_promote`, plus label-aware `mem_context_sync`, expose this
  over MCP; the Web UI adds a detail-panel version/label manager, list-card label
  chips (`GET /context/{type}?include=versions`), a versioned create layout, and
  an opt-in "enable versioning" migration that adopts existing flat web-created
  artifacts into the versioned layout (#1206, #1212–#1216, #1218).

- **Per-project sync enrollment.** Project discovery is split from sync: matching
  projects are auto-displayed, but artifacts only fan out once a project is
  explicitly enrolled. Adds enroll / pause / resume UI, sync-eligibility gating
  on the matrix and Sync-All, and a write-guard that returns a structured `409`
  (with localized detail) on runtime-writing routes for paused or not-enrolled
  projects (#1203, #1205, #1208, #1210, #1211).

- **`mm tags` — list / rename / delete / merge tags (#688).** New CLI verb with a
  matching Tags tab in the Web UI for the same rename / merge / delete actions.
  Dry-run sampling is now global so the preview sample matches the count and the
  applied set (#1175–#1177).

- **CLI session command tracing + Langfuse v4 (#1199).** CLI command runs can
  emit session traces through a new `observability/session_tracing.py`, updated
  to the Langfuse v4 client surface.

- **Kimi Code install detected via the `~/.kimi-code` home dir (#1204).** The
  install probe also checks `~/.kimi-code` (the CLI data home set by
  `KIMI_CODE_HOME`), fixing an ADR-0021 false-negative where a present Kimi
  install was reported as absent.

- **Retry and webhook reliability fixes.** `with_retry` now honors string
  `retry_after` attributes instead of ignoring them (#1178), and
  `_validate_webhook_url` URL-safety behavior is pinned by tests (#1179).

- **Web UI accessibility + UX consolidation.** Context Gateway roster
  consolidation (Overview matrix removed, Sync routed through the Projects
  Portal), active-project + tier controls hoisted into one gateway header bar, a
  single capability-mapped source for artifact-section toolbars, artifact lists
  scoped to the active project with a "Show all projects" toggle, Sync/Import
  dry-run confirm previews with count/destination threading, and a11y fixes
  (settings-nav `<nav>` landmark + `aria-current`, un-nested project-remove `×`,
  labeled Hooks controls) (#1217, #1219–#1223).

- **Localization (EN/KO).** Command palette labels, the Context Gateway
  create-form name placeholder, Search history and recent chips, settings
  recovery messages, Decay scan status, Index streaming progress, and the
  embedding-mismatch banner are now localized (#1184–#1190).

- **Internal.** Unused `noqa` directives removed across src and tests; validated
  `TargetScope` casts replace return-value `type: ignore`s; the gc orphan-project
  report renderer is typed (#1180–#1183).

## [0.2.3] — 2026-06-01

- **`mm memory doctor` — read-only hygiene report for memory stores.** A
  registered `memory_dir` can be barely indexed (the fs watcher only reacts to
  live events, so files that landed while the server was down stay invisible
  until a forced re-walk) and its index/TOC file can drift from what's on disk
  — `mem_search` then silently can't find the un-indexed files. The new
  `mm memory doctor` makes the 3-way drift between disk, the agent index file
  (e.g. `MEMORY.md`), and the searchable DB visible: per configured dir it
  reports DB coverage gaps, DB chunks whose source file was deleted, index/meta
  files indexed as content, broken index links (classified
  `missing_target`/`outside_root`, leaving `url`/`anchor` alone), files absent
  from the TOC, hot-cache budget overruns, and never-accessed "cold" files.
  Human output by default, `--json` for CI; exits `1` only on definite
  inconsistencies (deleted-source chunks, convention violations, broken links).
  Report-only by default — the report never writes disk, DB, or config (loads
  config with `migrate=False` so it can't trigger the legacy auto-discover
  rewrite). A narrow, opt-in `--fix` then removes *only* broken
  `missing_target` index links (`outside_root`/`url`/`anchor` left alone),
  dry-run by default with `--apply` to write — byte-exact line-splice under a
  sidecar lock with fresh re-validation, removals count-bounded to what the
  report saw, per the ADR-0020 subtractive write contract (#1172, #1173).

- **Provider index-file conventions enforced on every index path (fixes
  `MEMORY.md` pollution).** The exclude set for agent index/TOC files — Claude
  Code's `MEMORY.md`/`README.md` and Codex's `README.md` — was previously
  honored only by `mm ingest`, so the general `memory_dirs` walk, the file
  watcher, and `mm index <dir>` indexed `MEMORY.md` as searchable content: a
  pointer-only table of contents that then surfaced as a high-score duplicate
  on every query. The conventions now live in one table
  (`config._PROVIDER_INDEX_CONVENTIONS`) consulted by the shared
  `_path_is_excluded` funnel, so ingest, the engine walk, the watcher, and
  `mm purge --matching-excluded` all skip the same files. `mm purge
  --matching-excluded --apply` consequently reclaims `MEMORY.md` chunks indexed
  before this fix. The convention is provider-scoped — a `MEMORY.md` in a plain
  user folder is still indexed as real content.

- **MCP server definitions in the Context Gateway (#1165).** A new **MCP Servers**
  gateway section manages canonical definitions under
  `.memtomem/mcp-servers/<name>.json` and fans them out to the project
  `.mcp.json` `mcpServers` object (create / read / update / delete / diff /
  Sync All, with an overview tile and EN/KO i18n). Writes and sync run through
  the privacy write-guard, so a secret-shaped `env` value is refused rather than
  copied into the shared `.mcp.json` — use `${VAR}` references for secrets. v1 is
  intentionally narrow: only the `project_shared` tier is writable (reads on
  other tiers return empty, never an error), and only stdio servers (a non-empty
  `command` field) are accepted — network (SSE/HTTP) transports, user-home client
  configs, and reverse import are deferred to a follow-up that needs stronger
  host-write and secret-handling policy.

- **Antigravity CLI (`agy`) documented as an MCP target; Gemini CLI marked
  deprecated (#1167).** Google is replacing Gemini CLI with the Go-based
  Antigravity CLI; Gemini CLI stops serving free/Pro/Ultra individual tiers on
  2026-06-18 (enterprise Gemini Code Assist keeps it). Antigravity CLI registers
  MCP servers in its own `~/.gemini/antigravity-cli/mcp_config.json` (key
  `mcpServers`, stdio entries with `"type": "stdio"`) — distinct from both Gemini
  CLI's `~/.gemini/settings.json` and the Antigravity IDE's
  `~/.gemini/antigravity/mcp_config.json`. The `mm init` paste-hints plus the
  getting-started, mcp-clients, uninstall, llm-providers, and PyPI README guides
  now surface this path and the deprecation date. The shared Gemini-compatible
  surfaces are intentionally unchanged: Antigravity CLI reads `~/.gemini/GEMINI.md`,
  so `mm ingest gemini-memory` and the `.gemini/` context fan-out keep working —
  no runtime removed, no config repathed.

- **`mm init` splits Codex memories into per-subdir namespaces (#1164).** The provider
  preset for `~/.codex/memories/` now generates three ordered namespace rules
  instead of one flat `codex` rule: `codex:rollout_summaries` (per-session
  episodic recaps), `codex:extensions` (the ad-hoc note inbox), and
  `codex:global` (the consolidated top-level memory) as the catch-all. Folding
  those three distinct memory classes into one namespace defeated per-class
  search and time-decay. Rules use literal namespaces only, so the RFC #304
  placeholder lock is unaffected. Re-running `mm init` after upgrading migrates
  an existing flat `codex` rule in place: the stale catch-all is removed and the
  split takes its position (reported with a `↻` line), so the new subdir rules
  resolve correctly under first-match-wins instead of being shadowed. A
  hand-edited codex rule (custom namespace) is never touched. Existing indexed
  data is migrated by re-running `mm index ~/.codex/memories --force`.
- **Context Gateway adds Kimi CLI runtime support.** Skills now fan out to
  `.kimi/skills/`, agents render to Kimi `agent-file` YAML under `.kimi/agents/`,
  hooks sync into Kimi `config.toml` through a memtomem-managed TOML block, and
  `mm init --mcp kimi` writes `~/.kimi/mcp.json` (or `$KIMI_SHARE_DIR/mcp.json`).
  Kimi custom commands stay explicitly unsupported until Kimi documents a
  file-backed custom-command format.

## [0.2.2] — 2026-05-31

Patch release on top of 0.2.1: adds network MCP transports, a `mm web`
background daemon, hooks fan-out to the Codex and Gemini runtimes, and a new
artifact-migration MCP tool, alongside broad Context Gateway / Web UI
accessibility and reliability hardening.

- **New `mem_context_artifact_migrate` MCP tool (#1147, B5-1).** Exposes the
  CLI `mm context migrate` verb over MCP for agents / commands / skills: flat→dir
  layout normalization (`to_scope` omitted) and ADR-0011 scope-tier moves
  (`to_scope` set). Dry-run by default (`apply=True` to execute); `force=True`
  migrates dirty flat files (flat→dir only); `confirm_project_shared=True` is
  required to write the git-tracked tier. Reuses the same pure functions and
  gate semantics as the CLI; memory-tier migration stays on
  `mem_context_memory_migrate`.
- **`mem_context_migrate` renamed to `mem_context_memory_migrate` (#1147, B5-2).**
  The tool only ever covered *memory*-tier migration, but its bare name implied
  parity with the full CLI `mm context migrate` (which also does artifact
  flat→dir and scope-tier moves). `mem_context_migrate` remains as a deprecated
  alias that forwards every argument unchanged and will be removed in a future
  major release; the `mem_do` action `context_migrate` likewise resolves to
  `context_memory_migrate`.
- **`memtomem-server` now supports network MCP transports.** Adds
  `--transport sse|http` plus `--host`, `--port`, `--url`, `--allowed-host`,
  `--allowed-origin`, and `--disable-dns-rebinding-protection` for deployments
  that need SSE or streamable HTTP instead of stdio. Treat sse/http transports
  as trusted-network only; place an authenticated reverse proxy in front before
  exposing publicly.
- **`memtomem-server` direct TTY launches now exit with guidance.** Running the
  stdio MCP server directly in a terminal prints MCP-client setup and
  network-transport examples instead of waiting on stdin. Network transport
  defaults are also tightened and the startup banner ordering fixed (#1092).
- **`mm web` can run as a background daemon (#1028).** Lets the Web UI keep
  running detached from the launching shell.
- **Hooks now fan out to the Codex and Gemini runtimes (ADR-0018, #1109).**
  memtomem-owned hook rules are marked so re-sync can update them in place
  (ADR-0019, #1111), duplicate same-matcher conflicts resolve by exact rule
  identity (#1115), and the sync confirmation discloses the actual runtime
  target files with a scope-drift guard (#1114).
- **Custom Commands are promoted to the production tier in Context Gateway
  (#1108).**
- **LLM and embedding requests retry on HTTP 429 (#1056).** Adds shared
  `RateLimitError` handling with `Retry-After` parsing (#1084) and validates
  `max_delay` before sleeping to avoid negative sleeps (#1041).
- **`mem_dedup_merge` defaults to `dry_run=True` for safety parity (#1045).**
  Dedup scan timeout is aligned to 120s and the 408 path is covered (#1085).
- **`mm init` no longer gates HTTP embedding providers on unused client
  packages (#1148).**
- **Context Gateway and Web UI accessibility + reliability hardening.** Focus
  trap/restore and `aria-modal` across modals, a skip-to-main link, polite
  `aria-live` results, reduced-motion support, accessible names for icon-only
  buttons and form inputs, and modal-aware shortcut gating (#1053 series);
  plus project-switch scope/draft race fixes, deep-link cold-load routing,
  single-column mobile layout, and localized error / empty / diff text
  (#1116, #1117, #1120–#1122).
- **Section parser hardened against round-trip data loss; settings TOCTOU and
  the skill-promote rollback leak closed (#1132, #1145).**
- **`/api/reindex` no longer 500s** when index roots need path coercion
  (#1058).

## [0.2.1] — 2026-05-13

Patch release focused on Web UI stability and release polish after
0.2.0.

- **Context Gateway project switching is safer.** Adds the Gateway
  project switcher and fixes stale-response / draft-isolation races
  around scoped project changes, Hooks sync, and project-normalized
  requested scope handling.
- **Home and Sources views are more reliable.** Fixes Home dashboard
  aggregate counts across visible sources, mobile overflow, pinned
  chunk navigation, sampled activity heatmap labeling, source-tree
  navigation, namespace chart actions, and search-filter removal UX.
- **Runtime reranker settings stay in sync.** Fixes runtime reranker
  config synchronization and async hot-reload lazy loading.
- **Add Project and Web copy polish.** Fixes Add Project picker
  discovery scope, localizes Home dashboard states, clarifies Home
  quick-action flow, updates Web docs/comments, and embeds the README
  hero GIF.

## [0.2.0] — 2026-05-13

The 0.2.0 release closes the **ADR-0011 memory scope axis** epic
(per-tier memory writes and reads — `user` / `project_shared` /
`project_local`) and the **Tiered Context Gateway v2** epic (#868 —
ADR-0016 tier-aware Context Gateway + ADR-0017 project-root field
rename) and flips the **Web UI CSRF / Origin / Host guard to enforce
mode** (RFC #787 stage 2). The release notes below are split into the
0.2.0 highlights (post-PR-F work, #930-#972) and the underlying
ADR-0011 PR-A through PR-F changes that landed earlier in the cycle.

### Highlights

- **Tiered Context Gateway v2 — ADR-0016 (#868 epic complete).** The
  web Context Gateway is promoted to a top-level main tab (#962/#967)
  and the surface is now tier-aware end-to-end: write affordances dim
  + intercept on non-shared tiers with a banner (#945), the overview
  dashboard surfaces `project_root` + detected runtimes (#947),
  sync-direction pointers per row (#951), a "last sync" freshness
  indicator (#954), and tier-correct copy in the header (#955) and
  empty-state hint (#957). Tile clicks deep-link to the filtered /
  highlighted leaf via `?section=&filter=&artifact=` URL carriers
  (#834 / #950). Per-rule Hooks detail panel + detail-meta surface
  (#968), Hooks scope-aware sync labels with `needs_confirmation`
  surfacing (#966), no_source state polish (#969), and a guard for
  tier-aware Hooks sync stale-response handling (#972).
- **Project-root field rename — ADR-0017 (#922).** Renamed the
  `project_context_root` BaseSettings field to `project_root` with a
  kept env alias so existing configs keep working. The kept-alias
  contract covers four moving parts that `AliasChoices` alone does not
  (tolerant CLI unset pop, loader env-alias enumeration, Web query
  both-set precedence, and a 4-cell env × persisted test matrix).
  Lands three months early via PR #953.
- **CSRF / Origin / Host guard flipped to enforce mode (RFC #787
  stage 2).** PR #958 flips the soft-warn observation phase to enforce
  for all Web UI mutators. PR #961 folds in a codex review that found
  five SPA mutators that bypassed the threading (including one
  lazy-import call site) and adds an AST-level architectural guard
  with a frozenset of protected entry points, a depth-aware
  fetch-args scanner, and identifier-bound CSRF binding verification.
  Production-loopback Host pin is observed via the central log. This
  is a behavioral change for any embedded / proxied deployments — see
  the RFC for the migration path.
- **Scope-aware audit / rescan (#934).** `mm context rescan` learns
  `--scope=user|project_shared|project_local` and the audit closes a
  cross-project SQLite leak in `iter_chunks_for_audit`. The walker
  targets `canonical_artifact_dir` per tier and unions
  `detect_agent_files` only for `project_shared` (per ADR-0011 §3 the
  project-local tier has no runtime fan-out). MCP parity for the
  audit-class verbs is intentionally deferred (audit is a CI gate,
  not an agent-runtime path).
- **`mem_context_migrate` MCP wrapper (#887 / #926).** The fifth and
  final context MCP tool — all five (`init`, `sync`, `generate`,
  `diff`, `migrate`) now expose the canonical artifact `scope=` axis
  and mirror the CLI's `scope_explicit` gate, click-exception
  handling, per-call output buffers, and stderr-tail preservation.
- **FTS5 query tokenization hardening (#963).** Hardens the FTS5
  query path against the keyword / quote / unicode edge cases that
  previously surfaced as silent zero-result responses.
- **Deep-link from overview tile to filtered / highlighted leaf
  (#834 / #950).** Overview tile click sets a `?section=&filter=&
  artifact=` URL carrier that the leaf view replays — `?artifact=` is
  a DOM-remove (count==1 negative pin), `?filter=` is the `hidden`
  attr (so reset round-trips without refetch). Tier swaps clear the
  carrier; unknown filter values fall through.
- **Web "last sync" freshness indicator + sync-direction pointers
  (#832 / #833 / #951 / #954).** Each row gets a relative-time
  freshness indicator and an arrow pointer for the direction of the
  most recent sync. Skill-dir mtime is read from the manifest file
  rather than the directory itself so any aux-file write does not
  advance the timestamp.
- **Pending chunk highlight retry across same-source loads (#680 /
  #971).** Timeline → Source jumps that land outside the initial
  100-chunk page now retain the pending target across the next
  fetch, so a Load All retry can highlight the intended chunk. The
  pending target is source-scoped via a new
  `pendingActivateChunkSourcePath` companion field.
- **Shared `chunk_progress` throttle helper (#659 / #959 / #660 /
  #964 / #965).** Lifts the 100ms throttle + final-tick bypass +
  boundary-reset shape out of `runIndexStream` and `mdReindexOne`
  into `makeChunkProgressRenderer`. Browser tests pin the consumer
  shape and the locale-toggle mid-stream template via
  `add_init_script` + a `langchange` flag + `wait_for_function`
  (the `documentElement.lang` attribute is statically served and is
  not a usable init-done signal).
- **Browser + jsdom test harness landed.** Playwright `tests/web/`
  with 16 specs reaches a stable shape (shared `install_default_stubs`
  in `conftest.py`, lifespan=None app, `page.route` stubs,
  browser-marker auto-skip) and a companion `tests-js/` Vitest +
  jsdom track for fast UI logic specs is documented in CONTRIBUTING
  alongside a Python-route-vs-browser-test decision rule (#970).
- **Deferred ADR tracker convention (#922 / `docs/adr/TRACKER.md`).**
  ADR PRs that defer open questions append a single line per deferred
  question into `TRACKER.md` in the same PR, with the
  `adr-feedback` label and tracking-issue comments as the signal
  channels.
- **CLA Assistant native workflow (#960).** Replaces the third-party
  CLA action with a native GitHub workflow gate; the CLA status
  comment failure is tolerated so a transient failure to post the
  status comment no longer blocks merges.
- **Misc Web polish.** Empty-state CTA helper formalized + namespaces
  layout regression fix (#864); tier filters polish (#940); rescan
  USERPROFILE-pinned tests for Windows; emptyState i18n authoring
  carve-out for technical domain vocabulary.

### Behavioral changes — please read before upgrading

- The Web UI now **enforces** CSRF, Origin, and Host on all mutators
  (RFC #787 stage 2). Integrators that proxy or embed the Web UI must
  ensure the CSRF token is threaded through every mutating fetch and
  that the Origin header is preserved or matches the configured
  loopback Host. The soft-warn observation phase that shipped in
  0.1.x is gone.
- The `project_context_root` setting is renamed to `project_root`
  (ADR-0017). The env alias is kept so existing
  `MEMTOMEM_PROJECT_CONTEXT_ROOT=` configs keep working in 0.2.x; the
  tracking row in `docs/adr/TRACKER.md` schedules the alias-removal
  review for 2026-08-12.
- `iter_chunks_for_audit` previously cross-leaked between projects on
  the shared SQLite path. `mm context rescan` and the audit class
  callers now filter by `(project_root, scope)` and defensively raise
  on `scope='user' + project_root != None` (#934).

### Added

- **Web tier badges + `/api/add` project-tier rejection hint (ADR-0011
  PR-F Web/docs slice, #929).** Memory sources/chunks and context
  Skills/Commands/Subagents list rows now carry literal tier badges
  (`user`, `project_shared`, `project_local`) per ADR-0016. Web list
  routes accept `?target_scope=` for canonical tier selection and keep
  `project_local` hidden by default unless explicitly requested; context
  rows in `project_local` annotate the draft/no-runtime-fan-out behavior
  inline. `POST /api/add` now rejects unconfirmed
  `scope=project_shared` writes with `403 detail=blocked_project_shared`,
  a CLI remediation hint, and docs URL, mirroring MCP Gate B. Gate A also
  receives the resolved scope so `force_unsafe` cannot bypass
  `project_shared` protections through Web writes.
- **Memory scope axis schema (ADR-0011 PR-B).** First-time start of an
  upgraded server runs a one-shot SQLite migration that adds two new
  columns to ``chunks``: ``scope TEXT NOT NULL DEFAULT 'user'`` (one
  of ``user`` / ``project_shared`` / ``project_local``) and
  ``project_root TEXT`` (the registered project root for project-tier
  rows; ``NULL`` for user-tier). Every existing row backfills to
  ``scope='user'`` so behaviour for current memtomem deployments is
  unchanged on upgrade. Two new indexes
  (``idx_chunks_scope (scope, project_root)``,
  ``idx_chunks_project_root (project_root) WHERE project_root IS NOT
  NULL``) cover the always-on default-merge filter shape. Migration is
  idempotent — DB files already on the new schema are no-ops.
- **`--scope` filter on `mm mem recall`, `mem_search`, `mem_recall`
  (ADR-0011 PR-C).** Optional explicit scope filter accepts a single
  value (``user``), a comma list (``user,project_local``), or a glob
  (``project_*``). Without ``--scope`` the search falls into the
  project-aware default merge (see Changed entry below).
- **`mm mem add --scope` + `--confirm-project-shared` / `--yes` (CLI)
  (ADR-0011 PR-D).** The CLI ``add`` command now writes to one of three
  directories based on the resolved scope: ``~/.memtomem/memories``
  (user, default), ``<project>/.memtomem/memories`` (project_shared,
  git-tracked), or ``<project>/.memtomem/memories.local``
  (project_local, gitignored). Project-tier writes need a registered
  project context (`project_memory_dirs` covers the current cwd);
  without one the CLI exits with a clear error. ``project_shared``
  writes prompt for explicit confirm naming the git-tracked target
  path; ``--yes`` and ``--confirm-project-shared`` skip the prompt for
  scripted use.
- **`mem_add(scope=..., confirm_project_shared=...)` and
  `mem_batch_add(scope=..., confirm_project_shared=...)` (MCP)
  (ADR-0011 PR-D).** The same scope axis is exposed to MCP callers.
  Default scope ``user`` keeps existing behaviour; ``project_shared``
  requires the explicit confirm flag (Gate B). Critically, the write
  target now resolves per scope on the MCP path too — ``mem_add(
  scope='project_shared')`` lands in the project's
  ``.memtomem/memories/`` directory, not the user-tier path. Closes
  the CLI/MCP divergence flagged during PR-D review. ``mem_batch_add``
  routes each entry through ``enforce_write_guard`` per-entry (the
  pre-ADR-0011 inline-scan path is removed); ``force_unsafe=True`` is
  hard-refused on ``project_shared`` per Gate A; the transactional
  reject contract is preserved (clean siblings of a flagged batch
  do not record a ``pass``).
- **`mm context memory-migrate <source> --from <scope> --to <scope>`
  (CLI) (ADR-0011 PR-D).** v1: chunk-id-stable, single-DB rename of
  one markdown memory file between scope tiers. Chunk UUIDs and
  ``chunk_links`` lineage are preserved via a transactional UPDATE on
  the chunks table; no re-index is triggered. Default is dry-run;
  ``--apply`` mutates disk. ``--to project_shared`` re-runs the
  privacy guard on the file content (Gate A on migrate); secret hits
  reject the migration with no force bypass — git history is forever.
  If the DB UPDATE fails after the filesystem move, the move is
  reverted (best-effort) so the source path remains canonical.
  Cross-DB migration, glob/multi-file inputs, and partial chunk-link
  lineage preservation are deferred.
- **Multi-device sync guide (`docs/guides/multi-device-sync.md`).**
  Documents the namespace-aligned layout, `.gitignore` recipe, post-pull
  workflow, and anti-patterns for syncing markdown memories across
  personal devices via a private git repo. Anchors `mm sync-doctor`
  (shipped in 0.1.36 via #838) and the home-rooted `config.json`
  serialization (#836). Inbound links from `README.md`,
  `docs/guides/getting-started.md`, and
  `docs/guides/configuration.md` (Moving `config.json` between machines).
- **`mm context sync --scope=...` + canonical-side Gate A + skills
  staging-dir-first scan (ADR-0011 PR-E3).** ``mm context sync`` (and
  ``mm context generate``) thread the resolved canonical-artifact
  scope through the three include surfaces (``--include=agents`` /
  ``--include=skills`` / ``--include=commands``); the helpers in turn
  call ``generate_all_*`` with the new ``scope=`` kwarg. The default
  remains ``project_shared`` so pre-PR-E3 invocations are
  byte-identical. ``--scope user`` reads ``~/.memtomem/{agents,
  skills,commands}/`` and fans out to ``~/.{claude,gemini,codex}/...``;
  ``--scope project_local`` short-circuits to
  ``NO_PROJECT_FANOUT_FOR_RUNTIME`` skips per runtime (ADR §3 — the
  draft tier has no runtime equivalent). New
  ``context/privacy_scan.py`` runs ``enforce_write_guard`` per file
  (sync direction; ``force_unsafe`` is hardcoded ``False`` per ADR §5
  — sync has no escape valve, unlike init's
  ``--force-unsafe-import``). ``project_shared`` hits raise
  :class:`click.ClickException` with a remediation hint pointing at
  ``mm context migrate`` (PR-E4); ``user`` / ``project_local`` hits
  emit ``PRIVACY_BLOCKED`` skips. Skills fan-out now uses a
  staging-dir-first flow (``_stage_skill`` builds at
  ``dst.parent/.staging-…tmp`` for same-fs atomic
  :func:`os.replace`; vendor SKILL.md override applies BEFORE the
  scan so the scan walks the bytes that will actually be promoted;
  ``_promote_staging`` swaps with rollback). Skill auxiliary files
  (``scripts/``, ``references/``, ``assets/``) stay byte-equal to
  canonical even when an override is staged for ``SKILL.md`` —
  ``test_override_only_touches_skill_md_not_scripts`` invariant
  preserved. The Web PR-F slice in #929 replaced the transitional
  hardcoded route scope with request-driven tier selection.
- **`mm context init --scope=...` + Gate A/B for canonical artifact
  seeding (ADR-0011 PR-E2).** ``mm context init`` (no flag) keeps the
  pre-PR-E2 failure-mode shape — same context.md path, no Gate B
  prompt, no project-signal hard-error from outside a real project —
  with idempotent canonical sub-dir seeding (``agents/``, ``skills/``,
  ``commands/``) added under ``<proj>/.memtomem/`` (or ``<cwd>/`` when
  no project root is found, in which case a yellow hint suggests
  ``--scope=user`` for the cross-project case). All new gating
  behaviour is restricted to EXPLICIT ``--scope`` invocations. ``mm context init`` gains
  three new flags:
  ``--scope user|project_shared|project_local`` (the canonical artifact
  tier to seed), ``--confirm-project-shared`` (Gate B — required when
  ``--scope`` is explicitly ``project_shared``), and
  ``--force-unsafe-import`` (Gate A bypass valve for the runtime-import
  path; user / project_local destinations only). Without ``--scope`` the
  command preserves pre-PR-E2 behaviour: writes context.md and seeds the
  implicit ``project_shared`` canonical tree under
  ``<proj>/.memtomem/{agents,skills,commands}/``. With ``--scope user``
  the command instead reads ``~/.claude/agents`` etc. and seeds
  ``~/.memtomem/{agents,skills,commands}/``. With
  ``--scope project_local`` the command seeds the gitignored draft tier
  ``<proj>/.memtomem/{agents,skills,commands}.local/`` and idempotently
  appends a comment-marked block to ``<proj>/.gitignore`` (covering
  ``.memtomem/*.local/`` and ``.memtomem/.staging/``). Gate A re-scans
  every imported file's bytes via ``enforce_write_guard``: ``user`` /
  ``project_local`` destinations skip-and-warn on hits (or honour
  ``--force-unsafe-import`` with audit-log + raw bytes through);
  ``project_shared`` destinations hard-abort with a
  :class:`click.ClickException` on any hit, with or without
  ``--force-unsafe-import`` (ADR §5: git history is forever). Skill
  imports walk the entire source skill tree (``scripts/``,
  ``references/``, ``assets/``) — one blocked file aborts the whole
  skill atomically. Gemini command imports scan the converted Markdown
  body (where the source ``prompt`` field lands) rather than the raw
  TOML.

### Changed

- **(CLI) ``mm context memory-migrate`` accepts glob input** (e.g.
  ``mm context memory-migrate 'memories/*.md' --from user --to project_local``).
  Quote the pattern to prevent shell expansion. Glob runs an
  all-or-nothing pre-flight (privacy scan + per-file lockfile
  acquisition) before any FS move; on mid-batch DB failure, the
  failing file is reverted (ADR-0011 §5), already-completed files
  stay migrated, and remaining files are left untouched so the user
  has a deterministic resumption point. Single-file input behaviour
  is unchanged. (#886)
- **(CLI) ``mm context memory-migrate`` plan output reports actual
  ``chunk_links`` neighborhood size.** Previously hard-coded
  ``chunk_links lineage: 0 dropped (chunk-id-stable single-DB
  rename)``; now ``chunk_links lineage: N preserved, 0 dropped``
  where N is the count of links touching the moving source's chunks.
  Backed by a new ``count_chunk_links_for_source`` storage helper
  and a regression test pinning lineage preservation across rename.
  (#886)
- **In-project default search merge — behaviour change (ADR-0011 PR-C).**
  ``mem_search`` / ``mem_recall`` running with an MCP server cwd inside
  a registered project (``project_memory_dirs`` covers the cwd) now
  scope by default to ``user`` rows + the current project's
  project_shared / project_local rows. Previously the same call
  returned a cross-project union (every project's rows visible). The
  fragment is composed via the new ``scope_context_sql`` helper and
  applied unconditionally in the storage layer, so a caller cannot
  accidentally drop the boundary by omitting an explicit scope.
  Out-of-project searches still return user-tier only. **Upgrade
  impact:** users who run memtomem from inside a project_memory_dirs-
  covered cwd will see fewer results on the same query than they did
  on 0.1.36 — the dropped rows lived in *other* projects'
  project-tier directories. Pass ``--scope=project_*`` (CLI) /
  ``scope='project_*'`` (MCP) to opt back into a cross-project search.
- **`mem_edit` / `mem_delete` infer scope from the chunk's persisted
  `metadata.scope` (MCP) (ADR-0011 PR-D).** Editing or deleting a
  chunk that lives in ``project_shared`` is gated by the same
  hard-refusal as ``mem_add(scope='project_shared')`` — a client
  cannot bypass Gate A on edit by omitting an explicit ``scope``
  kwarg. ``mem_delete`` adds Gate B: deleting a project_shared chunk
  requires ``confirm_project_shared=True``. Bulk
  ``mem_delete(source_file=...)`` probes the scope set of affected
  chunks via a new ``list_scopes_by_source`` storage method (using
  ``SELECT DISTINCT scope`` rather than the row-limited
  ``list_chunks_by_source``) and rejects all-or-nothing if any
  matched chunk is project_shared without explicit confirm.
- **`mem_consolidate_apply` rejects mixed-scope groups and requires
  explicit `confirm_project_shared=True` for project_shared
  consolidation (MCP) (ADR-0011 PR-D).** Source chunks are loaded
  by ``chunk_ids`` (the truth source — robust to source rename /
  re-index between ``mem_consolidate`` and the apply call) via
  ``get_chunks_batch``, not by re-resolving ``group["source"]``.
  Mixed scope sets skip with a user-visible "Skipped group N: mixed
  memory scopes (...)" message in the MCP return string (in addition
  to the existing logger.warning); single-scope groups inheriting
  ``project_shared`` skip the same way unless ``confirm_project_shared
  =True`` is passed. The summary is written via ``_mem_add_core``
  with the inherited scope so it lands in the matching tier
  directory.
- **`mm context generate` warns when Cursor/Codex/Copilot would merge
  Rules + Style.** These three runtimes fold the canonical `Rules` and
  `Style` sections into a single block via `_compact_rules`, so the
  split does not survive a hand-edit of the generated file followed by
  `mm context init` re-extraction. The CLI now emits a single yellow
  stderr notice naming only the affected runtimes that intersect with
  the target set (no warning for `--agent=claude`/`--agent=gemini`, no
  warning when only one of Rules/Style is populated). Generated file
  format is unchanged; `context.md` remains the source of truth.
- **`config.json` writers serialize home-rooted paths as `~/...`.**
  `indexing.memory_dirs` and `storage.sqlite_path` (and any future
  path-typed config field) are written in tilde form when they sit
  under `$HOME`, restoring portability for the documented "moving
  `config.json` between machines" workflow at
  `docs/guides/configuration.md:96`. Outside-`$HOME` paths stay
  absolute. Loaders are unchanged — `Path.expanduser()` already runs
  at use sites, so the round-trip is symmetric. Legacy absolute-path
  configs continue to load on the same machine; the next save through
  any writer (`mm config set`, the Web UI, `mm init`) rewrites them
  into tilde form. New helper `_portable_path_str` plus
  `_relativize_config_paths_in_place` centralize the transform; all
  four writer call sites (`save_config_overrides`,
  `_persist_auto_discover_migration`, `mm config unset`, `mm init`'s
  `_write_config_and_summary`) now invoke it before
  `_atomic_write_json`.

### Fixed

- **ADR-0011 PR-D review round 12 — `mm context memory-migrate`
  handles nested project-tier sources.** The pre-fix project_root
  inference assumed a fixed depth of three
  (``<root>/.memtomem/memories[.local]/<file>``) and hardcoded
  ``source.parent.parent.parent``. Files in subdirectories like
  ``<root>/.memtomem/memories/notes/foo.md`` left
  ``project_root=None``, AND the fallback to ``_find_project_root()``
  only ran for ``to_scope != "user"`` — so migrating a nested
  project_shared file BACK to user scope errored out at
  ``resolve_memory_scope_dir`` before any FS / DB mutation. Valid
  nested project-tier memories were unmigratable. Now walks the
  source's parents looking for the ``.memtomem`` ancestor, supporting
  arbitrary subdirectory depth.
- **ADR-0011 PR-D review round 11 — engine cooperates with migrate
  sidecar lock.** Round 10's ``mm context memory-migrate`` lock was
  one-sided: only the migrate command itself acquired the sibling
  ``.<name>.lock`` advisory file. The watcher path (``index_file``)
  never asked for the lock, so a concurrent ``mm web`` watcher firing
  ``index_file(target)`` between migrate's ``shutil.move`` and the
  DB UPDATE could still INSERT duplicate chunks at the destination.
  ``IndexEngine.index_file`` now wraps its ``_index_file`` call in
  the same ``_file_lock(_lock_path_for(...))`` so the lock pattern
  is genuinely transitive. New pin
  ``test_engine_index_file_acquires_sidecar_lock_for_watcher_cooperation``
  spies on ``_file_lock`` to confirm the entry path takes the
  expected lockfile.
- **ADR-0011 PR-D review round 11 — direct `dense_search` callers
  thread project context.** Round 9 closed the
  ``SearchPipeline.search`` and ``recall_chunks`` direct-caller gaps
  but ``dense_search`` had its own set of direct callers that the
  always-on storage scope filter still routed to user-tier-only:
  - ``GET /api/chunks/{chunk_id}/similar`` — pin to the source
    chunk's own ``metadata.project_root`` so similar-chunk results
    respect the chunk's tier.
  - ``search/conflict.py:detect_conflicts`` — accepts
    ``project_context_root`` kwarg; ``mem_conflict_detect`` MCP
    tool threads it via ``_resolve_project_context_root(app)``.
  - ``search/dedup.py:DedupScanner._find_near_duplicates`` —
    per-chunk ``chunk.metadata.project_root`` so scans honour
    each chunk's own project tier.
  - ``search/expansion.py:expand_query_headings`` — accepts
    ``project_context_root`` kwarg; ``SearchPipeline.search``
    threads it from the outer search's ``project_context_root``.
  - ``IndexEngine.is_duplicate`` — accepts the kwarg for
    forward-compat (no in-tree callers today).
  Architectural guard test (``test_scope_context_threading.py``)
  now scans ``dense_search`` and ``bm25_search`` direct callers in
  addition to ``SearchPipeline.search`` / ``recall_chunks``, so a
  future regression on any of the four read-surface methods trips
  CI.
- **ADR-0011 PR-D review round 10 — `mem_consolidate_apply` rejects
  project-tier groups with no source `project_root`.** Round 7 added
  the cross-project leak guard for groups whose sources span >1
  distinct ``project_root``, but the zero-root case slipped past:
  every source chunk with ``project_root=None`` (legacy rows
  pre-PR-B backfill, or any decode that left the column NULL) made
  ``project_root_override=None`` and ``_mem_add_core`` then resolved
  the destination via the server cwd, silently leaking summaries
  into whatever project the server happened to be in. Now rejects
  with an explicit ``no source chunk carries a persisted project_root``
  message naming a ``mm reindex`` recovery path.
- **ADR-0011 PR-D review round 10 — `mm context memory-migrate`
  watcher race + transaction lock-up.** The migrate command now
  holds a sidecar advisory lock on both the source and target paths
  spanning ``shutil.move`` + ``update_chunks_scope_for_source`` (the
  ``feedback_sidecar_lockfile_for_replaced_files.md`` pattern), and
  ``update_chunks_scope_for_source`` wraps its SELECT-then-UPDATE
  pair in an explicit ``BEGIN IMMEDIATE`` transaction. Without
  these, a concurrent ``mm web`` watcher firing
  ``index_file(target)`` between the FS move and the DB UPDATE could
  INSERT duplicate chunk rows at the destination, defeating the
  chunk-id-stability guarantee the migrate command promises.
- **ADR-0011 PR-D review round 10 — `mm context memory-migrate
  --yes` parity.** ``--to project_shared`` now requires an explicit
  ``--confirm-project-shared``; ``--yes`` alone is no longer
  sufficient. Mirrors the round-7 fix on ``mm mem add`` for CLI/MCP
  parity with the MCP ``confirm_project_shared=True`` requirement.
- **ADR-0011 PR-D review round 10 — engine `_apply_scope` unchanged-
  chunk drift documented.** Hash-diff means unchanged chunks are not
  re-UPSERTed on a regular reindex, so a previously project-tier
  file whose project is later deregistered keeps stale ``scope`` /
  ``project_root`` rows in storage. Documented the
  ``mm reindex --force`` recovery path inline at the engine site
  and via the existing CHANGELOG project-tier migration entry.
- **ADR-0011 PR-D review round 9 — read surfaces thread project
  context onto the always-on scope filter.** Round 7 introduced the
  always-on scope-context fragment in
  ``storage/sqlite_scope.scope_context_sql`` so missing
  ``project_context_root`` defaults to ``scope='user'`` only. The
  primary ``mem_search`` / ``mem_recall`` callers were updated, but
  every other read surface kept calling ``search_pipeline.search`` /
  ``storage.recall_chunks`` without the kwarg — silently dropping
  project_shared / project_local rows for any caller running inside a
  registered project. Threaded the resolver through:
  - MCP tools: ``mem_ask``, ``mem_temporal_search``,
    ``mem_procedure_list``, ``mem_agent_search``,
    ``_mem_add_core``'s post-write duplicate check, the
    ``recall_chunks`` calls in ``mem_session_summary`` and
    ``mem_reflect_save``.
  - CLI surfaces: interactive shell ``search``, ``ask``, and
    ``recall`` commands.
  - Web routes: ``GET /search``, ``GET /timeline``.
  - LangGraph integration: ``MemtomemStore.search``.
  Web routes that don't have an ``app``/``comp`` wrapper use a new
  ``_resolve_project_context_from_dirs(project_memory_dirs)`` helper
  alongside the existing ``_resolve_project_context_root(app)``
  variant. New AST-scanning guard in
  ``tests/test_scope_context_threading.py`` fails CI if a future
  call site forgets the kwarg.
- **ADR-0011 PR-D review round 7 — cross-project leak in
  `mem_consolidate_apply`.** `mem_consolidate` enumerates source files
  globally so a project-tier group can come from a project that is not
  the MCP server's current cwd. The apply path now derives the
  destination project root from the source chunks' persisted
  `metadata.project_root` (rejecting groups that span multiple
  projects) and threads it into `_mem_add_core` via a new
  `project_root_override` kwarg, so the summary lands in the source
  project's `.memtomem/...` tier instead of being silently routed to
  the server-cwd project.
- **ADR-0011 PR-D review round 7 — `mm mem add` user-tier base.**
  `cli/memory.py` now reads `comp.config.indexing.memory_dirs[0]`
  to derive the user-tier base, matching MCP `_mem_add_core` and
  `mm context memory-migrate`. The previous hardcoded
  `Path("~/.memtomem/memories")` literal split CLI/MCP writes for
  any user who remapped `memory_dirs` — exactly the divergence PR-D
  was meant to close.
- **ADR-0011 PR-D review round 7 — web PATCH/DELETE Gate B.**
  `PATCH /api/chunks/{id}` now infers scope from the loaded chunk's
  `metadata.scope` and feeds it into `enforce_write_guard`, so a
  `force_unsafe` edit on a project_shared chunk hits the same hard
  refusal MCP `mem_edit` enforces (mirrors memory_crud.py:406-413).
  `DELETE /api/chunks/{id}` adds a `confirm_project_shared` query
  parameter and refuses without it for project_shared chunks, mirroring
  the MCP `mem_delete` round-3 fix.
- **ADR-0011 PR-D review round 7 — `--yes` no longer satisfies Gate B.**
  `mm mem add --scope project_shared --yes` (without
  `--confirm-project-shared`) now exits with a clear error rather than
  silently writing to the git-tracked tier. `--yes` is a generic
  "skip prompts" flag users alias for unrelated reasons; treating it
  as an explicit project-shared opt-in broke CLI/MCP parity (MCP
  `mem_add` requires `confirm_project_shared=True` regardless).
- **ADR-0011 PR-D review round 7 — `StorageBackend` Protocol drift.**
  `bm25_search` / `dense_search` / `recall_chunks` Protocol signatures
  in `storage/base.py` now declare `scope_filter` and
  `project_context_root` kwargs (default `None`), aligning with the
  `sqlite_backend` implementation. Without these, alternate backends
  silently dropped the always-on scope-context fragment.
- **ADR-0011 PR-D review round 7 — `idx_chunks_project_root` partial
  index.** New partial index `(project_root) WHERE project_root IS
  NOT NULL` covers the dominant in-project filter shape
  `(scope='user' OR project_root=?)`. The composite
  `idx_chunks_scope (scope, project_root)` could not serve the OR's
  second leg because `project_root` is the trailing column; once a
  user opts into project tiers and accumulates rows, that leg
  degraded to a full table scan. Partial index keeps storage cheap
  for the user-tier majority case.
- **ADR-0011 PR-D review round 7 — Windows CI green.** Two test fixes:
  the `_ALLOWED_DIRECT_ACCESS_SUFFIXES` allowlist in
  `test_all_index_roots.py` now matches via `Path.as_posix()` so
  Windows backslash paths still hit forward-slash entries; the
  `scope_context_sql` parameter assertions in
  `test_search_scope_filter.py` use `str(Path(...))` so the test
  matches native-path stringification on every OS.

### Documentation

- **Obsidian as editor on top of git transport.** New section in the
  multi-device sync guide (`docs/guides/multi-device-sync.md`) describing
  vault layout (vault root = synced repo vs. vault contains a `memories/`
  sub-folder), the required `**/.obsidian/**` exclude_patterns step (vault
  metadata includes JSON which memtomem indexes by default), `.gitignore`
  guidance for vault-local state, and plugin-generated formats (`.canvas`,
  `.excalidraw.md`). Cross-references to the existing one-shot
  `mem_do(action="import_obsidian", …)` flow in `reference.md` clarify
  that they cover different use cases (live sync vs. one-shot ingest).
  Also adds an `**/.obsidian/**` example to the
  `configuration.md#exclude-patterns` block.

- **`configuration.md#exclude-patterns` example is now copy-paste-safe
  (#854).** The block previously used a ` ```jsonc ` fence with a
  path-label comment on the first line and `//` comments inside the
  array, but the fragment loader at
  `packages/memtomem/src/memtomem/config.py:1157` calls strict
  `json.loads`, so a verbatim copy into `~/.memtomem/config.d/*.json`
  was dropped after only a single startup-log WARN — practically silent
  to most users. The example is rewritten to mirror the canonical PR
  #853 shape: prose lead-in, pure-JSON fence, per-pattern `Why` table
  underneath, and an explicit strict-loader note. A new
  `TestNoJsoncFenceInPublicGuides` regression in
  `packages/memtomem/tests/test_docs_guards.py` fails CI if any
  `docs/guides/**/*.md` reintroduces a ` ```jsonc ` fence.

## [0.1.36] — 2026-05-06

### Added

- **Windows MCP server target (#818, closes #817).** `memtomem-server`
  now starts and serves over stdio on Windows. The two `fcntl.flock`
  callsites in `server/__init__.py` (legacy-flock probe + XDG-runtime
  pid lock) are swapped onto `portalocker`, mirroring the #652 swap
  that landed for the CLI side. The legacy-flock probe early-returns
  on Windows because pre-0.1.25 servers were Linux-only by
  construction (the `mm` CLI itself didn't load on Windows until
  #652 / 0.1.34). `_install_sigterm_handler` now POSIX-gates the
  `signal.signal(SIGTERM, ...)` registration; on Windows the teardown
  path is FastMCP's stdin-EOF + `atexit`, since the C runtime does
  not deliver SIGTERM. The atexit cleanup closes the lock fd
  *before* unlinking the pid file on Windows (NTFS refuses to delete
  an open or locked handle, WinError 32) while POSIX still keeps the
  unlink-before-close ordering for the #437 inode-identity invariant.
  Pid files on Windows now land at
  `%LOCALAPPDATA%\Temp\memtomem-0\server.pid`; `~/.memtomem/.server.pid`
  is intentionally never created on Windows.

- **`mm index` streams discovery + chunking progress through a
  `click.progressbar` (#748, closes #656).** Indexing now emits
  per-source events (`discovered`, `parsed`, `chunked`) that drive a
  bar with `Discovered N / Indexed M` so a long index of thousands of
  files no longer feels stuck. The bar length is sized from the
  engine's discovery event, not a heuristic, so the percentage is
  accurate.

- **Tags rename / delete / merge service exposed on Web + MCP (#795,
  PR1 of #688).** A new `tag_management` service unifies the three
  operations behind `mem_tag_rename` / `mem_tag_delete` /
  `mem_tag_list` MCP tools and the Web `PATCH /api/chunks/{id}/tags`
  delegation. Before this, rename and delete were ad-hoc SQL behind
  the search panel only. The Web chunk-edit flow now goes through
  the same service so there's one tag-graph mutation surface to
  reason about.

- **JS unit-test layer for static modules (vitest + jsdom, closes
  #641).** `web/static/`'s pure modules — markdown rendering, tag
  pill construction, sort comparators, i18n key resolution — are
  now exercised under vitest with jsdom, run from a new `js-unit`
  CI job. Closes a real coverage gap: previously these helpers were
  only touched by Playwright e2e specs, which couldn't catch a
  regression in a pure helper without going through the full SPA
  load.

- **CSRF / Origin / Host guard middleware for `mm web` (#787 stage 1,
  observe-only mode).** Logs same-site policy violations on every
  state-changing request without rejecting yet; pins the metric so a
  later stage can flip to `enforce` with confidence the false-positive
  rate is tolerable. The middleware lives in
  `web/middleware/csrf_guard.py` and is wired before the routers.

- **Privacy redaction at the LTM trust boundary covers every user-driven
  write surface (#784, #789, #802).** Previously the redaction guard
  only ran on `mem_add` and the indexing pipeline; the SPA's "Add
  Memory" form, `/api/import`, the Sources upload toast, and the
  redaction-blocked 403 confirm-and-retry path now all flow through
  the same trust-boundary scan. The Add-Memory dialog wires
  `force_unsafe` so users with a confirmed PII-class hit can opt in
  per-write rather than disabling redaction globally.

- **Browser test harness for tag-filter mutation sites (closes #751).** A
  new `packages/memtomem/tests/web/` directory runs a tiny uvicorn-in-thread
  server against a stub-routed `pytest-playwright` page and pins the
  click → DOM-state contract for the three sites in `app.js` that mutate
  `tag-filter` (`_attachResultTagRow`, `renderTagChips`,
  `_searchByTag`). The #672 regression — the Tags Cloud pill click
  silently writing the tag into `search-input` — is now caught
  automatically; previously it relied on manual review. A `browser`
  pytest marker auto-skips on machines without `pytest-playwright` or
  Chromium, mirroring the existing `ollama` pattern, so
  `uv run pytest -m "not ollama"` stays green for contributors who
  haven't run `uv run playwright install chromium`. CI gets a new
  `test-browser` job alongside `test-golden-path`.

### Changed

- **Context Gateway editor surfaces a 3-button conflict dialog on 409
  mtime mismatch instead of silently discarding the user's edits
  (closes #763).** Previously `PUT /api/context/{type}/{name}` returning
  409 (`status: aborted`, mismatched `mtime_ns`) reduced to a single
  toast (`settings.ctx.mtime_conflict`) followed by a quiet
  `loadCtxDetail` that overwrote whatever the user had typed. With
  parallel sessions and the RFC-761 Settings prod expansion bringing
  more concurrent writers, that default destroyed work whose only sin
  was racing another editor. The save handler now opens
  `#ctx-conflict-modal` with side-by-side previews (user buffer vs the
  freshly-fetched on-disk content) and three explicit choices:
  *Reload* (discard buffer, refresh detail), *Open diff editor*
  (renders the user-buffer-vs-on-disk LCS diff inline above the
  textarea so the user can hand-merge — `mtime_ns` is bumped to the
  freshly-read value so the next Save no longer 409s), or
  *Force save* (re-PUTs with the new opt-in `force: true` body field).
  The buffer is stashed in `sessionStorage` on every 409 entry so an
  Escape-out / accidental tab close does not destroy work — the next
  mount of the same `(type, name)` rehydrates the textarea and shows
  a `conflict_draft_restored` info toast. Backend: each of the three
  PUT routes (`context_skills`, `context_agents`, `context_commands`)
  gains a `force: bool = False` field on the Pydantic update model;
  when `force=True` the mtime guard is bypassed and the bypass is
  logged at `WARNING` with the path plus both client and server
  `mtime_ns` values for an audit trail. The 409 response shape itself
  is unchanged (RFC-761 PR-2 / #770 just pinned it). The legacy
  `settings.ctx.mtime_conflict` i18n key is no longer used by code
  but kept for one release as a deprecated alias to avoid churn for
  downstream translators.

- **Tag pill clicks no longer overwrite the search query (closes #672).**
  Clicking a tag in the Tags Cloud or List view now sets only `tag-filter`,
  leaving any text already in `search-input` intact. Previously
  `_searchByTag` copied the tag string into both fields, so the backend
  ran a confusing dual-axis search where the BM25 query and the tag
  filter were the same string — documents that merely *mentioned* the
  tag in prose got boosted on top of the tag-filter constraint, and the
  search bar looked as if the user had typed the tag themselves. With
  the BM25 query left alone, a tag pill click is now a pure narrowing
  filter on whatever the user was already searching for.

- **Tag pill click runs a tag-only search on a fresh session (closes #750).**
  After #672 stopped `_searchByTag` from copying the clicked tag into
  `search-input`, a fresh-session click left `search-input` empty and
  the search early-returned — landing the user on the Search tab with
  `tag-filter` populated but no results, requiring an extra keystroke
  to actually run. The frontend `doSearch()` / `load-more-btn` guards
  now allow a search whenever either `q` *or* `tag-filter` has a
  value; the `/api/search` route's `q` parameter is optional
  (rejecting only "no axis at all" with a 400); and
  `SearchPipeline.search` routes empty-query calls through a new
  `_filter_only_search` branch that enumerates via
  `storage.recall_chunks(tag_filter=…)` and skips
  BM25/dense/expansion/rescue/rerank — the post-filter stages
  (validity → decay → access boost → importance boost → context
  window) still apply so the rank reflects recency × access ×
  importance. `recall_chunks` gained an optional `tag_filter` kwarg
  with the same comma-separated OR semantics as the keyword path's
  post-fusion tag filter.

- **Settings Hooks sync surface promoted to prod web UI (refs #761).**
  Phase D of the Context Gateway (additive merge of canonical
  `.memtomem/settings.json` hooks into `~/.claude/settings.json`) is
  now visible in the default `mm web` surface, alongside Skills /
  Commands / Agents. Previously gated behind `MEMTOMEM_WEB__MODE=dev`.
  The promotion satisfies ADR-0001 §5 readiness criteria — round-trip
  + soft-abort HTTP-layer test fixtures pinned in PR #770; i18n
  parity already covered by `test_i18n.py` auto-discovery; the
  helper-level merge / mtime / atomic-write paths covered by the
  existing 21 tests in `tests/test_context_settings.py`. Rollback:
  `git revert` the gate-flip commit restores dev-only gating.

### Fixed

- **`mm uninstall` deletion is now transactional via stage-then-rmtree
  (closes #757).** Previously a partway failure (permission flip mid-walk,
  external lock acquired during deletion) left the state directory in a
  half-wiped condition with no clean recovery — the user had to manually
  audit what survived. The new path stages the entire deletion to a
  sibling `~/.memtomem.uninstall-staging/` via `os.replace`, then a
  single `rmtree` finalizes; mid-stage failures roll back via reverse
  `os.replace` and the user-visible state is unchanged. Cross-FS staging
  upfront-refuses with a clear message (`os.replace` would EXDEV) and a
  late-firing EXDEV during the rename loop also rolls back. POSIX/Windows
  parity tests use `as_posix()` keys so the snapshot survives both path
  separators.

- **`mm uninstall --force` refuses cleanly on Windows when a writer is
  alive (closes #730).** The POSIX `--force` semantics rely on
  `unlink-while-open` to wipe a path the calling user no longer wants
  even if a process holds the inode; NTFS refuses (WinError 32). Rather
  than half-wipe and fail mid-walk, Windows now refuses upfront with an
  actionable hint pointing at Sysinternals `handle.exe` / Resource
  Monitor / `Get-Process`. POSIX/Windows behavior is now pinned by a
  paired test.

- **Windows runtime dir resolution is NTFS-aware (closes #637).**
  `_runtime_paths` was using POSIX mode-bit gates against NTFS-synthesised
  permissions, which meant `ensure_runtime_dir` refused its own previously
  created directory on every second invocation. The owner-uid +
  mode-`0o700` gates now skip on `os.name == "nt"` and rely on
  `%LOCALAPPDATA%\Temp\` already being per-user; symlink rejection and
  the fresh-creation-with-mode path remain cross-platform.

- **Windows pytest cleanup of components fixture (closes #206).**
  ORT-backed embedder `close()` now forces `gc.collect()` before
  releasing the file handle, so `tmp_path` can rmtree the model dir on
  Windows without lingering handle errors. Tmp-path fixtures gained
  `mkdir(exist_ok=True)` to absorb the same-test reuse pattern.

- **Concurrent-merge abort test forces distinguishable mtimes (closes
  #645).** Windows clock resolution can produce identical `st_mtime_ns`
  for two writes in the same millisecond, breaking the test's contention
  assumption. The setup now bumps mtime explicitly between the two
  writes via `os.utime(..., ns=...)`.

- **Context Gateway editor 409 echoes the current `mtime_ns` on Settings
  resolve abort (#782).** Previously the abort response carried the
  client-submitted (stale) `mtime_ns`, so the next save still 409'd
  even when the client wanted to retry. The route now reads the file
  again post-abort and returns the fresh value so the SPA's resolve
  flow can re-arm without an extra GET.

- **Web i18n race-free dispatch + listener registration (closes #698).**
  `applyDOM` was clobbering already-rendered live data on `langchange`
  because the dispatch happened mid-render. Listeners now register at
  module top-level (not inside conditional setup) and dispatch via
  `setLang` so the order is deterministic. Tests pin both the dispatch
  contract and the listener-registration site.

- **Web a11y: `role="button"` + `aria-label` on row-as-clickable
  surfaces (#700, batched as #808 / #809 / #810).** Search results,
  Timeline rows, and source-detail chunk cards now announce as buttons
  with descriptive labels for screen readers; previously they were bare
  `<div>`s with click handlers, which read as plain text.

- **`/api/sources` resolves `memory_dir` paths instead of returning raw
  config strings (closes #675).** The Sources panel's grouping now
  matches the on-disk path the indexer used, so a `~`-prefixed
  `memory_dirs` entry no longer renders as a separate group from its
  resolved twin.

- **`mm init` filesystem ops wrap into the wizard's `fail_step` (closes
  #664).** `_step_memory_dir` and `_step_settings` now propagate IO
  errors through the wizard's structured error path instead of dumping
  a Python traceback at the user.

- **Web tab activation re-applies `hidden=true` on deactivation (closes
  #699).** Previously a freshly-activated tab could leave the
  just-deactivated sibling visible briefly, producing a flash. Settings
  Runtime group header is now hidden in prod tier (#701).

- **Sync All classifies every non-`ok` Settings status, not just
  `needs_confirmation` (closes #799).** `generate_all_settings` returns
  one of five per-result statuses — `ok` / `skipped` / `error` /
  `needs_confirmation` / `aborted`. #774 (PR #798) added a branch for
  `needs_confirmation` only, so an `error` (canonical or target file
  malformed JSON) or `aborted` (concurrent-write mtime conflict) result
  still toasted `sync_success` even though the merge never landed —
  the same class of "resp.ok hides per-result failure" bug, just with
  different status values. Sync All now surfaces `error` as an
  error-level toast carrying the route's `reason` and `aborted` as a
  warning-level `mtime_conflict` toast — both reusing the same i18n
  keys the per-target Sync flow already uses, so no new strings to
  translate. Severity ladder: `error` > `aborted` >
  `needs_confirmation` > all-`ok`/`skipped` (success). The Settings
  panel's `Sync Now` host-write confirmation flow stays out of scope
  per #774.

### Security

- **Privacy scan covers full content at the LTM trust boundary, not just
  the first 10K chars (#792).** Previously the redaction guard sampled
  only a prefix to keep the scan cheap on large pages, but a determined
  attacker could push secrets past the cutoff and have them ingested.
  The scan now runs against the full content; the perf cost on typical
  prose is negligible and the trust-boundary contract is restored.

- **DNS resolution pinned in `mem_fetch` to defeat rebinding (#794).**
  The URL fetcher now resolves once and pins the IP for the connection,
  preventing a DNS-rebinding attacker from steering a `https://example.com`
  fetch toward `127.0.0.1` between resolution and connect. Combined
  with the existing private-network refusal, this closes the rebinding
  variant of SSRF for the indexer.

- **Sync All no longer lies when Settings host writes need confirmation
  (closes #774).** The Context Gateway "Sync All" button posts to four
  endpoints sequentially; the Settings hop's `POST /api/context/settings/sync`
  body-less request defaults `allow_host_writes` to false, and the route
  returns HTTP 200 with `{"results": [{"status": "needs_confirmation",
  ...}]}` for host-write targets like `~/.claude/settings.json` — the
  merge is skipped. Previously the JS only checked `resp.ok`, so the
  post-merge `sync_success` toast lied about a merge that never
  happened. After RFC #761 (#771) flipped Settings to prod, this
  silent-skip behavior reached every prod user instead of just the
  `mm web --dev` audience. The frontend now inspects per-result
  `status` and surfaces an info-level toast (`Sync All complete except
  Settings — confirm host writes in the Settings panel.`) with an
  `Open Settings` action that navigates to the Settings panel where
  the user can drive the host-write confirmation. The Skills /
  Commands / Agents fanout keeps its all-or-nothing failure shape.

## [0.1.35] — 2026-05-02

### Added

- **Model-readiness banner for `mm web` (PR #703, closes #696).** A new
  `GET /api/system/model-readiness` endpoint reports per-component
  (embedder + reranker) load state derived from `_loading` /
  `_load_error` flags on `OnnxEmbedder` and `FastEmbedReranker`, plus a
  filesystem probe of the fastembed cache. The header banner polls the
  endpoint and renders `Downloading bge-m3 (~2300 MB)…` /
  `Loading model…` / `Model failed to load — check Settings` so the
  first search after a cold-cache boot no longer feels like a hung UI.
  Boot hydrate, `visibilitychange` re-hydrate, and `doSearch()`
  pre-flight cover the three entry points; non-onnx/fastembed providers
  short-circuit to `skipped`.

- **i18n wired up across Export, Import, Tags sort, Auto-Tag, and
  Timeline (PR #695, PR #727).** All target keys already lived in
  `en.json` / `ko.json`; the matching `data-i18n` /
  `data-i18n-placeholder` attributes were missing on Export filters,
  Import bundle/result rows, Tags sort buttons, the Auto-Tag form/result,
  and Timeline date-range/filter labels — Korean-mode users saw mixed
  English/Korean labels (`Source Filter`, `Today`, `Last 7 days`,
  `Count↓`, `Dry run`, `Limit`, etc.). The "Load All" button on the
  Sources chunk browser is also localized via a new
  `chunks.load_all` key (closes #681).

### Fixed

- **Duplicate chunk inserts on shared SQLite DBs (#691, PR #705).** When
  `mm web`'s file watcher and a separate `mm` MCP / CLI process indexed
  the same file at the same time, both processes' independent
  `asyncio.Lock`s let them each `INSERT` chunks with fresh UUIDs that
  shared `(namespace, source_file, content_hash, start_line)`. The
  differ then reused only one of the IDs per re-index, leaving the rest
  as silent ghosts — one user's DB had 77 such groups (~5% of total
  chunks) before the fix. The storage layer now enforces
  `UNIQUE(namespace, source_file, content_hash, start_line)` and uses
  `INSERT OR IGNORE` so race losers are silently dropped at insert time.

  **Migration is automatic on first startup**: existing duplicate groups
  are collapsed to one row each (keeper rule: highest accumulated
  `access_count + use_count`, tie-break on oldest `created_at` then
  `id`), with the matching `chunks_fts` and `chunks_vec` sidecar rows
  removed in lockstep. The migration body is wrapped in
  `BEGIN IMMEDIATE` / `COMMIT` so two processes booting simultaneously
  don't both run the cleanup. A log line — `Cleaned up N duplicate
  chunk row(s) across M group(s)` — is emitted once per DB; subsequent
  startups short-circuit on the new `idx_chunks_unique_content` index.

  `tools.export_import.import_chunks(on_conflict="duplicate")` is
  preserved for back-compat but the storage invariant is now
  authoritative — the mode no longer materialises duplicate rows.

- **`mm web` SPA + `/api/docs` work fully offline (PR #706, PR #712,
  closes #693).** The SPA pulled DOMPurify, marked, and Prism (core +
  five language plugins + theme) from `cdnjs.cloudflare.com`, and
  FastAPI's default `/api/docs` pulled Swagger UI bundle/css from
  `cdn.jsdelivr.net` plus a favicon from `fastapi.tiangolo.com` — three
  failure modes hit at once on every page load: (a) silent breakage on
  offline / firewalled / air-gapped deployments, (b) per-load beacon to
  the CDN with the visitor's IP / UA / request time, (c) no `integrity=`
  hashes so a CDN compromise served arbitrary code into the same origin
  as `/api/...`. All eleven assets (DOMPurify 3.1.6, marked 9.1.6, Prism
  1.29.0 + 5 lang plugins + tomorrow theme, swagger-ui-dist 5.32.5
  bundle.js + css) are now vendored under `web/static/vendor/` with
  pinned SHA-256 in `THIRD_PARTY_LICENSES.md`. CSP tightens to
  `default-src 'self'; script-src 'self'; style-src 'self'
  'unsafe-inline'; img-src 'self' data:; connect-src 'self';
  frame-ancestors 'none'` and a paired positive/negative regression
  guard pins it. `/api/docs` is hand-rolled (instead of
  `get_swagger_ui_html`) so the bootstrap is an external
  `swagger-init.js` and the favicon reuses the SPA's own — no
  `'unsafe-inline'` needed. Footprint: ~1.7 MB total minified
  (~89 KB SPA + ~1.6 MB Swagger UI).

- **Context overview badge surfaces non-`in_sync` states (PR #704,
  closes #692).** The Context tab's overview badge previously checked
  `inSync < total`, which under-counted multi-runtime divergence —
  e.g. three commands tracked under both `claude_commands` (in sync)
  and `gemini_commands` (no target) reported `3/3 synced` while
  `missing_target` was 3. The badge now sums `missing_target +
  missing_canonical + out_of_sync + parse_error` and surfaces the most
  actionable status (precedence:
  `parse_error → missing_target → missing_canonical → out_of_sync`) so
  the user sees `3 missing` in `badge-warning` colour. Five new
  `settings.ctx.badge_*` i18n keys (en + ko).

- **Home bar chart shows full namespace on hover (PR #694).** Long
  auto-namespaces like `claude:-Users-<user>-Work-<project>` truncated
  to `claude:-Users-...` under the 120 px CSS clip with no way to see
  the rest. A `title` attribute on each row makes the full string
  reachable via the browser's native tooltip.

- **Context: `installed_at` captured from filesystem mtime (PR #733,
  closes #732).** On Windows, capturing `installed_at` via Python's
  wall clock and comparing against NTFS `FILETIME` could place
  just-installed files strictly *later* than the captured stamp,
  false-positiving every fresh install as `dirty`. Capture now reads
  `max(st_mtime_ns)` from the destination tree itself, and ceiling-
  divides ns→µs before ISO-8601Z formatting so the round-trip stays
  monotonic across NTFS's 100-ns residual. POSIX is byte-identical for
  ordinary writes; the `dirty.py` strict-`>` invariant is preserved.

- **`mm init` wizard no longer crashes on Windows consoles (PR #728,
  cluster H-1 of #643).** The default Windows codepage (cp1252 / cp437)
  cannot encode the box-drawing and em-dash glyphs the wizard prints,
  so `mm init` raised `UnicodeEncodeError: 'charmap' codec can't encode
  character '─' (U+2500)` at the very first banner line. The CLI entry
  point now reconfigures `sys.stdout` / `sys.stderr` to UTF-8 with
  `errors="replace"` on `sys.platform == "win32"` (POSIX is a no-op),
  so missing glyphs degrade to `?` instead of crashing.

- **Indexing: Windows memory_dir prefixes match stored chunk paths
  (PR #717, closes #647).** `norm_dir_prefix` appended a hardcoded `/`
  to a `Path.resolve()`-ed path, yielding `C:\Users\foo/` on Windows
  that no `chunks.source_file` row could match under
  `target.startswith(prefix)`. This bricked
  `resolve_owning_memory_dir` (every chunk classified as orphan),
  `memory_dir_stats` (all dirs returned 0), the `/api/sources` `kind`
  attribution, and `delete_chunks=true` removal. Fix uses `os.sep` so
  the prefix shape matches `norm_path`'s native-separator output. No
  DB migration required.

- **Search: `source_filter` normalises path separators across seven
  MCP tools (PR #722, closes #720).** `source_filter` was substring
  / glob-matched directly against stored paths with no separator fold,
  so a caller-supplied filter like `/tmp/keep/` never matched a chunk
  stored as `\tmp\keep\policy.md` on Windows. Three contract-specific
  helpers in `search/pipeline.py`
  (`match_source_filter` / `match_source_filter_substring` /
  `match_source_filter_glob`) now centralise the fold across
  `mem_search`, `mem_list`, `mem_consolidate`, `mem_decay`,
  `mem_auto_tag`, `mem_export_chunks`, and `mem_entity_scan`. Persisted
  `chunks.source_file` rows are unchanged — only the comparison picks
  a canonical form.

- **Config: Windows backslash paths in `categorize_memory_dir`
  (PR #716, closes #316).** The provider-pattern table is forward-slash
  regex (`r"/\.claude/projects/[^/]+/memory/?$"`, etc.) but
  `categorize_memory_dir` matched against `str(path)` directly — on
  Windows that's backslash-separated, so every Windows provider
  directory silently fell through to `"user"` and the `mm init`
  wizard could not auto-detect Claude Code / Codex / Gemini memory
  folders. Fix normalises the input via `.replace("\\", "/")`; UNC
  paths and mixed separators covered.

- **Config: `~/...` `path_glob` rules expand via `as_posix()`
  (PR #726, cluster E of #643).**
  `NamespacePolicyRule._expand_and_validate_glob` stored the expanded
  glob via `str(Path(v).expanduser())`, which yielded backslashes on
  Windows. The downstream consumer `pathspec.GitIgnoreSpec` interprets
  gitignore syntax — gitignore is POSIX-only and treats `\` as an
  escape character, so any rule with a leading `~/` (a common config
  shape: `~/.claude/projects/**`, `~/Documents/notes/**`) silently
  matched zero files on Windows and fell back to `default_namespace`
  with no warning. `Path.expanduser().as_posix()` keeps the tilde
  expansion and forces `/` separators; POSIX no-op for absolute paths.

- **Wiki: `mm wiki {skill,agent,command} override` prints consistent
  separators (PR #719).** The `Seeded …` line interpolated a `Path`
  object (native separator) while the adjacent `git add …` hint
  hardcoded `/` — same logical path, two different shapes on Windows.
  Now `Seeded {rel.as_posix()}`. Absolute paths handed to `$EDITOR`
  via `click.edit(filename=str(...))` and shell pipelines via
  `click.echo(str(result.path))` intentionally stay platform-native.

### Documentation

- **`mm embedding-reset` warns about the same-dim model-swap race
  (PR #710, closes #707).** PR #705's `INSERT OR IGNORE` path keeps
  whichever embedding commits first when two processes embed the same
  chunk under *different* models; the dim-mismatch gate catches
  cross-dimension swaps but not same-dimension model swaps. A callout
  in `configuration.md#reset-flow` and a one-line warning in
  `embeddings.md` "Switching Models on an Existing Index" document the
  single-process invariant — stop `mm web` (and any other MCP / CLI
  process) before invoking `mm embedding-reset`.

### Internal

- **Windows test compatibility sweep (refs #643, #644).** A
  cross-cutting cleanup of POSIX-only assumptions in the test suite so
  the Windows informational CI job can graduate to required: a
  `set_home` helper plus a sweep of ~133 HOME monkeypatch sites
  (PR #714); `skipif` markers on POSIX-only mode-bit / `fcntl` /
  `signal` / `pwd.getpwuid` / unlink-semantics assertions (PR #713,
  PR #721, PR #725, PR #729); path-separator normalisation in
  cross-platform assertions (PR #711, PR #718); `shutil.which` for the
  `mm` entry-point lookup (PR #723); and a Windows-friendly
  `test_init_cmd.py` (PR #724).

## [0.1.34] — 2026-05-02

### Added

- **`mm context migrate` (PR-D C4, ADR-0008)** — converts agents and
  commands from the legacy flat layout (`<type>/<name>.md`) to the
  canonical directory layout (`<type>/<name>/agent.md` or
  `<type>/<name>/command.md`) introduced in PR-C. Pre-PR-C installs and
  reverse-imports left flat files on disk; this verb normalizes them so
  the dir-only paths can simplify in a future cleanup PR.

  - **Three signatures**: `mm context migrate` (every flat asset across
    `agents/` and `commands/`), `mm context migrate <type>` (one type),
    `mm context migrate <type> <name>` (single asset). Skills are always
    directory layout (Agent Skills spec); invoking the verb on
    `skills` exits 0 with an informational message rather than an error.
  - **Dry-run by default**; `--apply` mutates the filesystem via
    `os.replace` (atomic single-rename). The lockfile is not touched —
    layout is inferred from the filesystem authoritatively
    (`list_canonical_agents` / `list_canonical_commands`), and
    `installed_at` is preserved so dirty detection (Invariant 2) keeps
    working across migrations.
  - **Eight-row truth table**: the classifier surfaces every combination
    of `flat? × dir? × lockfile-entry? × dirty?` as one of six states
    (`migrate`, `noop`, `cleanup_flat`, `refuse_dirty`, `skip_manual`,
    `skip_orphan`). Manual flat files (no lockfile entry) and orphan
    lockfile entries are surfaced and left untouched — those are out of
    scope for the install/upgrade lifecycle.
  - **Dirty handling mirrors `mm context update --force`** — flat files
    with `mtime > installed_at` are refused unless `--apply --force` is
    passed. With `--force`, a `.bak` sibling is written before mutation
    so the user's edits survive in a forensic snapshot. For `flat+dir`
    collisions the dir layout is left untouched (it carries the
    canonical wiki bytes per PR-C policy); the user reviews the `.bak`
    manually if they want to merge.
  - Pairs with `mm context status`: status walks the dir-only dest tree
    (`is_asset_dirty` is dir-scoped) and so flat-only installs surface
    as `missing` rows; `mm context migrate` is the verb to normalize
    them in place.

- **Multi-project read-only discovery for `mm web` Skills/Commands/Agents
  (PR2 of the multi-project context UI series).** Each tab now renders
  collapsible scope groups so a user running `mm web` from `memtomem`
  can also browse skills/commands/agents under `~/Edu/inflearn/` (or
  any other project root) without restarting the server.
  - **`GET /api/context/projects`** enumerates every discovered scope
    with the response shape `{scope_id, label, root, tier, sources,
    missing, experimental, counts: {skills, commands, agents}}`. Sources
    union "server-cwd", "known-projects", and the opt-in
    "claude-projects" scan; the `scope_id` is `p-<sha12>` of the
    case-normalized resolved path so it survives refresh and restart.
  - **`POST /api/context/known-projects`** registers a project root
    (atomic write + sidecar lockfile). Validates absolute path +
    `is_dir()`; returns a `warning` field but still HTTP 200 when no
    `.claude`/`.gemini`/`.agents`/`.memtomem` marker is present.
  - **`DELETE /api/context/known-projects/{scope_id}`** drops a
    registration (stale entries are removable too).
  - **`GET /api/context/{skills,commands,agents}`** accept an optional
    `?scope_id=` query that targets a different project root; without
    it the legacy single-cwd contract is preserved exactly. Mutating
    routes (POST/PUT/DELETE/sync/import) stay cwd-only — multi-scope
    writes ship in PR3.
  - **Web UI** gains an "Add Project" button in each tab header and
    renders `<details>` scope groups with item counts, source/missing/
    experimental badges, and a per-scope remove (×) button. Server CWD
    items keep their click-to-edit behavior; non-cwd scope items render
    as read-only cards in PR2.
  - **Config** adds `[context_gateway]` with `known_projects_path`
    (default `~/.memtomem/known_projects.json`),
    `experimental_claude_projects_scan` (default `false`), and
    `user_tier_enabled` (default `false`, forward-compat for PR3).

- **Web UI Skills/Commands/Agents empty-state surfaces "why nothing
  happened"**. Previously `mm web` → Settings → Skills/Commands/Agents
  showed a generic `Sync completed` toast even when the canonical root
  (`.memtomem/skills/` etc.) was empty, and a generic `Import completed`
  even when no `.claude/skills/`, `.gemini/skills/`, or `.agents/skills/`
  directory existed under the project root. The toast looked successful
  but nothing actually moved on disk, so users perceived the buttons as
  broken. Now:
  - **`POST /api/context/{skills,commands,agents}/sync`** responses gain
    `canonical_root: str` and each entry in `skipped[]` carries
    `reason_code: "no_canonical_root" | "unknown_runtime" | "parse_error"
    | …` (machine-readable, stable across i18n changes).
  - **`POST /api/context/{skills,commands,agents}/import`** responses gain
    `project_root: str` + `scanned_dirs: list[str]` so the UI can name
    the cwd and the runtime directories that were inspected. Each
    `skipped[]` entry carries `reason_code` (`invalid_name`,
    `already_imported`, `canonical_exists`, `toml_parse_error`).
  - **Web UI** matches on `reason_code` (not the human prose) and shows
    info-tone toasts: `No canonical {type} under {canonical}. Create one
    first.` for empty-canonical syncs, and `No runtime {type} found in
    {project_root}. Scanned: {scan_dirs}.` for empty-runtime imports.
  - **Empty list state** under each tab now points at the canonical and
    runtime paths instead of the generic "Create one or import from
    existing runtimes." line — users see exactly where to drop a
    `SKILL.md` / `*.md` / `*.toml` to populate the panel.
  - **Sync handler** also now reads `data.skipped` (in addition to
    `data.dropped`); previously skills' `skipped` was silently ignored
    because skills don't carry `dropped` field-level omissions.

  All response changes are additive — existing clients that only read
  `imported`/`generated`/`skipped[].name|runtime|reason` keep working.
- **`mm index --debounce-window` / `--flush` / `--status` flags** (closes
  PR #536 documented gap). `mm index` now exposes a file-system-backed
  debounce queue under `~/.memtomem/index_debounce_queue.json`
  (flock-protected). Three new flags:
  - `--debounce-window <SECONDS>` records PATH and drains any entries
    silent at least SECONDS. Designed for `PostToolUse[Write]` hook
    callers — rapid consecutive writes to the same file restart the
    window, so a codegen burst indexes the final state once at the end
    rather than once per Write.
  - `--flush` synchronously drains every queued entry. **Blocks until
    each queued file is indexed (or recorded as an error).** Worst-case
    latency ≈ queue depth × per-file index cost. Plugin's `Stop` hook
    now chains `mm index --flush` before `mm session end --auto` so the
    final burst doesn't get left in the queue.
  - `--status` prints a snapshot of the queue (depth, oldest entry).
    **Race-prone by design** — concurrent hooks may add or drain
    entries between the read and any caller action. Use as telemetry
    only; `--flush` is the only correctness primitive for "drain the
    queue".

  Indexer errors leave the entry in the queue for retry on the next
  hook fire. Last-write-wins for `--namespace`/`--force` when the same
  path is enqueued twice. `MEMTOMEM_INDEX_DEBOUNCE_QUEUE` env var
  overrides the queue path (test-only).

  **Future-extensibility (RFC-B PreCompact, deferred):** when the
  PreCompact hook contract lands and a checkpoint handler wants to
  flush only the files Claude Code reports as in-flight, `--flush`
  will gain a `--paths <list>` form for selective drain. The current
  `--flush` (drain all) remains the default; the API in
  `memtomem.indexing.debounce.drain_all` already accepts an optional
  `paths=` filter so adding the CLI surface is additive.

  Plugin `hooks.json` and the `claude-code.md` Hooks Automation Setup
  snippet are updated byte-for-byte (parity test catches drift). The
  PostToolUse[Write] hook now calls `mm index --debounce-window 5`;
  the Stop hook chains `mm index --flush` before
  `mm session end --auto`.
- **Web UI Memory Dirs sort** — Sources tab → Memory Dirs panel now
  exposes a sort dropdown on any product leaf with at least 6 entries
  (the dominant case is "Claude projects" once a few dozen
  per-project dirs are auto-discovered). Six modes: newest first,
  oldest first, path A→Z, most files, most chunks, recently indexed.
  Selection persists per product via `localStorage`. Backend
  `/api/memory-dirs/status` gains `created_at` (OS filesystem birth
  time, ISO-8601 UTC) and `last_indexed` (max `chunks.updated_at`
  under the dir prefix) — both `null` for missing or un-indexed dirs.
  Linux without statx-birthtime falls back to `st_ctime`, which can
  shift on `chmod` / `chown` but is monotonic for fresh dirs in
  normal workflows.
- **Memory Dirs item-level metadata** — each row now shows the
  directory creation date (locale-formatted) and a `{files} files ·
  {chunks} chunks` badge mirroring the group total, so sort modes
  like "Most files" / "Recently indexed" can be verified row-by-row.
  Hovering the date shows full ISO timestamps for both `created_at`
  and `last_indexed`.
- **Optional chunk cleanup on memory_dir remove** —
  `POST /api/memory-dirs/remove` accepts `delete_chunks: bool` (default
  `false`). When `true`, every chunk whose `source_file` is under the
  resolved dir prefix is dropped (cascades to FTS / vector / link
  tables); the underlying files on disk are never touched. The Web
  UI's delete confirm shows an opt-in checkbox labeled "Also delete N
  indexed chunks" only when the dir has chunks — the safe default
  remains unregister-only.

### Changed

- **`mm init` wizard now prompts on failed steps instead of silently
  advancing (#626).** When `_step_embedding`'s Ollama branch hits a
  failure (server unreachable, model status indeterminate, `ollama pull`
  errored), the wizard surfaces a red `✗` line and a
  `Retry, back, or quit? [R/b/q]` prompt — pre-fix it printed a warning
  and walked into the next step (Reranker), which masked the real
  problem. Mechanism is reusable: new `StepRetry` exception +
  `fail_step()` helper in `memtomem.cli.wizard`. Tri-state
  `_ollama_has_model` returns `bool | None` so server-error is no longer
  conflated with "model absent". **Behavior change**: the OpenAI
  `--provider openai` "API key invalid + decline to continue" path now
  exits **0** (cancelled by user) instead of **1** (error). Scripts
  parsing the exit code to detect a bad-key cancel must switch to
  parsing stdout for `Wizard cancelled.` or use `--yes` mode (which
  retains the non-zero exit on missing extras). Other wizard steps
  still use the pre-#626 silent-continue pattern; an audit and follow-up
  PR will migrate them onto `fail_step` next.

- **`embedding.threads` default flipped from `0` (= ORT default = all
  physical cores) to `4`** so a bulk reindex doesn't pin every core and
  starve the web server / other apps. Live diagnosis of #640 confirmed
  the prior default made a normal indexing run feel like a hang because
  nothing else on the machine could respond. Existing installs without
  a `threads` override will see ONNX use 4 cores after upgrade — slower
  on machines with > 4 cores but visibly responsive. To restore the
  previous behavior on dedicated machines, set
  `embedding.threads = 0` in `~/.memtomem/config.json` (or
  `MEMTOMEM_EMBEDDING__THREADS=0`) and restart `mm web`. Network-bound
  providers (Ollama, OpenAI) ignore the field. Field is restart-required
  per the `MUTABLE_FIELDS` exclusion noted in `EmbeddingConfig`.

- **Force-reindex preserves chunk identity and per-chunk personalization
  (`mem_edit` / `mem_delete` / `mm index --force` / `POST /reindex`).**
  Pre-fix the force path called `delete_by_source` followed by fresh
  `upsert_chunks`, generating new UUIDs and resetting `access_count`,
  `use_count`, `last_accessed_at`, `importance_score` for every chunk
  in the file — including chunks the caller never touched. Now the
  force path runs the same hash-aware diff the non-force path uses,
  preserves IDs for hash-matched chunks, and re-embeds them
  (the `force=True` semantics) via the existing-row UPDATE clause —
  which intentionally does not rewrite personalization columns.
  User-visible: agents that cache chunk IDs across `mem_edit` calls
  stop seeing silent invalidation; access-frequency boost
  (`search/access.py`) and importance scoring
  (`server/tools/importance.py`) no longer drop to 0 when a sibling
  chunk is edited. `chunk_links` rows pointing at unchanged chunks
  also survive — pre-fix the blanket `delete_by_source` cascaded via
  `ON DELETE CASCADE` (target_id) / `ON DELETE SET NULL` (source_id),
  silently dropping consolidation-summary and provenance edges
  whenever a sibling chunk was edited. Contract recorded in
  `docs/adr/0005-force-reindex-metadata-contract.md`. (#582 item 4.2)

- **`POST /api/memory-dirs/add` indexes the registered directory by
  default now.** PR #571 introduced opt-in `auto_index` (default
  `False`) for backward compatibility; the Web UI sent
  `auto_index=true` while CLI/direct-API callers kept the two-step
  (register, then `/api/index`). PR #576 flips the default to
  `True` so omitting the field also indexes — register + index in
  one call for the common case. Callers that want register-only must
  now send `auto_index=false` explicitly (JSON `null` is also
  treated as opt-out, distinct from field omission). Response shape
  is unchanged, but callers that omit `auto_index` will now receive
  an `indexed` object (or `indexed: {"error": ...}` on failure)
  where they previously received `indexed: null` — schema-strict
  clients should expect a populated value. **Performance note:**
  `index_path()` runs in the request/response cycle, so direct-API
  callers indexing large directories will see longer `add` response
  times than before; pass `auto_index=false` to keep the historic
  register-only timings. Watcher invariant and lock boundary are
  unchanged (indexing still runs outside `_config_lock`); indexing
  failures still surface as `indexed: {"error": ...}` instead of
  bubbling a 500 (registration is preserved).

- **`showConfirm` modal** gained an optional `extraOption: { id, label,
  defaultChecked }` parameter that renders an opt-in checkbox below
  the message. Callers that pass extras receive `{ ok, extras }`
  instead of a plain boolean; existing callers without extras get the
  boolean shape unchanged (backward compat).

### Fixed

- **`mm web` Sources page per-row stats badge now renders for
  config-raw memory_dirs.** `/api/memory-dirs/status` was the only
  `/api/memory-dirs/*` endpoint that returned the path with
  `expanduser()` only — every sibling endpoint and `/api/config` use
  `expanduser().resolve()`. When `mm init` (or any wizard pass) wrote
  a tilde-prefixed entry like `~/memories` or a path under a symlinked
  prefix like macOS `/tmp` to `config.indexing.memory_dirs`, the
  frontend's `STATE.memoryDirs` (resolved) and
  `STATE.memoryStatusByPath` keys (unresolved) diverged, the per-row
  lookup missed, and the file/chunk/created badge was skipped. Status
  now resolves on read so the badge renders regardless of how the
  config row was originally persisted. (#666)
- **`mm web` Sources tab now shows orphan-indexed files instead of
  silently dropping them.** The `/api/sources` response carries rows
  with `memory_dir=null, kind=null` for two paths — Index-tab uploads
  saved to `~/.memtomem/uploads/` (which isn't a configured
  `memory_dir`) and chunks whose owning dir was removed from
  `memory_dirs` post-indexing. The server already grouped these as
  "general" with the explicit intent that orphans "ride along" so
  users can find and prune them, but the Sources tab's
  `_renderMemorySourceTree` keyed grouping by `s.memory_dir || ''` and
  then filtered out the empty key (`if (k) allDirs.add(k)`), so the
  orphan rows ended up assigned to no vendor group at all and never
  rendered. The user-facing failure mode: a file uploaded via Index
  tab appeared in `mem_search` and the namespaces tab, but clicking
  its source link from namespaces landed on a Sources tab that didn't
  contain it. The renderer now collects orphan rows separately and
  appends them as a collapsed "Other (unregistered)" sub-section under
  the `user` vendor's active panel, with file rows sharing the same
  click-to-browse-chunks behaviour as indexed dirs. The user vendor's
  sub-tab badge and the empty-state guard count orphans too, so a
  user with only Index-tab uploads sees a populated `user` tab
  instead of "user memory not found".
- **`mm web` now starts a `FileWatcher` and runs a startup backfill.**
  Two gaps fixed together because they presented as one bug ("files
  added to a `memory_dir` don't show up in Sources"):
  - `mm web`'s lifespan previously did not wire `FileWatcher` at all
    (only the MCP server's `AppContext` did), so `mm web` ran with no
    fs watcher — files added while the server was up were never
    auto-picked-up. The lifespan now starts and stops a `FileWatcher`
    in the same way `server/context.py` does, gated on the same
    degraded-mode check (skipped when embedding is broken).
  - `FileWatcher.start()` previously only registered watchdog
    observers — files that landed before `start()` (server was down,
    or the dir was newly added to `memory_dirs`) stayed invisible
    until manual reindex. A one-shot startup backfill task now walks
    each watched dir via `IndexEngine.index_path(recursive=True)`,
    gated by the new `indexing.startup_backfill` flag (**default
    False**). Content-hash dedup makes already-indexed files no-ops,
    so an enabled backfill costs the changed-file count rather than
    tree size. Default-off because an unconditional startup walk
    reintroduces the PR #295 failure mode (silent multi-minute CPU
    embed job blocking the server on first install) — the `mm init`
    wizard's opt-in seed is the user-driven path that resolves the
    same gap with a visible progress bar; users who want backfill on
    every restart can flip the flag explicitly. `mm index <dir>` and
    the web UI's per-dir Reindex button cover ad-hoc indexing without
    flipping the flag.

- **`mm init` wizard now offers `indexing.startup_backfill` as an
  opt-in toggle.** After a successful inline seed the wizard adds a
  second prompt — "Auto-index new files on every server restart?" —
  defaulting to No (same default-skip discipline as the seed prompt).
  Yes writes `indexing.startup_backfill: true` to `config.json`; the
  unset / declined / non-TTY paths leave the key out and preserve
  the `IndexingConfig` default. Closes the discoverability gap from
  the FileWatcher backfill PR — users who actively sync a memory_dir
  from elsewhere (cloud sync, periodic git pull) no longer need to
  hand-edit `config.json` to enable it.

- **`FileWatcher` startup backfill now logs progress.** A
  `Startup backfill: walking N memory_dir(s)...` line at start and a
  `Startup backfill complete: M new chunks indexed` line at end —
  without these the only backfill log was a per-dir summary that only
  fired when something was actually indexed, so opt-in users on slow
  embedders couldn't tell whether the walk was hung or just busy.

## [0.1.33] — 2026-04-29

### Added

- **`mm upgrade` CLI** (#443) — wraps `uv tool install --refresh
  --reinstall memtomem` with process-level hygiene so an in-memory
  pre-upgrade `memtomem-server` can't keep running the old code next
  to the freshly written disk bytes (the v0.1.25 → v0.1.26 stale
  `.server.pid` repro that motivated this command). Probes the server
  pid lock, sends `SIGTERM`, escalates to `SIGKILL` after `--grace`
  seconds (default 5), unlinks the stale pid file, then runs the
  reinstall. **Extras are preserved automatically** by reading the
  current `uv tool` receipt — a `memtomem[all]` install stays `[all]`
  across upgrades instead of silently regressing to a BM25-only base;
  override with `--extras onnx,web` or suppress with `--extras none`.
  `--version X.Y.Z` pins a release; `--dry-run` prints the plan;
  `--json` emits a structured result. On Windows the kill stage is
  skipped automatically (POSIX advisory flock + signals are
  unavailable) and the reinstall runs alone with a warning.
- **`chunk_links` provenance from session summary → source chunks**
  (RFC P1 Phase B-2). When the auto-summary path runs on
  `mem_session_end`, the server now writes `link_type="summarizes"`
  rows from the new `archive:session:<id>` chunk back to each source
  chunk it summarized, bounded by `session_summary.max_summary_links`
  (default 50, newest first, tail dropped per RFC Open-Question-1).
  Manual `summary=` callers do not collect source chunks and so do
  not write links. Each row is best-effort: a single `add_chunk_link`
  failure is logged and skipped rather than aborting `mem_session_end`.
- **Auto LLM session summary on `mem_session_end`** (RFC P1 Phase B-1).
  When `mem_session_end` is called without `summary=` and an LLM
  provider is configured, the server summarizes chunks added during
  the session and persists the result through Phase A's
  `archive:session:<id>` chunk path. New `session_summary` config
  block (`auto`, `min_chunks=5`, `max_summary_tokens=500`,
  `max_input_chars=60000`) gates the behavior; skip reasons
  (`disabled`, `no llm`, `below min_chunks`, `too large`,
  `empty output`, `llm error`) surface in the tool response. See
  [Session Summary](docs/guides/configuration.md#session-summary).
- **`mm session start` SessionStart hook primitives** (#541, RFC
  `memtomem-docs#24`). New flags so a Claude Code SessionStart hook
  can resume the active session instead of orphaning it on every
  start: `--idempotent` returns the existing active session for the
  same `--agent-id` (cross-agent collisions auto-end the prior
  session); `--auto-end-stale=<duration>` (e.g. `24h`, `7d`) closes
  active sessions older than the duration before the idempotency
  check, with a 100-row per-call cap so a backlog of orphans drains
  across multiple hook fires instead of stalling boot synchronously;
  `--json` emits one line of `{"session_id": ..., "resumed": bool,
  "auto_ended": [...]}` for hook parsing — the list groups both
  cutoff-based stale cleanup and cross-agent forced-end UUIDs;
  ``sessions.metadata.reason`` (`stale` or `cross_agent`) carries the
  per-row distinction. The plugin's `hooks.json`
  ships a SessionStart entry calling `mm session start --idempotent
  --auto-end-stale 24h --agent-id claude-code` and the
  `claude-code.md` Hooks Automation Setup snippet matches byte-for-byte
  via the `TestPluginHooksDocsParity` guard added in #536.
  Single-process safe; concurrent SessionStart fires are not locked
  (Claude Code's hook runner serializes them per session, which is
  the supported case).

### Changed

- **Plugin `PostToolUse[Write]` hook now filters by extension and
  path** — the inline `mm index` command in
  `packages/memtomem-claude-plugin/hooks/hooks.json` was indexing every
  `Write` regardless of file type or location, which fanned out to
  `node_modules/`, `dist/`, `__pycache__/`, lock files, binaries, and
  images in monorepo checkouts (embedding-cost amplifier + search
  noise). The hook now allowlists canonical source extensions
  (`md`, `py`, `ts`/`tsx`, `js`/`jsx`, `go`, `rs`, `rb`, `java`, `kt`,
  `swift`, `c`/`cpp`/`h`/`hpp`, `sh`, `toml`, `yaml`/`yml`, `json`)
  and blocklists build / cache / VCS paths (`node_modules`, `dist`,
  `build`, `target`, `.next`, `.nuxt`, `__pycache__`, `.git`,
  `.venv`/`venv`, `coverage`, `.cache`) inline — `case` statements,
  no external script. Adjust the patterns in `hooks.json` for
  project-specific needs. Other hooks (`UserPromptSubmit`,
  `PostToolUse activity log`, `Stop`) are unchanged. Docs at
  [`docs/guides/integrations/claude-code.md`](docs/guides/integrations/claude-code.md)
  Hooks Automation section synced to match. A
  ``test_plugin_hooks_command_matches_docs_snippet`` parity test
  locks ``hooks.json`` and the docs snippet against silent drift on
  future edits. Existing installs that copy-pasted the previous
  snippet into ``~/.claude/settings.json`` continue with the old
  unfiltered behavior until the user re-pulls the snippet. **Documented
  gap**: rapid consecutive writes still re-index the same file; native
  debounce support is tracked separately.

## [0.1.32] — 2026-04-26

### Changed (BREAKING)

- **Caller-supplied `namespace=` / `target=` overrides on every
  session-start entry point and `mem_agent_share` now reject malformed
  shapes via a new `validate_namespace` gate.** PR #491 / #494 / #498
  closed the bypass on `agent_id` itself, but each session-start surface
  also accepted an explicit `namespace=` argument that landed verbatim
  in storage — a Python / MCP / CLI caller could write
  `"agent-runtime:foo:bar"` even though `agent_id` was clean (the
  bypass shape PR #495 review flagged as Concern 3). The new validator
  now runs at `mem_session_start(namespace=)` (MCP),
  `mm session start --namespace` (CLI),
  `MemtomemStore.start_agent_session(namespace=)` and
  `MemtomemStore.start_session(namespace=)` (Python adapter),
  `mem_agent_share(target=)` (MCP), and `mm agent share --target`
  (CLI — closes #497, the kin gap PR #499 review flagged on the share
  surface). `agent-runtime:<seg>` overrides re-route the trailing
  segment through `validate_agent_id` so the override path can't widen
  the contract that the direct `agent_id=` path enforces.

  **Migration:** callers passing structured-but-unsupported namespace
  shapes (anything containing slashes, whitespace, comma, control
  characters, leading dash, or more than one colon under the
  `agent-runtime:` prefix) will now see
  `Error: invalid namespace 'X': ...` instead of the prior silent
  store. Existing in-tree shapes (`default`, `shared`,
  `archive:summary`, `claude-memory:project-x`,
  `agent-runtime:planner`, `custom:scope`) are unchanged. The bare
  single-segment `"agent-runtime"` (no trailing colon) is also rejected
  now — it shadows the multi-agent prefix and the strict-arity rule
  requires exactly one trailing segment after the prefix. Anyone
  holding such a namespace should rename it via `mem_ns_rename` before
  running session-start with the override. Closes #496 and #497.

- **`mem_agent_register`, `mem_agent_search`, and `mm agent register`
  now reject malformed `agent_id` values loudly instead of silently
  rewriting them.** PR #491 had wired `validate_agent_id` into the
  three session-start surfaces (`mem_session_start` / `mm session
  start` / `mm session wrap`) so a hostile shape like `"foo:bar"` or
  `"../x"` raised
  `Error: invalid agent-id 'foo:bar': must match [A-Za-z0-9._-]+ ...`.
  The matching multi-agent registration / search surfaces still ran
  `sanitize_namespace_segment`, so the same input produced two UX
  outcomes depending on which tool the caller hit first — registering
  an agent rewrote the id in place under `agent-runtime:foo_bar`,
  while starting a session for that same id rejected it. The read /
  write contract is now symmetric: an `agent_id` either works on
  every surface or fails on every surface, with the same
  `invalid agent-id 'X'` error fragment regardless of entry point.

  **Migration:** callers passing values that were previously rewritten
  (anything containing `:`, `/`, `..`, whitespace, or characters
  outside `[A-Za-z0-9._-]`) will now see a hard error. Those callers
  were already storing memories under unexpected namespaces — pick a
  canonical id matching the documented charset and re-register. The
  LangGraph adapter parity gap remains tracked separately. Closes #493.

- **`MemtomemStore.start_agent_session` (LangGraph adapter) now
  rejects the same hostile `agent_id` shapes via `validate_agent_id`
  instead of the prior `if not agent_id` empty-only check.** Closes
  the last in-process surface where a Python caller could land
  `"agent-runtime:foo:bar"` in storage even though the MCP / CLI
  surfaces refuse the same shape. The raised exception narrows from
  the generic `ValueError("agent_id must be a non-empty string")` to
  `InvalidNameError("invalid agent-id 'X': ...")` — `InvalidNameError`
  is a `ValueError` subclass, so `except ValueError:` callers keep
  working, but any code substring-matching the old message text will
  break and should switch to matching `"invalid agent-id"`. Closes
  #492.

### Internal

- Renamed the legacy storage-layer helper
  `memtomem.storage.sqlite_namespace.validate_namespace(name) -> bool`
  to `_is_valid_ns_chars` and removed it from
  `memtomem.storage.sqlite_backend.__all__`. Disambiguates from the
  strict caller-input validator
  `memtomem.constants.validate_namespace(value) -> str` introduced in
  PR #491–#503, which raises `InvalidNameError`. The two had the same
  name but different signatures and contracts (charset-only bool vs.
  shape-aware raises), which made debugging harder when a stack trace
  pointed at "validate_namespace". The storage helper has no in-tree
  callers outside its sibling `_ensure_valid_namespace` wrapper, so
  the rename is internal-only — it was nominally exported via
  `__all__`, but a repo-wide grep showed zero external imports.

## [0.1.31] — 2026-04-26

### Added

- **Web UI Context Gateway tabs (Artifact Sync, Skills, Commands,
  Agents) graduate from `--dev` to the polished prod surface.** The
  hardening that landed in #482 (round-trip `## *-Specific`
  preservation), #483 (project-scope `codex_agents` default), and
  #484 (settings-sync host-write confirm) closed the rough edges that
  justified hiding these from `mm web` users by default. The four
  `/api/context/*` routers and matching settings nav buttons now
  mount in `prod`; dev-only stays dev-only for `Namespaces`,
  `Sessions`, `Working Memory`, `Procedures`, `Health Report`, and
  `Hook Files`. Trust boundary unchanged — the loopback + single-user
  Tier 1 deployment shape from `feedback_tier2_web_gating_deferred.md`
  is still the only supported one, and these are the first
  mutator-heavy endpoints to ship in the prod surface, so a future
  Tier 2 hardening pass starts here.
- **`mm context {generate,sync} --include=settings` now confirms before
  writing settings files outside the project root** (today only
  `~/.claude/settings.json`); pass `--yes` / `-y` to skip the prompt.
  The same gate is enforced inside `generate_all_settings`, so callers
  that bypass the CLI — `mem_context_generate` / `mem_context_sync`
  (MCP) and `POST /settings-sync` (Web) — also refuse host writes
  unless they pass `allow_host_writes=true`. Closes one of the
  2026-04-26 audit P0 items: a stray sync from a worktree no longer
  silently edits the real home directory regardless of which front-end
  drove it. Symlink-aware (`Path.resolve()` both sides) so a
  symlinked `.claude` cannot smuggle a host write past the gate.

### Changed (BREAKING)

- **`mm context sync` now writes Codex sub-agents to project-scope
  `.codex/agents/<name>.toml` instead of user-scope
  `~/.codex/agents/<name>.toml`.** Previously every `mm context sync`
  wrote into the user's home directory regardless of which project the
  command ran from, so two projects with same-named canonical agents
  silently overwrote each other in `~/.codex/agents/`, and any
  `mm context sync` from a worktree leaked onto the real host home —
  defeating worktree isolation. The OpenAI Codex CLI [supports both
  scopes](https://developers.openai.com/codex/subagents) ("standalone
  TOML files under `~/.codex/agents/` for personal agents or
  `.codex/agents/` for project-scoped agents"), and project scope is
  the right default for a per-repo canonical source. This makes
  `codex_agents` symmetric with `claude_agents` (`.claude/agents/`)
  and `gemini_agents` (`.gemini/agents/`).

  **Migration:** if you relied on the old user-scope output, copy any
  agents you want to keep with
  `cp ~/.codex/agents/*.toml <project>/.codex/agents/`, or restore
  user-scope behavior on a future `mm context sync` once `--scope=user`
  ships (tracked separately). Existing `~/.codex/agents/` files are
  left in place; memtomem just stops writing there. After copying the
  ones you want to keep, delete the originals (`rm ~/.codex/agents/*.toml`)
  — Codex CLI still loads them, but memtomem no longer manages them, so
  any future edits in the canonical source won't reach the user-scope
  copy and the two will drift silently.

  `detect_agent_dirs` now also picks up `.codex/agents/<name>.toml`
  alongside Claude/Gemini, so `mm context detect` and the Web UI
  Context Gateway list project-scope Codex agents in their inventory.

- **`mem_add`, `mem_batch_add`, `mem_index`, and `mem_fetch` now
  inherit the agent scope from `mem_session_start(agent_id="...")`.**
  Following gap G1 of the 2026-04-26 multi-agent surface review, the
  session-aware MCP write tools join the same priority chain
  `mem_agent_search` already uses:

  1. explicit `namespace=` argument (escape hatch — wins everything),
  2. `app.current_agent_id` → `agent-runtime:<id>` (set by
     `mem_session_start`),
  3. `app.current_namespace` (legacy `mem_ns_set` path),
  4. config default.

  Previously these write tools consulted only step 3, so calling
  `mem_session_start(agent_id="planner")` followed by `mem_add` /
  `mem_index` / `mem_fetch` silently wrote to `default` instead of
  `agent-runtime:planner`, contradicting the multi-agent contract the
  public page advertises. The change is now symmetric with the
  LangGraph adapter (which has had this semantic since PR #460) and
  with `mem_agent_search`'s read-side resolution.

  `mem_consolidate_apply` already routed through `_mem_add_core`, so
  it picks up the new contract automatically.

  **Who is affected:**
  - **Pre-multi-agent users** (no `mem_session_start` call): no
    change. `current_agent_id` stays `None`; resolution falls
    through to step 3 / step 4 exactly as before.
  - **Multi-agent users using only `mem_session_start`**: writes
    now land in the agent's namespace — the documented behavior,
    finally honored. The 940-line e2e guide's workaround (passing
    `namespace="agent-runtime:<id>"` on every write) is no longer
    required.
  - **Rare combination — both `mem_ns_set` and `mem_session_start`
    set in the same session**: write target changes from the
    `mem_ns_set` namespace to the agent's namespace. Pass
    `namespace="<old-ns>"` explicitly on the call to keep the old
    destination, or call `mem_session_end()` before writing.

  `mem_add` now echoes the resolved `Namespace:` line in its
  confirmation output (`mem_batch_add` / `mem_fetch` already did),
  so the resolution is observable on the first call.

  Out of scope (tracked separately):
  - `mem_search` and `mem_recall` (single-agent reads) — `mem_search`
    keeps its `current_namespace` semantic; use `mem_agent_search` to
    read inside the agent scope.
  - `mem_import_notion` / `mem_import_obsidian` — these intentionally
    fall back to source-tagged namespaces (`notion` / `obsidian`)
    rather than the session scope, since their value is tagging
    ingested content by source.

### Fixed

- **`mm context` round-trip no longer silently drops `## <Agent>-Specific`
  sections.** `extract_sections_from_agent_file` mapped agent-override
  headings (`## Claude-Specific`, `## Cursor-Specific`,
  `## Gemini-Specific`, `## Codex-Specific`, `## Copilot-Specific`) to
  their literal heading strings, but the generators look for canonical
  keys (`Claude`, `Cursor`, …). On `mm context init` (extract-existing)
  followed by `mm context generate`, the override section was discarded
  and `mm context diff` showed `[in sync]` because both sides had the
  same loss — masking the data loss. Added the five aliases to the
  reverse-extract map so override sections survive the round-trip.
  Separately, `CopilotGenerator` emitted the override content without
  any `##` heading, so a second round-trip absorbed it into the
  preceding section; it now writes `## Copilot-Specific` like the
  other generators.

- **`mm session start` and `mm session wrap` now derive
  `agent-runtime:<id>` namespace from `--agent-id`, mirroring the MCP
  `mem_session_start` behavior shipped in PR #475.** Previously both
  CLI surfaces silently lost `--agent-id` for namespace derivation,
  leaving sessions in the `default` namespace despite the multi-agent
  contract the public page advertises (Persona-A trap from the
  2026-04-26 surface review, gap G2). `mm session wrap` was the more
  consequential half — its default `--agent-id headless` meant every
  `mm session wrap -- claude -p ...` invocation also landed in
  `default` regardless of the wrapped command. Both surfaces now use a
  shared `_derive_session_namespace` helper with the same priority
  chain as MCP: explicit `--namespace` wins; otherwise
  `agent-runtime:<agent-id>` for non-default agents; otherwise
  `default`. `mm session start` also echoes the resolved namespace so
  users can verify before continuing.

- **`mm web` Sync All button no longer toasts "Settings sync failed"
  in `prod` mode.** Follow-up to the #488 Context Gateway tabs prod
  graduation. The "Sync All" overview button fanned out to
  `/api/context/settings/sync` (the settings-hook merge), but that
  router intentionally stays dev-only; in `prod` the hop returned 404
  and the whole button toasted failure even though the artifact
  fanout (skills / commands / agents) succeeded — making the prod
  surface look broken. The same shape hit the overview's 4th
  "Settings" card, which deep-linked into the dev-only `hooks-sync`
  section, so clicking the card landed on a dead tab. Both code
  paths now self-gate on `STATE.uiMode === 'dev'` (matching the Home
  dashboard pattern); prod gets a clean 3-card overview and a Sync
  All that completes silently for the artifacts it actually has a UI
  for. JS pin tightened to count both gate sites so a future
  refactor can't silently drop one. (PR #489)

## [0.1.30] — 2026-04-26

### Fixed

- **`mm uninstall` no longer reports `pid None` after a contended
  server start.** The runtime pid file was opened with
  `open(pid_file, "w")`, which truncates at open *before* the
  `fcntl.flock(LOCK_EX | LOCK_NB)` probe decides ownership. When a
  second `memtomem-server` started while the first was still
  running (multiple Claude Code / Codex / Gemini MCP clients
  spawning in parallel, or any restart race), the second process
  zeroed out the live server's pid file, then bailed on the flock
  probe — leaving `mm uninstall` with no recorded pid to surface.
  Fixed by switching to `"a+"` (no open-time truncate) and
  performing `seek(0); truncate(); write(pid)` only after the
  flock is held. `mm uninstall` now distinguishes a `pid unknown`
  branch (truncate-race fingerprint or a partial-write startup
  crash) from `pid None` and points at `lsof <pidfile>` for
  diagnosis. Latent since the pid lock was introduced; predates
  the #412 runtime-dir relocation. (PR #476)

- **`mem_agent_share` example in server `instructions=` had wrong
  parameter name.** The recipe shipped in #477 showed
  `mem_agent_share(memory_id=...)` but the real signature is
  `mem_agent_share(chunk_id=..., target=...)`. Weak LLMs (reproduced
  on Claude Haiku 4.5) followed the example verbatim, hit a
  parameter-name rejection, then `mem_do(action="help")`-recovered to
  the correct args — burning an extra round trip per multi-agent
  share. Stronger LLMs (Sonnet 4.5+) inferred past the typo. Fixed
  the example and tightened `tests/test_server_instructions.py` with
  a signature-parity check: any `tool(arg=...)` form in INSTRUCTIONS
  is now cross-checked against `inspect.signature(...)`, so future
  parameter renames force the example to follow.

### Added

- **MCP `initialize` response now carries server-level `instructions`.**
  The MCP server passes a workflow-recipe string to
  `FastMCP(instructions=...)`, which clients auto-inject into every
  LLM session alongside the tool list. The string covers the
  single-agent quickstart (`mem_add` / `mem_search`), the multi-agent
  recipe (`mem_agent_register` → `mem_session_start` →
  `mem_agent_search` / `mem_agent_share` → `mem_session_end`),
  namespace conventions (`default` / `agent-runtime:<id>` / `shared:`),
  and common pitfalls (e.g. `mem_add` without `namespace=` consults
  `current_namespace`, not the session's `agent-runtime:*` scope).
  Source of truth: `memtomem/server/instructions.py`; pinned by
  `tests/test_server_instructions.py` so renames or convention drift
  fail loud. Motivation: tool docstrings alone left LLMs guessing
  which tool to call when users asked for "agent isolation" — clients
  were silently falling back to `mem_add` instead of
  `mem_agent_share`. The instructions field gives the LLM a workflow
  hint without anyone having to paste a system snippet.

### Changed

- **`mem_session_start(agent_id="<id>")` now derives the session
  namespace from `agent_id` when the caller doesn't pass an
  explicit `namespace=`.** Previously the session record's
  namespace fell back to `app.current_namespace` or `"default"`,
  ignoring the supplied `agent_id` even though the LangGraph
  adapter `MemtomemStore.start_agent_session` already auto-derived
  `agent-runtime:<id>`. The MCP tool now matches that semantic.
  Priority order: explicit `namespace=` > `agent-runtime:<id>`
  (when `agent_id != "default"`) > `app.current_namespace` >
  `"default"`. Only the **session record's** namespace field
  changes; `app.current_namespace` is untouched, so subsequent
  `mem_add` / `mem_search` without explicit `namespace=` keep the
  legacy fallback. Backward compat: callers passing explicit
  `namespace=` are unaffected (priority 1); callers not passing
  `agent_id` (defaults to `"default"`) are unaffected (priority 3
  fallback). Caught while walking the multi-agent test scenarios
  — `mem_session_start(agent_id="planner")` reported
  `Namespace: default` even though `current_agent_id` was set to
  `"planner"`, which the tester reasonably expected to flow into
  the session row.

## [0.1.29] — 2026-04-25

Hotfix release. `mm agent register <id>` now surfaces in
`mm agent list` immediately, even before any chunks land in the
agent's namespace. Caught during the first external walkthrough
of the v0.1.28 multi-agent test scenarios — exactly the gap the
walkthrough was designed to expose.

### Fixed

- **`mm agent list` no longer hides registered-but-empty agents.**
  `list_namespace_meta` previously sourced rows from
  `chunks LEFT JOIN namespace_metadata`, so a namespace with a
  metadata row but zero chunks was filtered out by `GROUP BY
  c.namespace`. After `mm agent register planner` the agent
  stayed invisible until someone wrote into
  `agent-runtime:planner`. The Web UI's `GET /namespaces`
  response had the same blind spot through the same storage
  method. Fixed by sourcing from the union of
  `namespace_metadata.namespace` and `chunks.namespace`, then
  joining for chunk count + description/color. Three states now
  surface correctly: metadata only (registered, 0 chunks),
  chunks only (legacy / un-registered), and both. Return shape
  unchanged so callers (CLI, Web route) pick up the fix
  transparently. (PR #473)

### Docs

- **ADR-0002 graduates to public.** The blockquote-tags
  reader/writer contract shipped in v0.1.28 is now documented at
  `docs/adr/0002-mem-add-blockquote-tags.md` with file:line
  references against the v0.1.28 layout, an inbound link from
  the `mem_add` section of the reference guide, and a reverse
  cross-link from the v0.1.28 changelog entry. (PR #472)

## [0.1.28] — 2026-04-25

Multi-agent share provenance + per-entry tag round-trip release.
Largest behavior change: the markdown chunker now promotes per-entry
``> tags:`` blockquotes into ``ChunkMetadata.tags`` and strips the
header from chunk content, so ``mem_add(tags=...)`` finally
round-trips through ``mem_search(tag_filter=...)``. This regenerates
chunk UUIDs on first reindex — a one-time discontinuity; the new
``chunk_links`` table closes the structural gap for future shares,
and a one-shot back-fill populates link rows from existing
``shared-from=<uuid>`` audit tags so older audit chains resolve.

See [ADR-0002](docs/adr/0002-mem-add-blockquote-tags.md) for the
full reader/writer contract and the reasoning behind the on-disk
blockquote format.

Also flips ``agent-runtime:`` into ``system_namespace_prefixes`` by
default (restores the isolation guarantee from the multi-agent
guide), and defaults ``mem_import(on_conflict)`` to ``"skip"`` so
re-imports become idempotent.

### Fixed

- **`mem_batch_add` no longer over-applies tags across entries or onto
  pre-existing chunks.** Pre-fix the tool collected the union of every
  entry's tags after indexing and broadcast that union onto every chunk
  the file produced — including chunks added in earlier sessions and
  unrelated entries inside the same batch. With the chunker now
  promoting per-entry blockquote tags directly (PR #463), the
  post-index broadcast is removed; per-entry tags now stay attached to
  the entry that declared them, and pre-existing chunks in the same
  file are no longer retagged on a subsequent batch.
- **Oversized sections with a per-entry blockquote header no longer
  drift sub-chunks' file line numbers.** When the chunker stripped the
  ``> created:`` / ``> tags:`` blockquote from section text before
  paragraph-splitting, ``_split_section`` still seeded its line
  counter from the heading line — so sub-chunks 2..N reported
  ``start_line`` 3–5 lines earlier than the body they actually
  covered. ``mem_edit`` of a non-first sub-chunk would then pull in
  real body lines from the previous sub-chunk and silently drop them
  on save. The chunker now tracks how many lines the strip consumed
  and seeds the line counter accordingly; the first sub-chunk's
  ``start_line`` still anchors at the heading (preserving
  ``mem_edit``'s header-preservation contract for it). Single-chunk
  sections and sections without a blockquote header are unchanged.

### Changed

- **`mem_add(tags=...)` now writes a canonical blockquote header.**
  ``append_entry`` emits ``> tags: ["a", "b"]`` (explicit ``> `` prefix
  on every line, JSON / double-quoted list) instead of the legacy
  lazy-continuation ``tags: ['a', 'b']`` (Python ``repr()``). Old
  files still parse — the chunker's section-leading parser accepts
  both shapes — but a fresh ``mem_add`` no longer relies on
  CommonMark lazy continuation for the metadata block.
- **`mem_edit` and Web UI chunk edit preserve the per-entry header.**
  New ``replace_chunk_body`` helper keeps the heading line and the
  section-leading ``> created:`` / ``> tags:`` blockquote intact when
  the caller passes body-only ``new_content``. Both ``mem_edit`` and
  the Web UI ``PATCH /api/chunks/{id}`` route use it. The Web UI
  editor surfaces ``chunk.content`` (header-stripped by the chunker),
  so this matters for Save-from-browser. To override the heading
  explicitly, prefix ``new_content`` with ``## `` and the call
  reverts to a full replacement (preserving the pre-RFC semantic).

- **`mem_add(tags=...)` now round-trips through `mem_search(tag_filter=...)`.**
  The markdown chunker promotes the per-entry blockquote header
  (``> created: ...`` / ``> tags: [...]`` / legacy lazy-continuation
  ``tags: [...]``) to first-class `ChunkMetadata.tags`, and strips it
  from chunk content so it no longer leaks into BM25 / embedding
  inputs. File-level YAML frontmatter tags compose with per-section
  blockquote tags via union. Mid-section blockquotes (a quoted
  paragraph in body prose) are untouched. Multi-agent integration
  test asserts on `metadata.tags` for `shared-from=<src>`. Reindex
  to backfill tags onto memories added by older `mem_add` calls.

  **Note: re-indexed chunks regenerate UUIDs.** Stripping the
  blockquote header changes `content`, which changes
  `content_hash = sha256(content)` (`models.py:97`), which the differ
  treats as a new chunk and assigns a fresh `uuid4()`. After
  reindex: any external pinning of `chunk_id` (notebooks, scripts,
  cross-LTM references) will miss, and existing
  `shared-from=<old-uuid>` audit-tag chains will reference UUIDs
  that no longer exist. This is a one-time discontinuity; the new
  `chunk_links` table (see below) closes the structural gap for
  future shares, and a one-shot back-fill in this release
  best-effort recovers pre-existing chains from the
  `shared-from=` tags.

### Added

- **LangGraph adapter (`MemtomemStore`) gains multi-agent helpers.** New
  `start_agent_session(agent_id)` derives the namespace from
  `agent-runtime:<id>` and binds `_current_agent_id`; subsequent
  `search()` / `add()` calls inherit the agent scope without the caller
  passing `namespace=` on every call. `search(include_shared=...)` is a
  3-state toggle: `None` (auto: include shared if an agent is bound),
  `True` (force include — raises `ValueError` if no agent is bound,
  surfacing programming errors instead of degrading to a silent
  un-pinned search), `False` (private only). `add()` defaults
  `namespace=None` to the bound agent's private bucket; pass an
  explicit `namespace="shared"` to publish across agents. Existing
  `start_session(agent_id, namespace)` is preserved as a low-level
  escape hatch. The adapter still does not implement LangGraph's
  `BaseStore` (`aput` / `aget` / `alist_namespaces`); that surface
  remains a follow-up.

- **`mm agent` CLI: `register`, `list`, `share`, plus hidden `debug-resolve`.**
  Mirrors the `mem_agent_*` MCP tools so operators don't have to spin up
  an MCP client for one-off agent setup. `mm agent list [--json]` groups
  registered agents (`agent-runtime:` namespaces) with the cross-agent
  `shared` namespace; `mm agent share <chunk-id> [--target ...]`
  performs the same content copy + `shared-from=<src>` audit tag as the
  MCP tool. The hidden `mm agent debug-resolve` dumps the namespace
  filter `mem_agent_search` would resolve given simulated
  `current_agent_id` / `current_namespace` / `--include-shared` inputs,
  as JSON — for use in multi-agent integration scripts so they can
  assert namespace resolution without standing up an MCP client.

- **Structured share lineage via new `chunk_links` table.**
  `mem_agent_share` now records a source→destination row in
  `chunk_links` (indexed FK, `ON DELETE SET NULL` on `source_id`,
  `ON DELETE CASCADE` on `target_id`) alongside the
  `shared-from=<src>` audit tag it already writes into chunk
  content. Tag-only provenance was a full-table
  `LIKE '%shared-from=%'` scan with no index and broke on reindex
  UUID churn; the structured link is an `O(fanout)` indexed lookup
  and stays correct across source delete. New `Storage` Python API:
  `add_chunk_link`, `get_chunk_link`, `get_chunks_shared_from`,
  `walk_share_chain` (cycle defence + `max_depth`). Public MCP
  surface is unchanged — `mem_agent_share`'s signature and
  copy-on-share semantics are identical; the link is a storage
  invariant. A one-shot back-fill on first 0.1.28 startup scans
  pre-existing `shared-from=<uuid>` tags and populates link rows
  so audit chains authored on older versions resolve
  structurally; unresolvable source UUIDs store `source_id=NULL`.
  (PR #469, PR #470)

- **`mem_import` gains `on_conflict` and `preserve_ids` (bundle schema v2).**
  Bundles now carry per-chunk `chunk_id` + `content_hash` so importers can
  dedup by content across instances. `on_conflict` accepts `"skip"`
  (default, idempotent re-import and cross-PC merge), `"update"` (overwrite
  existing row's metadata while preserving UUID), or `"duplicate"` (pre-v2
  row-duplication, kept for back-compat). `preserve_ids=True` reuses the
  bundle's original UUIDs for new inserts in v2 bundles. v1 bundles still
  import; hashes are derived from content on the fly. Exposed on the
  `mem_import` MCP tool, `import_chunks()`, and `POST /api/export/import`
  (`on_conflict` / `preserve_ids` multipart fields). (PR #451 follow-up)

### Changed

- **`agent-runtime:` is now hidden from default `mem_search` (behavior
  change).** ``search.system_namespace_prefixes`` default extended from
  ``["archive:"]`` to ``["archive:", "agent-runtime:"]`` so per-agent
  private chunks (created by ``mem_agent_register`` / ``mem_agent_search``)
  no longer leak into ``namespace=None`` search results — restoring the
  isolation guarantee advertised on the multi-agent guide. The hidden
  count surfaces through the existing ``hidden_system_ns`` hint, and
  ``mem_agent_search`` is unaffected because it pins ``namespace=``
  explicitly. To restore the pre-change behaviour, override
  ``search.system_namespace_prefixes: []`` in ``config.json`` (or drop
  ``agent-runtime:`` from the list while keeping ``archive:``). New
  ``memtomem.constants`` module exports ``AGENT_NAMESPACE_PREFIX``,
  ``SHARED_NAMESPACE``, and ``default_system_prefixes`` so callers
  derive the literal from a single source.

- **Default import behaviour flipped to `on_conflict="skip"`.** Previously
  every record got a fresh UUID, so re-importing the same bundle doubled
  row counts and merging bundles with overlapping content produced silent
  duplicates. Callers that relied on the old row-duplication behaviour
  must pass `on_conflict="duplicate"` explicitly.

### Fixed

- **`mem_agent_share` now stamps a `shared-from=<source-uuid>` audit
  tag on the copy, with chain dedup.** The docstring previously
  contradicted itself ("Creates a copy" / "cross-reference link instead
  of copying" / actual implementation: a brand-new chunk via
  `mem_add`). The new behaviour is a content **copy** with provenance
  recorded only via the new tag — the function name is preserved for
  API stability and true cross-reference / link semantics are tracked
  as a follow-up RFC. Re-sharing strips any inherited
  `shared-from=...` tag before appending so audit chains do not grow
  unbounded.

- **`mem_session_start` now records `agent_id` so `mem_agent_search`
  inherits it via session context.** Previously `mem_session_start`
  only set `current_session_id`, while `mem_agent_search(agent_id=None)`
  fell back to `current_namespace` — a different axis — so the
  multi-agent guide's "agent_id is not auto-detected, but inherited via
  session context" promise silently broke. New `current_agent_id` field
  on `AppContext`, guarded by a dedicated `_session_lock` (kept distinct
  from `_config_lock` so config writes can't block session updates and
  vice versa). `_resolve_agent_namespace` helper documents the priority:
  explicit `agent_id` arg > `current_agent_id` > `current_namespace`.
  Calling `mem_session_start` while a session is already active now
  auto-ends the previous session (warning log + inline notice in the
  return string) instead of silently overwriting the pointer; the
  storage row for the auto-ended session is closed with an
  `auto_ended: true` metadata flag for audit. `mem_session_end` resets
  both fields.

## [0.1.27] — 2026-04-24

Hotfix release. Closes the multi-instance bug (#444): running
`memtomem-server` in one Claude Code session used to block every
other session from connecting, because the legacy-flock probe took
`LOCK_EX` and `sys.exit(1)`'d on contention. Multiple sessions across
different projects can now coexist.

### Changed

- **Legacy flock downgraded to `LOCK_SH`; two 0.1.26+ servers can now
  coexist.** Previously `_try_hold_legacy_flock` took `LOCK_EX` and
  called `sys.exit(1)` on contention, which was intended as a
  cross-version mutex against pre-0.1.25 servers (#412 B1) but also
  blocked two 0.1.26+ instances from running at the same time — e.g.
  one MCP server per Claude Code session across multiple projects.
  Shared locks compose with other shared locks but still conflict with
  exclusive, so pre-0.1.25's `LOCK_EX` still blocks us (and vice
  versa) — cross-version protection is preserved. On contention we
  now log a warning and fall through to the XDG path rather than
  aborting. (#444, PR #445)

## [0.1.26] — 2026-04-24

Hotfix release. Closes the stale-legacy-pid-file race that caused
`memtomem-server` to intermittently refuse to start after an earlier
shutdown, producing the misleading "pre-0.1.25 install" error in
Claude Code / `claude mcp list`. The live-orphan-holder axis of the
same user symptom (server alive, handshake failed, lock legitimately
held) is tracked as a separate follow-up (#440).

### Fixed

- **Legacy `.server.pid` teardown: `~/.memtomem/.server.pid` is now
  unlinked on both normal exit and SIGTERM.** Previously the legacy
  path was only `flock`-released by kernel cleanup but the file itself
  survived, so the next server spawn could race against the stale file
  under parallel MCP health probes (e.g. `claude mcp list`) and abort
  with the misleading "pre-0.1.25 install" message.
  `_install_sigterm_handler` is now variadic and tracks both the new
  `$XDG_RUNTIME_DIR` pid file and the legacy one (when held), matching
  the `atexit` cleanup. (#437, PR #439)

## [0.1.25] — 2026-04-23

Feature + UX release. Adds `mm status` CLI as the terminal mirror of
`mem_status`. Cuts idle-handshake disk writes: MCP handshake no longer
creates `~/.memtomem/` — the DB opens on the first tool call, and the
server pid file moved to `$XDG_RUNTIME_DIR/memtomem/`. Plus several
`mm init` back-navigation and flag-precedence fixes (#371, #420, #421),
a `serverInfo.version` pin (#383), a `mem_embedding_reset` recovery-path
fix (#409), `-y` refuses to write config for missing extras (#396, #402),
and a docs troubleshooting cleanup (#398).

### Added

- **`mm status` CLI command — terminal mirror of the MCP `mem_status` tool.**
  Closes the gap noted in #382: the README and Quick Start told first-time
  users to "call the `mem_status` tool" to confirm a healthy install, but
  there was no equivalent CLI command — only `mm config show` (config, not
  runtime state) and `mm watchdog status` (periodic-check results). Users
  with no editor open, or running a scripted post-install check, had no
  one-liner that surfaced "is the DB reachable, what's indexed, is the
  embedding config in sync." `mm status` is a thin wrapper that builds an
  `AppContext` from `cli_components` and calls the same `format_status_report`
  helper now shared by `mem_status`, so the CLI and MCP outputs are
  identical. (#382)

### Changed

- **`memtomem-server` pid / flock file moved to `$XDG_RUNTIME_DIR/memtomem/server.pid`.**
  Previously the server wrote `~/.memtomem/.server.pid` at startup, which
  forced `~/.memtomem/` into existence on every MCP handshake (even for
  clients that connect but never call a tool). Runtime state now lives
  on `$XDG_RUNTIME_DIR/memtomem/` when the platform provides it
  (Linux w/ systemd), or `$TMPDIR/memtomem-$UID/` otherwise (macOS, BSD,
  Linux without systemd). Combined with #399 Phase 3's lazy DB creation,
  an idle MCP handshake against a fresh machine now leaves `~/.memtomem/`
  untouched entirely. `mm uninstall` probes both the new and legacy
  locations during the transition window, so a mixed-version upgrade
  (pre-#412 server still running + new uninstall CLI) still refuses
  correctly. (#412)

- **MCP handshake no longer creates `~/.memtomem/memtomem.db`.** Previously,
  every MCP client that connected to memtomem (Claude Code's `claude mcp list`,
  Cursor, Windsurf, Gemini CLI) instantiated the SQLite database on handshake —
  even before the user ran `mm init`, and even for short-lived health-check
  spawns. The DB now opens on the first tool call (`mem_search`, `mem_add`,
  `mem_status`, …) instead of on the lifespan startup, so a client that
  connects but never calls a tool leaves the storage path alone. Two
  follow-on behavior changes are accepted as part of the trade-off:

  - The `"Embedding dimension mismatch detected at startup — entering
    degraded mode"` warning for issue #349 no longer fires on
    `memtomem-server` boot stderr. It now surfaces inside the first tool
    call that triggers initialization. Recovery tools
    (`mem_embedding_reset`, `mem_status`, `mem_stats`, `mem_list`,
    `mem_read`) remain callable; users learn about the mismatch from
    the first tool response instead of the boot log.
  - An idle server (no tool calls) no longer runs background
    maintenance. Consolidation, policy, and health-watchdog schedulers
    start on the first tool call rather than the lifespan handshake.
    A client that connects but never calls a tool will not see
    scheduler ticks — consistent with "no DB to maintain" but worth
    flagging for any maintenance schedule that assumed
    background-without-tool-calls semantics.

  Paired with #412 (runtime pid file relocation), an idle MCP handshake
  now leaves `~/.memtomem/` untouched altogether — the advisory-lock
  write that forced its creation moved to `$XDG_RUNTIME_DIR`. (#399,
  #411; builds on #400 plumbing and #410 handler migration.)

- **Docs: troubleshooting dropped the misleading `memtomem-server` TTY
  verify command.** `uvx --from memtomem memtomem-server` launched bare in
  a terminal paints the terminal with JSON-RPC parse errors on a healthy
  install (the server expects JSON-RPC on stdin, so TTY noise triggers
  `ERROR` lines) and silently provisions `~/.memtomem/`. "Tools don't
  appear in my editor" in `docs/guides/getting-started.md` and
  `docs/guides/mcp-clients.md` now point at side-effect-free checks
  instead: `mm --version` (or `uvx --from memtomem mm --version` for
  uvx-only setups), then a `mem_status` call from inside the editor.
  A blockquote explains why the old command is not a healthy-install
  indicator. (#398, closes #381)

### Fixed

- **`mm init`: step headers show the correct position in every flow —
  not a hardcoded number correct only for `--advanced`.** Previously every
  `_step_*` function called `step_header(N, title)` with a hardcoded `N`
  that matched the step's index in the 10-step advanced sequence. Preset
  flows re-use the same step functions in shorter sequences, so
  `_step_memory_dir` advertised `3. Memory Directory` even when it was
  the first prompt the user saw (`--preset korean`) or the second (default
  interactive after a preset pick). `wizard.run_steps` now seeds
  `state["_wizard_position"] = (current_index, total)` before each
  invocation, and `step_header(state, title)` reads that instead of
  taking the integer as an argument. Numbers: advanced 1–10 (unchanged),
  `--preset X` 1–3, default-interactive 1–4 (picker counts as step 1, the
  silent `_step_provider_dirs_auto` counts as step 3 but prints no header
  of its own). (#420)

- **`mm init`: "b" (back) at the MCP step now reaches the memory-directory
  prompt instead of stalling on a silent banner.** The wizard's
  `_step_provider_dirs_auto` sits between `_step_memory_dir` and `_step_mcp`
  but only prints a detection banner — no prompt. Pre-fix, "b" at the MCP
  step decremented the step index onto that silent step, which re-emitted
  its banner and advanced straight back to `_step_mcp` — so "b" appeared to
  do nothing until the user hit it twice. `wizard.run_steps` now skips past
  `@silent_step`-marked functions when handling `StepBack`, so "b" lands on
  the previous *interactive* step (memory_dir). Banner does not re-fire on
  the back-pass (it only runs forward, once after each memory_dir
  confirmation). Unit coverage for the skip mechanism lives in
  `test_wizard.py`; end-to-end CliRunner regression is in
  `test_init_cmd.py::TestBackNavThroughSilentStep`. (#421)

- **`mm init`: "b" (back) now works at step 3 in the default interactive
  path.** The preset picker and its follow-up steps (memory dir, provider
  dirs auto-detect, MCP config) used to run in two separate `run_steps`
  calls, which made `_step_memory_dir` index 0 of the second call — so
  hitting "b" at the "Memory Directory" prompt showed `(already at first
  step)` and re-prompted the same step instead of returning to the preset
  picker. The two calls are now combined into one: `_step_preset_picker`
  applies the chosen preset inline and raises a new `_AdvancedSelected`
  exception when the user picks Advanced, which the caller catches to
  dispatch the full 10-step wizard. "b" from memory-dir decrements back
  into the picker, and re-picking a different preset overwrites the
  previously applied state cleanly (via the idempotent `_apply_preset`).
  The explicit `--preset <name>` flag path is unchanged — there's no
  picker to return to there. (#371)

- **`mm init` default-interactive CLI flag precedence is now "flag wins
  over prompt", matching the documented behavior of
  `_override_from_flags`.** The default path used to run the override
  pass before the memory-dir / MCP prompts, so prompts silently
  clobbered explicit flags: `mm init --memory-dir /x` (no `--preset`,
  no `-y`) would apply `/x`, then immediately ask for a directory and
  overwrite with whatever the user typed (usually the default
  `~/memories`). The override pass now runs after the combined
  `run_steps` on the preset branch, so flag values win — same rule that
  already held for `--preset <name>` (path 2) and `-y` (non-interactive).
  User-visible effect: `mm init --memory-dir /x` now respects `/x`
  without requiring `--preset`. Advanced (`--advanced`) is unchanged —
  advanced prompts per-field and has no baseline to override. (#371)

- **MCP `serverInfo.version` now reports the memtomem package version.**
  Previously `FastMCP.__init__` left the underlying `Server.version`
  at `None`, so the lowlevel server fell back to
  `importlib.metadata.version("mcp")` and every `initialize` response
  carried the MCP SDK version (e.g. `1.27.0`) as `serverInfo.version`
  instead of `mm --version` (e.g. `0.1.24`). Monitoring probes, client
  telemetry, and any "which version are we both on" comparison saw a
  misleading field. The value is now pinned at module construction
  time via a direct write to `mcp._mcp_server.version`. (#383)

- **`mem_embedding_reset(mode="revert_to_stored")` no longer raises
  `AttributeError` on the recovery path.** #399 Phase 1 made `embedder`,
  `search_pipeline`, and `index_engine` read-only `@property`s on
  `AppContext`, but `_revert_to_stored` kept writing to them directly.
  Any user hitting the degraded-mode "revert to stored" recovery flow
  would see `AttributeError: property 'embedder' of 'AppContext' object
  has no setter` — the exact scenario the tool exists to handle. The
  writes now mutate `app._components` fields directly (the underlying
  dataclass is still mutable by design), and a new end-to-end test
  pins all three runtime slots as swapped, not just `embedder`. (#409)

- **`mm init -y` refuses to write `config.json` when required extras are missing.**
  Previously `-y` accepted `--provider onnx|ollama|openai` and
  `--tokenizer kiwipiepy` without checking whether the corresponding Python
  extras were importable, then wrote the user-specified choice to
  `config.json`. The mismatch surfaced only at runtime as a stderr warning
  from `component_factory` (embedder falls back to 0d) or `fts_tokenizer`
  (kiwipiepy falls back to unicode61) — a scripted install would appear to
  succeed and silently degrade. `-y` now exits non-zero with an actionable
  error listing the missing extras (and collapses multiple misses into a
  `memtomem[all]` hint when all four are missing). The interactive wizard's
  warn-and-continue path is unchanged; the wizard wording was aligned with
  the new `-y` refuse semantics in #405 (closes #403). (#396, #402)

## [0.1.24] — 2026-04-23

Bug-fix release closing a first-run UX gap in `mm init --preset <name>`
and a matching prod-mode log-noise bug in the web UI. Wizard installs
that auto-discover provider memory folders (Claude Code projects,
Claude Desktop plans, Codex memories) now actually consider those
folders in the seed decision, instead of silently skipping the seed
because the primary `~/memories` happens to be empty.

### Fixed

- **Wizard auto-seed now scans `memory_dir + provider_dirs` union.**
  `mm init --preset korean` / `--preset english` / `--preset minimal`
  registers the primary memory dir plus every auto-discovered provider
  folder into `indexing.memory_dirs`, but the opt-in inline seed only
  inspected the primary dir. Since the primary is typically the empty
  `~/memories` on a fresh install, the seed silently returned `False`
  and the Next-steps hint pointed at `mm index ~/memories` — which
  indexed zero files and left the 28 provider dirs invisible to search
  until the user found Sources → Reindex All in the web UI.

  The seed now sums file count and bytes across the union (deduped
  preserving order, mirrors the `combined_dirs` construction that
  feeds `indexing.memory_dirs`) and prompts with "across N memory
  dirs" phrasing when more than one dir is in scope. `_seed_with_progress`
  streams paths serially with a single progress bar. Multi-path Ctrl-C
  / failure hints point at `mm web` → Sources → Reindex All instead of
  a misleading single-path `mm index <dir>` (the `mm index` CLI is
  single-path only as of v0.1.23). Next-steps step 1 in
  `_write_config_and_summary` gets the same split — declined seed with
  multi-dir registration now reads "uv run mm web  (Sources → Reindex
  All to index N memory_dirs)" instead of `mm index ~/memories`.
  PR #295 constraints remain intact: every seed action is
  confirmation-gated (default No), progress-bar instrumented, and
  Ctrl-C resumable. No silent startup scan is reintroduced. (#388)

- **Web UI: `loadNamespaceDropdowns` gated on dev mode.** `/api/namespaces`
  is mounted only in `_DEV_ONLY_ROUTERS` by design (test pin in
  `test_web_mode.py::test_dev_only_routes_blocked_in_prod_but_exposed_in_dev`),
  and the `loadDashboard` caller already had the matching gate. The
  dropdown loader in `settings-namespaces.js` was the orphan: every
  prod session fired a guaranteed 404 at page load (bare module-level
  call) and another on each switch to the search or timeline tab.
  Gate is now internal to `loadNamespaceDropdowns` (prod → early
  return, filter dropdowns keep the static "All Namespaces" option);
  boot-time populate moves into `_applyUiModeFilter` which runs after
  `STATE.uiMode` resolves, so dev still gets a single deterministic
  fetch instead of the previous race-dependent bare call. (#385)

## [0.1.23] — 2026-04-22

Feature release adding a first-class `mm uninstall` command so users can
clean up local state (DB, config, fragments) with confirmation and
install-context awareness, instead of guessing at `rm -rf ~/.memtomem/`.

### Added

- **`mm uninstall` CLI** — categorised inventory + confirmation prompt +
  per-install-context binary-uninstall hint. Flags: `--keep-config`
  (preserve `config.json` + `config.d/*` + backups), `--keep-data`
  (preserve DB + WAL/SHM/journal + `memories/`), `--force` (bypass the
  running-server safety check), `-y/--yes` (skip confirmation).

  The command solves three concrete problems that `uv tool uninstall`
  alone can't: (1) it tells you the right binary-removal command for
  your detected install context (uv-tool / uvx / venv-relative / system
  / unknown, reusing the existing `RuntimeProfile` from
  `cli/init_cmd.py`), (2) it refuses to delete while the MCP server is
  alive (open WAL handle during deletion risks corruption), and (3) it
  honours custom `storage.sqlite_path` so DBs outside `~/.memtomem/` are
  included in the cleanup. It falls back to default paths when
  `config.json` can't be loaded (uninstall is itself a recovery
  scenario), and detects external editor MCP entries (`~/.claude.json`,
  `~/.codex/config.toml`, etc.) as inventory-only — those must still be
  cleaned manually, tracked for a follow-up PR. Deletion runs in
  low→high value order (pid/session → fragments → backups → config →
  memories → DB) with per-group success logging, so a mid-flight
  failure leaves a recoverable trail. (#379)

### Docs

- **`docs/guides/uninstall.md`** — adds a "Recommended: `mm uninstall`"
  section at the top; the manual `rm -rf` flow remains below as a
  fallback for environments without the CLI installed. (#379)

## [0.1.22] — 2026-04-22

Bug-fix release closing the second root-cause path for "no such table:
chunks_vec". v0.1.21 (#305) added the startup gate for legacy DBs whose
stored `dim=0` mismatched a real configured provider; this release fixes
the symmetric writer-side gap that surfaced on **fresh** `mm init
--provider none` installs (no provider switch, just intentional BM25-only
mode). Also bundles two follow-ups from the v0.1.21 wizard work that
wouldn't have made sense to ship separately.

### Fixed

- **`mm index` now succeeds on fresh `--provider none` installs** —
  NoopEmbedder reports `dimension=0`, so the schema gate at
  `sqlite_schema.py` correctly skipped creating the `chunks_vec` virtual
  table. But the writer never honored that skip: `upsert_chunks`,
  `delete_chunks`, `delete_by_source`, and `delete_by_namespace` issued
  unconditional SQL against `chunks_vec`, rolling back the whole
  transaction with `no such table: chunks_vec`. Every file indexed as 0
  chunks, and BM25 search returned empty because the FTS write shared
  the same rolled-back transaction. Fix gates every chunks_vec touch on
  `SqliteBackend._has_vec_table` (cached boolean, primed from a one-time
  `sqlite_master` probe at `initialize()` and refreshed by
  `reset_embedding_meta` / `reset_all`). `NamespaceOps` receives the
  flag as a required live callable so dim=0 → real-provider recovery
  flips cleanly without reconstruction. Existing dim=0 DBs need no
  migration; `mm embedding-reset` recovery path is unchanged. (#377)

### Added

- **Wizard auto-seeds the initial index for small memory dirs** — when
  the discovered `memory_dirs` are below the size threshold and stdin is
  a TTY, `mm init` offers an opt-in confirmation (default No) to run the
  initial `mm index` inline so users see search results immediately
  without a separate command. (#375)
- **Wizard shows progress bar + final summary for large memory dirs** —
  same opt-in seed extended to large dirs with a streaming progress bar
  (files-done / files-total) and a defensive "0 chunks indexed" yellow
  warning if the stream lands zero counters. (#376)

### Docs

- **Indexing model clarified** — `memory_dirs` are auto-watched (file
  changes are picked up live) while `mm index` is the one-shot
  manual-seed entry; previous wording conflated the two. (#374)

## [0.1.21] — 2026-04-22

Phase 3 of the `mm init` install-context series (#360 → v0.1.20 → this
release). The v0.1.18 axis-mismatch bug exposed two independent axes
(cwd filesystem vs runtime interpreter) being re-derived at five
different call sites with no shared contract; this release collapses
them into a single `RuntimeProfile` struct so the next install-context
judgment has exactly one place to land. Also adds first-class `uvx`
detection — pre-Phase-3 the wizard bucketed ephemeral-env invocations
into the generic "PyPI" label and the ephemeral-install hint branch
was dead code.

### Changed

- **`mm init` now classifies `uvx memtomem init` as ephemeral** — when the
  wizard detects it's running under `uvx` (sys.prefix points at
  `~/.cache/uv/archive-v0/…` or `builds-v0/…`), the summary labels the
  install as `uvx (ephemeral)` and `Next steps` adds a one-line note
  explaining the env is destroyed on exit and pointing at
  `uv tool install "memtomem[all]"` for repeat use. Pre-Phase-3 the wizard
  bucketed uvx into the generic "PyPI" label and the existing
  `_install_extras` uvx hint branch was dead code (the call site never
  routed to it). (#363)

### Internal

- **`RuntimeProfile` dataclass + single-source-of-truth refactor** — the
  cwd filesystem axis (source / project / pypi) and runtime interpreter
  axis (`sys.executable`, workspace `.venv` match, `mm` binary origin) are
  now built once at `mm init` entry into a frozen `RuntimeProfile` struct
  threaded through state; downstream call sites
  (`_collect_missing_extras`, `_extra_install_hint`, the cwd-vs-runtime
  mismatch banner, `Next steps` `run_prefix`, summary `Install:` label)
  all read from it. Replaces 4 near-identical 5-ancestor walks
  (`_is_source_install` / `_detect_source_dir` / `_is_project_install` /
  `_detect_project_dir`) with 2 helpers returning `Path | None`. Also
  unifies the in-process module-presence check on `importlib.util.find_spec`
  across `mm init` and `mm web` (was split between `find_spec` and
  `__import__`). 20 new tests cover RuntimeProfile fields, the 5-way
  `mm_binary_origin` heuristic, and the project-install path E2E. (#363)
- **Legacy `state["source_install"] / source_dir / project_install /
  project_dir` keys dropped from `mm init`** — after the Phase 3
  `RuntimeProfile` refactor landed, the parallel legacy state keys were
  intentionally left in place so #367 could ship as a structural-only
  change. This release removes them: `init()` entry now writes only
  `state["_profile"]`, and every downstream reader (the MCP server
  command builder, the missing-extras workspace dir, the "Detected:
  install" echo paths) reads from `profile.cwd_install_type` /
  `profile.cwd_install_dir` directly. Test fixtures migrate to a new
  `_make_test_profile(tmp_path, kind=...)` helper that builds a
  `RuntimeProfile` without invoking the live `_runtime_profile()`. (#368,
  #369)
- **`_get_or_build_profile` back-compat shim removed** — the shim existed
  to let test fixtures build state directly without populating
  `_profile`. With the legacy state keys gone (#369) the reconstruction
  path was dead for production; this release deletes the function
  entirely (~65 lines) and inlines `state["_profile"]` at the 6 call
  sites. `_extra_install_hint` and `_collect_missing_extras` now tolerate
  missing `_profile` via `state.get("_profile")` + `None`-check, treating
  it as PyPI install — preserves the documented "no state / PyPI default"
  contract that `_extra_install_hint(extras, state=None)` already
  advertised. (#370)

## [0.1.20] — 2026-04-22

Phase 2 of the `mm init` install-context UX series (#360 → #361 → this
release). Phase 1 (v0.1.19) fixed the install-hint text for the
cwd-vs-runtime mismatch; this release closes the UX loop by actually
explaining the mismatch and offering to install the missing extras.

### Changed

- **`mm init` now offers to auto-install missing python extras** — the
  summary still surfaces the same Phase 1 install-type-aware hint, but
  first prompts `Install memtomem[all] now?` (defaulting to No — Enter
  skips, preserving the previous hint-only behavior). When the user
  confirms, the wizard shells out to `uv sync --extra …`
  (source/project installs) or `uv tool install --reinstall
  "memtomem[…]"` (tool installs) and prints a single `Installed missing
  extras: …` line on success. Subprocess failures (missing binary,
  timeout, non-zero rc) fall back to the hint. Also adds a one-line
  info banner when the wizard is run from a source/project checkout
  with a global (non-workspace-venv) interpreter, explaining that
  `Next steps` assume `uv run mm` — Phase 1 silenced the false warning
  for this combination but didn't explain why. (#360 Phase 2, #362,
  #364)
- **Non-interactive `mm init -y` runs skip the new prompt** —
  non-TTY contexts (e.g. `mm init -y </dev/null`, CI jobs, Docker
  build steps) short-circuit the `Install memtomem[all] now?` prompt
  entirely and fall back to the Phase 1 hint output; TTY runs show
  the prompt with a **No** default. The non-TTY gate is required
  because `click.prompt` raises `Abort!` on stdin EOF rather than
  returning the `default=` — without it, every scripted pipeline that
  passed on v0.1.19 would hard-exit in v0.1.20. (#365)

## [0.1.19] — 2026-04-22

First-UX follow-up to 0.1.18. The new missing-extras warning misfired on
the "source repo cwd + global `mm`" combination, sending users into an
unnecessary tool-env reinstall when their workspace `.venv` already had
the extras `uv run mm` would use.

### Fixed

- **`mm init` reconciles install-type detection with runtime interpreter**
  — when source-install or project-install is detected, the missing-extras
  probe now spawns `<dir>/.venv/bin/python` (the interpreter `uv run mm`
  will use) instead of running `find_spec` in the wizard's own process,
  and the install hint branches by install type: workspace installs get
  `uv sync --extra <name>` / `--extra all`, while PyPI / `uv tool install`
  paths keep the existing `uv tool install --reinstall "memtomem[…]"`
  hint. Fresh worktrees without a workspace `.venv` see a single
  `Workspace .venv not found — run \`uv sync --extra all\` first.` line
  instead of a noisy warning that probes the wrong interpreter. The
  interactive `_step_embedding` advanced path also shares the install-type
  hint and marks the warning as surfaced so the summary doesn't double-
  print. Subprocess probe failures (timeout / missing binary / bad JSON)
  fall back to the in-process probe so the wizard never silently swallows
  a real gap. (#360 Phase 1)

## [0.1.18] — 2026-04-22

First-UX patch on top of 0.1.17. New users following `mm init` →
`mm web` with a base install hit two silent-failure modes that this
release closes.

### Fixed

- **`mm init` surfaces missing `[onnx]`/`[web]` extras in the summary** —
  preset paths (minimal/english/korean) skipped `_step_embedding`'s
  inline fastembed check, and the `[web]` hint was gated out for
  source-install users. The wizard summary now lists any missing extras
  with a single `memtomem[all]` install hint (or narrower
  `memtomem[onnx]` / `memtomem[web]` when only one is missing) before
  "Next steps", so `mm index` and `mm web` don't fail with opaque
  `fastembed is required` / `Web UI requires the [web] extra` errors
  after following the printed commands. Interpreter-local
  `importlib.util.find_spec` check matches the `mm` binary the wizard
  is running under. (#353, #356)
- **Web UI surfaces `/api/index` embedding failures** — the backend
  route already propagated `IndexingStats.errors` in the response body,
  but the main-tab index handler in `app.js` ignored the field and
  always fired a green "Indexed N chunks" toast. A run where every file
  failed to embed (base install missing `fastembed`) looked like a
  clean success despite hundreds of `Embedding failed … fastembed is
  required` entries on stderr. The handler now flips to a red
  `toast.index_partial` toast and unhides an `Errors` row in the result
  card with up to 5 entries plus a `"…and N more"` trailer. i18n
  `toast.index_partial` / `index.result.errors` added for en + ko.
  (#354, #357)

## [0.1.17] — 2026-04-22

memtomem remains in **alpha**. This release closes the embedding-mismatch
recovery loop end-to-end. The MCP server used to fail-fast crash at
lifespan startup whenever a legacy `provider=none` DB (dim=0) was paired
with a real-provider config, leaving MCP clients with no in-protocol path
to fix it. The server now stays up in degraded mode, `mem_embedding_reset`
drives recovery over MCP, and the `mm web` banner + one-click reset
makes the same flow clickable instead of terminal-only.

### Added

- **`mm version` subcommand** — prints `memtomem <version>`, identical output to
  `mm --version`. Adds parity with `mms version` (memtomem-stm) so users
  switching between the two CLIs get consistent behavior. (#347)
- **`mm init` detects reinstall-path embedding mismatch** — when the new
  preset's provider / dimension differs from what an existing
  `~/.memtomem/memtomem.db` has stored (classically a previous
  `provider=none` install → `chunks_vec` at dim=0 vs a new `onnx/bge-m3`
  1024d config), the wizard now prints the mismatch and offers to reset
  the vector index in place. Without this prompt the next MCP server
  startup would fail with `EmbeddingDimensionMismatchError`. Under
  non-interactive `-y`, the wizard prints a loud recovery hint
  (`mm embedding-reset --mode apply-current`) instead of prompting. The
  chunks table itself is preserved; only the vector index is rebuilt,
  so a re-index is required afterwards. (#348)

### Changed

- **MCP server degraded mode on embedding mismatch (behavior change)** —
  `memtomem-server` no longer fail-fast crashes with
  `EmbeddingDimensionMismatchError` when it encounters a `chunks_vec` /
  provider mismatch at startup. It re-opens storage with
  `strict_dim_check=False` (the same seam `mm embedding-reset` uses),
  exposes the structured mismatch info on `AppContext.embedding_broken`,
  and stays callable. Vector-dependent writes (`mem_add`, `mem_batch_add`,
  `mem_edit`) return an actionable error instead of crashing on
  `upsert_chunks`; `mem_status` / `mem_stats` surface a DEGRADED line;
  `mem_embedding_reset` drives recovery entirely over MCP. File watcher
  and background schedulers (consolidation, policy, health_watchdog) are
  skipped in degraded mode — restart after recovery to bring them back.
  (#349, #350)
- **`mm web` banner fires on degraded mode** — the web lifespan used to
  auto-sync runtime config to the DB-stored embedding info and clear the
  mismatch flag, which for the dim=0 case silently downgraded the user's
  onnx/bge-m3 config to BM25-only and suppressed the banner. The auto-sync
  now skips when `embedding_broken` is set, so the existing
  `#embedding-mismatch-banner` + "Reset vector index" button reach the
  user. (#349 follow-up, #351)
- **`docs/guides/uninstall.md` documents the reinstall-from-scratch path** —
  new section explaining that `mm init` only rewrites config/MCP and
  leaves the DB in place, with a `rm -rf ~/.memtomem && mm init` recipe
  for users who want a fully blank slate. (#348)

## [0.1.16] — 2026-04-21

memtomem remains in **alpha**. This release narrows the default `mm web`
surface to the polished page set so first-time users see a coherent
dashboard instead of the full maintainer toolbox. The full surface
remains available via `mm web --dev` (or `MEMTOMEM_WEB__MODE=dev`), and
every UI-level hide is backed by a matching 404 on the API so
scripting callers fail loudly rather than against a half-built page.

### Added

- **`mm web --mode` / `--dev` flags + `MEMTOMEM_WEB__MODE` env var** — select
  the Web UI surface. `prod` (default) shows the polished page set; `dev`
  extends it with opt-in maintainer pages (Namespaces, Sessions, Working
  Memory, Procedures, Health Report, Artifact Sync, Hook Files,
  Skills/Commands/Agents). `--mode` and `--dev` are mutually exclusive; an
  invalid env value fails fast instead of silently falling back. (#343, #344)
- **`GET /api/system/ui-mode`** — localhost-guarded endpoint returning the
  resolved mode so the SPA can filter tabs on boot. (#343)

### Changed

- **`mm web` default surface shrunk to the polished page set.** `Home`,
  `Search`, `Sources`, `Index`, `Tags`, `Timeline`, and Settings (`Config`,
  `Dedup`, `Age-out`, `Export/Import`, `Reset Database`) are always on. The
  10 remaining Settings sections are opt-in via `mm web --dev` (or
  `MEMTOMEM_WEB__MODE=dev`). Their API routes (`/api/namespaces`,
  `/api/sessions`, `/api/scratch`, `/api/procedures`, `/api/context/*`,
  `/api/settings-sync`, `/api/watchdog/*`, `/api/eval/*`) now return
  **404** in `prod` mode — scripts that rely on them need `dev` mode. (#344)

## [0.1.15] — 2026-04-21

memtomem remains in **alpha**. APIs, defaults, and on-disk config surfaces
may still shift between 0.1.x releases — external feedback and issue
reports are especially welcome at
[github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

This release focuses on **CLI scriptability**. All read/write session
commands now expose `--json` output for hook consumers and scripting
pipelines, `mm --version` answers the obvious first question newcomers
ask, and the contributor docs lock the `--json` error-shape convention
(read vs write) so future flags don't re-litigate the shape per command.

### Added

- **`mm --version` flag** — Click's idiomatic entry point for version output,
  added via `click.version_option` at the group level. Emits
  `memtomem X.Y.Z`. (#330)
- **`mm config show --json`** — alias of `--format json`, added to align with
  the documented CLI output convention (binary human/machine scenario uses
  `--json`). Both flag forms emit identical output. (#332)
- **`mm session list --json`** — scriptable JSON output for the session list,
  emitting `{sessions: [...], count: N}`. Closes the parity gap where
  `watchdog` and `config show` were already scriptable but `session list`
  required parsing the text table. (#331)
- **`mm session events --json`** — same treatment for per-session event
  output, emitting `{session_id, events: [...], count: N}`. When invoked
  without a session argument and no active session, returns
  `{error: "no_session"}` on stdout (exit 0) so scripts can parse the
  failure instead of getting a Click exit-1 + stderr line. Text path is
  unchanged. (#331)
- **`mm activity log --json`** — scriptable ack output for hook-driven
  event writes. Success: `{ok: true, session_id, event_type}`. No active
  session: `{ok: false, reason: "no_active_session"}`. Write failure:
  `{ok: false, reason: "write_failed"}`. Exit code is always 0 (the
  silent-by-default hook contract is preserved without the flag). The
  ok-flag shape intentionally differs from `session events --json`'s
  `{error: ...}` — write acks have no natural disambiguator, so an
  explicit `ok` discriminator is clearer for consumers. (#335)
- **`mm activity log --json` invalid-meta ack** — malformed `--meta`
  JSON now emits `{ok: false, reason: "invalid_meta"}` under `--json`
  (exit 0) so scripts can distinguish bad input from a write failure.
  Without `--json` the `json.JSONDecodeError` still bubbles to Click
  (traceback + exit 1) so a hook author mistyping meta sees why. (#338)

### Changed

- **Documented CLI output convention** — `CONTRIBUTING.md` now spells out
  when to use `--json` (binary scenario) vs `--format [table|json|...]`
  (genuine non-JSON modes like `plain` / `context` / `smart`), with a
  forward-compatibility guidance to prefer `--format` when a command might
  grow additional modes later. (#332)
- **Documented JSON error shape convention** — `CONTRIBUTING.md` now
  specifies the `--json` error shape by command kind: read commands
  (`list`, `get`, `show`, `events`, `status`) emit `{"error": "<reason>"}`
  since their success payloads self-disambiguate; write commands (`log`,
  `add`, `set`, `run`) emit `{"ok": false, "reason": "<reason>"}` with an
  explicit discriminator since write acks have no natural key-based
  disambiguator. Both shapes exit 0 so `--json` pipelines don't break
  on handled failures. Anchors the shape choice made in #336 / #337 so
  future `--json` flags don't re-litigate per-command. (#339)

## [0.1.14] — 2026-04-21

memtomem remains in **alpha**. APIs, defaults, and on-disk config surfaces
may still shift between 0.1.x releases — external feedback and issue
reports are especially welcome at
[github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

### Added
- **`mm init` preset picker**: interactive `mm init` now opens with a preset
  picker (`Minimal` / `English (Recommended)` / `Korean-optimized`) plus an
  `Advanced` entry that runs the full 10-step wizard. Preset paths only
  prompt for the memory directory and MCP registration; embedding /
  reranker / tokenizer / namespace defaults come from the preset bundle.
  New CLI flags `--preset <name>` and `--advanced` expose the same choices
  non-interactively; `--preset` and `--advanced` are mutually exclusive.
  (#326)
- **Non-TTY guard for `mm init`**: running the default interactive path
  with piped stdin (no `--preset`, no `--advanced`, no `-y`) now exits
  cleanly with a usage error pointing at those flags, instead of hanging
  on a closed prompt. (#326)

### Changed
- **`mm init -y` behavior**: scripted `mm init -y` (with no other flags)
  is now equivalent to `mm init --preset minimal -y` — same defaults as
  before this release (provider=none, BM25-only, unicode61 tokenizer), so
  existing CI / automation calls continue to work unchanged. Existing
  explicit flags (`--provider`, `--model`, `--tokenizer`, ...) override
  the preset baseline in both interactive and non-interactive paths.
  (#326)

## [0.1.13] — 2026-04-20

memtomem remains in **alpha**. APIs, defaults, and on-disk config surfaces
may still shift between 0.1.x releases — external feedback and issue
reports are especially welcome at
[github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

### Added
- **`mm agent migrate` CLI**: renames legacy `agent/{id}` namespaces to
  `agent-runtime:{id}` (see `### Changed` below). Pass `--dry-run` to preview
  without applying. Safe to re-run — namespaces already in the new format
  are skipped (#318).
- **Wizard preset namespace rules**: `mm init` now appends matching
  `NamespacePolicyRule` entries to `namespace.rules` when you accept a
  provider category, so auto-discovered Claude-projects memory dirs route
  to a meaningful namespace instead of collapsing to `default` (#296).
  `claude-memory` → `claude:{ancestor:1}` (picks the project-id folder
  above the generic `memory` basename); `claude-plans` → `claude-plans`;
  `codex` → `codex`. Rules are deduplicated by `path_glob` (with `~`
  expansion on both sides) so re-running `mm init` is idempotent, and
  user-authored rules with the same `path_glob` but a different namespace
  are preserved rather than overwritten. The flag-driven non-interactive
  path (`--include-provider`) matches the interactive behavior. Labels are
  deliberately flat pending RFC #304 (`{provider, product}` hierarchy). The
  four-entry vocabulary (`user`, `claude-memory`, `claude-plans`, `codex`)
  is locked against silent expansion via an import-time assertion (#313).
- **Reranker candidate-pool scaling**: `rerank.oversample` (default `2.0`),
  `rerank.min_pool` (default `20`), and `rerank.max_pool` (default `200`).
  The cross-encoder now sees
  `max(min_pool, min(max_pool, int(oversample * top_k)))` candidates, so the
  classic 2× oversample holds at `top_k=10` (pool=20) and scales with
  larger requests (`top_k=50` → pool=100, `top_k=150` → pool=200). All
  three knobs plus `rerank.enabled` are runtime-tunable via `mm config set`
  and the Web UI — no restart required. `provider`/`model`/`api_key` still
  need a restart (reranker instance is cached).
- **Vendor → product grouping in the Memory Dirs panel**: the Sources tab
  now groups memory directories by vendor (`User`, `Claude`, `OpenAI`)
  with multi-product vendors (currently `Claude` → `Claude projects` +
  `Claude plans`) rendering products as nested sections. Single-product
  vendors keep the previous one-row layout with the product label
  (`User`, `Codex`). Driven by a new `provider` field on
  `GET /api/memory-dirs/status` so the client doesn't duplicate the
  category → vendor map. RFC #304 Phase 1–2 (#321 + #322). New i18n
  keys: `sources.memory_dirs.provider.{user,claude,openai}` (en + ko).

### Changed
- **Multi-agent namespace format**: `mem_agent_register` / `mem_agent_search`
  now generate `agent-runtime:{agent_id}` instead of the legacy
  `agent/{agent_id}`, aligning with the `{bucket}-{kind}:` convention used by
  `claude-memory:` and `codex-memory:` (#318). `/` is dropped from
  `_NS_NAME_RE` (reverting the temporary widening in #319) since no live
  caller needs it, and the duplicated `_NS_SAFE_RE` (ingest) +
  `_AGENT_ID_SAFE_RE` (multi-agent) sanitizers are consolidated into
  `sanitize_namespace_segment` in `storage/sqlite_namespace.py` (no allowlist
  change). Existing `agent/{id}` namespaces can be migrated with
  `mm agent migrate`.
- **Memory Dirs panel: per-child collapse removed** (behavior change):
  expanding the `Claude` vendor now reveals both `Claude projects` and
  `Claude plans` together — they no longer collapse independently. Old
  per-category collapse state was not persisted, so no migration is
  needed. First-load defaults unchanged (`User` open, vendor groups
  closed). No vendor-level bulk-reindex button; per-product reindex
  buttons remain on each product section. RFC #304 Q4/Q5 (#322).

### Fixed
- **Reranker candidate pool is now actually wired**: `RerankConfig.top_k`
  was declared but never read, so the cross-encoder only ever saw the
  response `top_k` and could not rescue items RRF ranked just outside it
  (#307).
- **Reranker-failure fallback now honors response size**: when the
  cross-encoder raises, `fused` is trimmed to the caller's `top_k`
  instead of leaking the wider pool size through the remaining pipeline
  stages (#309).

### Deprecated
- `rerank.top_k` (env var `MEMTOMEM_RERANK__TOP_K`) is superseded by
  `rerank.oversample` + `rerank.min_pool` + `rerank.max_pool`. Legacy
  configs are migrated to `rerank.min_pool` with a `DeprecationWarning`.
  Slated for removal in 0.3.

## [0.1.12] — 2026-04-19

memtomem remains in **alpha**. APIs, defaults, and on-disk config surfaces
may still shift between 0.1.x releases — external feedback and issue
reports are especially welcome at
[github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

### Changed
- **Provider memory directories are now opt-in via `mm init`.** The wizard
  has a new "Provider memory folders" step (Step 4 of 10) that detects
  Claude Code per-project memory (`~/.claude/projects/<project>/memory/`),
  Claude plans (`~/.claude/plans/`), and Codex memories
  (`~/.codex/memories/`) and lets you accept each category. Accepted paths
  land in `indexing.memory_dirs` directly, replacing the previous silent
  runtime auto-discovery. Non-interactive mode supports the new repeatable
  `--include-provider {claude-memory,claude-plans,codex}` flag.
- **Auto-discovery scope narrowed** to canonical memory surfaces per each
  provider's official documentation:
  - Claude Code: only the `*/memory/` subdirectories with at least one
    `.md` file (previously the entire `~/.claude/projects/` tree
    including session JSONL transcripts and `staging/`).
  - Codex: `~/.codex/memories/` (unchanged).
- **Gemini CLI removed from auto-discovery.** Its memory is the single file
  `~/.gemini/GEMINI.md` (incompatible with the directory-based
  `memory_dirs` abstraction), and the parent dir contains secrets like
  `oauth_creds.json`. Use `mm ingest gemini-memory` for one-shot manual
  import — that command is unchanged.

### Deprecated
- `indexing.auto_discover` is now a one-shot migration trigger only, not a
  runtime auto-discovery flag. Existing installs with the legacy default
  (`true`) get migrated transparently on the next CLI/server startup —
  canonical provider dirs that exist on the machine are appended to
  `indexing.memory_dirs` and the flag flips to `false`. The field will be
  removed in a future release.

### Migration notes
- After upgrading, run `mm index --rebuild` to clean up index entries left
  over from the previous wider scan (session transcripts, staging dirs,
  Gemini configs). The migration narrows `memory_dirs` but doesn't
  retroactively prune already-indexed content.
- New Claude Code projects created after running `mm init` are not
  auto-indexed — re-run `mm init` or use
  `mm config set indexing.memory_dirs` to add them when needed.

## [0.1.11] — 2026-04-19

memtomem remains in **alpha**. APIs, defaults, and on-disk config surfaces
may still shift between 0.1.x releases — external feedback and issue
reports are especially welcome at
[github.com/memtomem/memtomem/issues](https://github.com/memtomem/memtomem/issues).

### Added
- **FastEmbed reranker provider**: new `rerank.provider="fastembed"` routes
  reranking through `fastembed.rerank.cross_encoder.TextCrossEncoder` —
  local ONNX, no external service, no PyTorch dependency. Reuses the
  existing `memtomem[onnx]` extra so enabling reranking adds no new
  packages. Supports the built-in fastembed catalog (e.g.
  `Xenova/ms-marco-MiniLM-L-6-v2`,
  `jinaai/jina-reranker-v2-base-multilingual`) plus custom ONNX exports
  via `TextCrossEncoder.add_custom_model()`.
- **Chunking semantic pack**: new `indexing.target_chunk_tokens` (default
  384) drives a greedy Pass 2 that packs short hierarchy-compatible
  siblings/ancestor-descendants up to the target, plus a Pass 3 tail
  backward sweep for final-chunk orphans. Short orphans in Pass 1 are now
  rescued across sub-heading divergence as long as they share a top-level
  root (mem_add entries with distinct roots still stay separate). Set
  `target_chunk_tokens=0` to restore the pre-PR merge behaviour.
- **ReStructuredText chunker**: `.rst` section-header-aware splitting.
- **Web UI `--open` flag**: opt-in browser launch with configurable timeout
  (replaces the old always-open default).
- Numeric validation errors now include the offending value in MCP tool
  responses.
- **Namespace policy rules** (#253): new `NamespacePolicyRule` config list
  provides path-glob → namespace mappings, so users can auto-tag files at
  index time instead of passing `namespace=` on every `mem_index` call.
  Resolution order: explicit param → rules (first match) → `enable_auto_ns`
  → `default_namespace`. Uses `pathspec.GitIgnoreSpec` patterns
  (case-insensitive, same syntax as `indexing.exclude_patterns`) with a
  `{parent}` placeholder that expands to the matched file's immediate
  parent folder name. Contributes via `config.d/*.json` (APPEND merge).
  Default `[]` — existing users see no behavior change until they opt in.
  See `docs/guides/configuration.md`.
- **Wizard "Preserved" summary** (#254): `mm init` now lists non-default
  keys inherited from a previous config that the wizard didn't write this
  run, using a built-in-default diff (not a bool heuristic, so non-bool
  leftovers like `search.rrf_k=120` surface too). Malformed `config.json`
  is backed up to `config.json.bak-<unix-ts>` instead of silently
  overwritten. Transparency-only — write behavior unchanged.
- **`mm init --fresh`** (#255): opt-in flag that drops wizard-untouched
  canonical config keys whose values differ from built-in defaults, then
  runs the normal wizard. Complements PR #254's surfacing with bulk
  cleanup. Default behavior unchanged.
- **`mm config unset <key>`** (#259): targeted removal of a single
  override. Distinct from `mm init --fresh` (single-key vs bulk; no backup
  vs backup; idempotent scripting vs interactive wizard). Useful for stale
  cross-machine paths in `memory_dirs` or a single field shadowing a
  `config.d/` fragment.
- **Web UI per-field reset-to-default (↺) button** (#272): every Config
  field in the Web UI now has a ↺ action that restores the built-in
  default in place. Schema and choice metadata served via new
  `GET /api/config/defaults` + `/api/config/schema`; the frontend reads
  both and overlays a per-field reset affordance.
- **Web UI i18n coverage** (#281): remaining `showConfirm`/`showToast`
  dialog strings now route through `t()`, completing the Korean UI
  translation (closes #29).
- **`indexing.auto_discover` flag** (#282): opt-out of auto-discovery
  of `~/.claude/projects`, `~/.gemini`, `~/.codex/memories` from
  `memory_dirs`. Defaults to `True` — no behavior change for existing
  users. Use `mm config set indexing.auto_discover false` to pin
  `memory_dirs` to the explicit list in `config.json` + `config.d/*.json`.

### Fixed
- **Web UI config hot-reload** (#267, #269, #274):
  `~/.memtomem/config.json` and `config.d/*.json` are re-read on every
  `GET /api/config` and at the top of every config-writing endpoint
  (`PATCH /api/config`, `POST /api/config/save`,
  `POST /api/memory-dirs/add|remove`). Previously, external edits
  (`mm config set`, manual editor) were invisible to the running server
  and got silently clobbered on the next UI save. The writer lock was
  extended from PATCH to all four write handlers (closing a pre-existing
  gap). If `config.json` becomes invalid on disk, the UI keeps the last
  known-good config, surfaces `config_reload_error` in the response, and
  refuses writes with HTTP 409 until the file is fixed. Follow-up #269
  closes a GET-path signature overwrite race under concurrent requests;
  #274 mirrors the CAS guard onto the `reload_if_stale` error branch.
- **ONNX `bge-m3`**: fastembed 0.8.0 dropped `BAAI/bge-m3` from its built-in
  `TextEmbedding` catalog — re-registered via `add_custom_model` against the
  official HF ONNX export (1024-dim, CLS pooling, normalized). Existing
  `mm init` users keep working with no config change.
- **Async I/O**: 6 blocking file read/write calls in MCP tool handlers
  (`mem_add`, `mem_edit`, `mem_delete`, `mem_context_*`) wrapped with
  `asyncio.to_thread` to prevent event loop starvation.
- **Security**: parameterized query for namespace in `execute_auto_tag`
  (#155); exception class names removed from web error responses and
  `/health` endpoint (#80, #81).
- **Logging**: silenced errors surfaced in health watchdog, search
  pipeline, and consolidation engine (#164); silent `except: pass`
  blocks in `sqlite_backend` and `status_config` now log warnings (#78).
- **Auto-tag**: `namespace_filter` passed to `auto_tag_storage` to fix
  silent failure of namespace-scoped policies (#114).
- **CLI**: pass real `index_engine`/`config` to `FileWatcher` in
  `watchdog run` (#111); warn when code chunkers unavailable due to
  missing optional deps (#142).
- File handle leak on `flock` failure; stale `--include` help text;
  `response.ok` checks in `context-gateway.js` (#77).
- **Namespace rules via mutation path** (#257): `coerce_and_validate` now
  handles `list[BaseSettings]`, so `PATCH /api/config`, `mm config set
  namespace.rules '[...]'`, and the init wizard correctly coerce dict
  entries into `NamespacePolicyRule` instances. Previously the load path
  correctly coerced dict entries but the mutation path silently passed
  raw dicts through; downstream `rule.path_glob` access then raised
  `AttributeError`.
- **Fragment / env drag-in on save** (#258): in-process save paths (Web UI
  `PATCH /api/config`, `/memory-dirs/*`, MCP `mem_config`) now persist
  only fields whose values differ from a fresh comparand built from
  defaults + env + `config.d/` fragments. Previously, PATCH-ing one field
  silently copied fragment and env values into `config.json`'s REPLACE
  layer, freezing subsequent fragment edits. Extends #256's class-level
  default drop by broadening the comparand to include fragments and env.
- **Atomic config.json writes everywhere** (#262): `save_config_overrides`
  (every `mm config set` / Web UI PATCH / MCP `mem_config` /
  `/memory-dirs/add|remove`) and `_write_config_and_summary` (normal `init`
  + `init --fresh`) now use `_atomic_write_json` (tempfile + `os.replace`,
  tmp cleanup on failure). Prevents mid-write failure from corrupting
  `config.json` — the `--fresh` path's `shutil.copy2` backup + direct
  write could previously leave a half-written file next to a valid `.bak`
  on partial failure.
- **Indexing exclude guard coverage** (#271): moved the entry-point
  exclude guard from `index_file` into the innermost common seam
  `_index_file`, so sibling public entry `index_path_stream(single_file)`
  is also covered. Follow-up to the 0.1.10 security fix (#252).
- **Wizard `.mcp.json` scope clarification** (#280): wizard output now
  prints per-editor scope hints (Claude Code user vs project `.mcp.json`,
  Cursor global vs project) so users don't paste the same block in both
  scopes.
- **Context atomic writes + name validation** (#283): all 6 context
  fan-out sites (profile/skill/prompt read-modify-write paths) now use a
  shared `atomic_write_{bytes,text}` helper (`0o600` default, `fsync` +
  `os.replace`). Profile/skill/prompt names are validated through
  `validate_name` at parse + extract, rejecting `..`, path separators,
  control characters, and names longer than 64 bytes.
- **Context CRLF tolerance + TOML escape** (#285): CRLF line endings are
  now accepted everywhere, unknown keys warn instead of failing the
  whole parse, and full TOML-style escape sequences (`\n`, `\t`, `\"`,
  `\\`, `\uXXXX`) are honored in string values.
- **Context-gateway write serialization** (#286): dedicated `asyncio`
  lock prevents interleaved writes when multiple endpoints (context
  gateway, skill editor, profile editor) mutate the same file in quick
  succession.
- **Auto-discover regression in `build_comparand`** (#284):
  `ensure_auto_discovered_dirs` now also runs during the comparand
  build, so an unrelated save doesn't drag auto-discovered directories
  into `config.json`'s REPLACE layer. Post-merge follow-up to #282.
- **FTS rebuild singleton + thread offload** (#287): Web UI FTS rebuild
  now coalesces concurrent rebuild triggers into a singleton task and
  runs on `asyncio.to_thread` with a dedicated writer connection, so
  large indexes don't stall the event loop or serialize redundant
  rebuilds.

### Changed
- **Single-source version** via `importlib.metadata` + Python 3.13
  classifier (#76).
- **Typing overhaul**: `CtxType` widened to `Optional` (drops 82
  `type: ignore`, #90); `policy_engine` storage narrowed to
  `SqliteBackend` (#89); 12 `union-attr` ignores eliminated (#100);
  RST chunker annotations corrected (#126); `llm_provider` tightened
  from `object` to `LLMProvider` (#130); 4 list/dict element types
  tightened (#145).
- Dead config sections removed (`conflict`, `entity_extraction`,
  `timezone`) — `extra="ignore"` ensures old config files still load.
- **MiniLM pooling upstream change**: fastembed 0.8.0 switched
  `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` from CLS to
  mean pooling. Users who indexed with fastembed <0.5.1 and this model
  should re-index for consistent dense-search quality; new installs are
  unaffected.
- **Reranker default**: `rerank.provider` now defaults to `fastembed`
  (local ONNX, ~80 MB download on first use) instead of `cohere`
  (external API); `rerank.model` default is now
  `Xenova/ms-marco-MiniLM-L-6-v2`. Installs that had
  `rerank.enabled=true` with the implicit `cohere` default must now set
  `provider: "cohere"` (and `api_key`) explicitly to keep prior behavior.
  `rerank.enabled=false` installs (the shipped default) are unaffected.
  Non-English content should set
  `model="jinaai/jina-reranker-v2-base-multilingual"` — the English
  default degrades non-English quality.
- Refactored truncation magic numbers in consolidation engine;
  watcher queue maxsize extracted to constant.
- CI: ruff lint/format scope extended to `tests/`; notebooks CI job
  and branch protection added.
- **Silent-leftover prevention on save** (#256): every mutable-field save
  path (`mm config set`, `PATCH /api/config?persist=true`,
  `POST /api/config/save`, `/memory-dirs/add|remove`, `mem_config` MCP)
  now drops fields whose values equal the class-level default and prunes
  matching historical leftovers on next save. Stops Web UI section-saves
  from pinning default-False `mmr.enabled` into `config.json` and
  permanently shadowing `config.d/` fragments.

### Docs
- Webhook config section and `indexing.supported_extensions` added to
  configuration reference (#170 high tier).
- MCP tool error response contract documented (#167).
- **Beginner-surface restructure** (#288): move WIP / internal /
  power-user-only docs into a private `memtomem/memtomem-docs` repo.
  The public surface is now intentionally small:
  `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `CLA.md`,
  `SECURITY.md`, `docs/adr/`, `docs/guides/` (4 intro + 4 power-user
  guides), and `packages/memtomem/README.md` (PyPI page).
- **Notebooks slim** (#289): public `examples/notebooks/` now contains
  `01_hello_memory.ipynb` (5-minute Python-API quick-start) only.
  Notebooks 02–08 (filters, agent patterns, search tuning, LangGraph,
  lifecycle, embedding providers, LLM features) were moved to the same
  private `memtomem-docs` repo as internal reference material.

## [0.1.10] — 2026-04-19

### Security

- Fix credential-file indexing on filesystem-watch events.

  **Affected versions**: 0.1.0 through 0.1.9.

  In these versions, the fs watcher's per-file re-index path
  (`IndexEngine.index_file`) did not apply the directory-exclude
  filter. Any supported-extension file (`.json`, `.yaml`, `.py`, ...)
  inside an auto-discovered memory directory (`~/.claude/projects`,
  `~/.gemini`, `~/.codex/memories`) was indexed on each modify event.
  For users running memtomem alongside Gemini CLI, the ~hourly OAuth
  token refresh drove continuous re-indexing of `~/.gemini/oauth_creds.json`.

  The fix combines changes across PRs #225 / #226 (built-in denylist +
  config + cleanup CLI), #252 (entry-point guard), and #251
  (documentation):

  - **PRs #225 / #226** — built-in credential/secret denylist
    (`oauth_creds.json`, `credentials*`, `id_rsa*`, `*.pem`, `*.key`,
    `.ssh/**`, Claude Code subagent metadata
    `.claude/**/*.meta.json`); directory denylist extended with `.aws`,
    `.ssh`, `.gnupg`; user-configurable `indexing.exclude_patterns`
    config field (`.gitignore` syntax, case-insensitive via `pathspec`;
    user `!negation` cannot override built-in secret patterns); and
    `mm purge --matching-excluded` cleanup CLI.
  - **PR #252** — entry-point guard at `IndexEngine.index_file` matching
    both absolute paths and memory-dir-relative paths.
  - **PR #251** — documentation of `exclude_patterns`, cloud-sync
    watcher edge cases, and related configuration surface in
    `docs/guides/configuration.md` and `docs/guides/google-drive.md`.

  **Upgrade action**: `pip install -U memtomem` to 0.1.10. No config
  migration required — any existing user `exclude_patterns` stack on
  top of the built-in denylist.

  **Post-upgrade recommended**:
  1. Dry-run the cleanup: `mm purge --matching-excluded` (prints what
     would be deleted).
  2. Apply it: `mm purge --matching-excluded --apply` to remove
     pre-existing chunks whose source paths match the denylist.
  3. Rotate any credentials that may have been indexed during the
     affected period. Gemini CLI refreshes OAuth tokens on an ~hourly
     schedule, so any v0.1.x server running alongside Gemini CLI
     should be treated as having refreshed copies of those tokens in
     the index. Also review any sensitive content under
     `~/.claude/projects` (Claude Code session/conversation data) and
     `~/.codex/memories` that may have been indexed, and handle per
     your usual data-handling policy.

  **Follow-up tracking** (defense-in-depth concerns surfaced during
  this work, not required for the security fix):
  - #260 — auto-discover unconditional override (design RFC)
  - #261 — post-#252 watcher residual investigation

## [0.1.9] — 2026-04-13

### Fixed
- **Config robustness**: invalid `config.json` values now warn and fall
  back to defaults instead of crashing on startup; `mm init` preserves
  non-init config fields on re-run; 4 cross-path sync gaps closed
  (CLI/Web/MCP all converge on the same read-merge-write logic);
  save-path data-loss edge case eliminated.
- **Embedding**: Ollama `base_url` defaults to `localhost:11434` when
  the env var is set but empty.

### Changed
- Default LLM models updated to latest releases.

### Docs
- Official website link added to README.
- Docs sweep: stale numbers, env vars, and notebook setup corrected
  across 14 doc files; missing onnx extra and CLI commands added to
  getting-started; Gemini CLI setup and tool categories added to
  reference sections; core MCP tool docstrings corrected.

## [0.1.8] — 2026-04-13

### Added
- **Structured search output**: `mem_search(output_format="structured")`
  returns JSON with `chunk_id`, `namespace`, `score`, `source`,
  `hierarchy`, and full `content` per result. Enables STM proxy to use
  real UUIDs for `increment_access` feedback instead of sha256 fallbacks.
- **Version negotiation**: `mem_do(action="version")` returns server
  version and capabilities JSON (e.g. `search_formats`). Used by STM
  proxy to discover supported features before switching parsers.
- **Auto-discover AI tool memory directories**: `memory_dirs` now
  automatically includes `~/.claude/projects`, `~/.gemini`, and
  `~/.codex/memories` when they exist, so `mem_index` and the file
  watcher accept paths under these directories without manual
  `MEMORY_DIRS` configuration. Auto-discovered directories are appended
  after `config.json` overrides via `ensure_auto_discovered_dirs()`.
- **Database reset**: `mm reset` CLI command, `mem_reset` MCP tool (advanced
  category, routed via `mem_do`), `POST /api/reset` web endpoint, and
  Settings > Maintenance > Reset tab in Web UI. Deletes all data (chunks,
  sessions, history, relations, entities, policies, health snapshots) and
  reinitializes the DB; embedding configuration is preserved.
- **Namespace prefix grouping**: Web UI Settings tab and namespace
  dropdowns group namespaces by colon prefix (collapsible sections).
- **Improved web extra messaging**: `mm web` and `mm init` now explain that
  the `[web]` extra is not included in the base install, reducing confusion
  when `uv tool install memtomem` registers the `memtomem-web` entry point
  but FastAPI is missing.

### Fixed
- **Hooks record format**: migrated context gateway hooks from array to
  record format for Claude Code ≥ 2.1.104 compatibility.
- **Home activity graph**: rebuilt as responsive GitHub-style contribution
  grid.
- **Codex commands**: removed phantom Codex commands generator (feature
  doesn't exist upstream).

### Changed
- **Web JS refactor**: split `app.js` (3554 lines) into core + 8 domain
  modules. No build step; global function dependencies preserved.

### Docs
- Uninstall instructions in user guide and getting started.
- Consolidated scope section in Agent Context Management.
- Hooks references updated to record format.
- Stale tool counts, wizard step count, and tags parameter type fixed.

## [0.1.7] — 2026-04-12

### Added
- **PolicyScheduler**: background loop that periodically runs all enabled
  memory lifecycle policies (`auto_archive`, `auto_consolidate`,
  `auto_expire`, `auto_tag`). Controlled by `MEMTOMEM_POLICY__ENABLED`
  and `MEMTOMEM_POLICY__SCHEDULER_INTERVAL_MINUTES`. Follows the existing
  `ConsolidationScheduler` / `HealthWatchdog` lifecycle pattern.
  - `run_all_enabled()` gains a `max_actions` parameter — cumulative
    action cap checked between policies (individual handlers run
    atomically). Configurable via `MEMTOMEM_POLICY__MAX_ACTIONS_PER_RUN`.
  - Consecutive failure counter: escalates to WARNING after 3 failures.
  - Cache invalidation only when mutations actually occur.
- **`auto_promote` policy handler** — inverse of `auto_archive`. Moves
  archived chunks back to an active namespace when access patterns
  indicate continued relevance (`min_access_count`, `recency_days`,
  `min_importance_score`). Ping-pong prevention: promotion resets
  `last_accessed_at` to now.
- **Gemini / Codex memory ingest**: `mm ingest gemini-memory` indexes
  `GEMINI.md` files (namespace `gemini-memory:<slug>`);
  `mm ingest codex-memory` indexes Codex `~/.codex/memories/` directories
  (namespace `codex-memory:<slug>`). Shared infrastructure via
  `_build_namespace(prefix=)` and `tag_fn` parameters.
- **Multi-slug Claude ingest**: `mm ingest claude-memory --source
  ~/.claude/projects/` auto-discovers all `<slug>/memory/` subdirectories
  and ingests them in a single run with per-slug + aggregate output.
- **MCP `mem_ingest` tool**: `mem_do(action="ingest")` exposes all three
  ingest commands (Claude, Gemini, Codex) via MCP, including multi-slug
  discovery for `source_type="claude"`.
- **Web UI: Hooks Sync** — new Settings subsection for comparing and
  resolving conflicts between memtomem's canonical hooks and Claude's
  `~/.claude/settings.json`. Per-conflict resolution with mtime guard.
- **Web UI: Korean i18n** — language toggle (EN/한) in the header.
  Auto-detects browser locale; persists choice in `localStorage`. All
  static labels translated via `data-i18n` attributes and `t()` function.

### Fixed
- **MCP tool registration audit** — 9 issues resolved: orphaned
  `mem_ask` import, incomplete `ns_assign`/`cleanup_orphans` registration,
  missing `ingest`/`search`/`context` categories in `mem_do` docstring,
  shutdown isolation, missing `@tool_handler` on `mem_increment_access`,
  and atexit ordering.

### Docs
- User guide Section 8: Memory Policies (5 types, 4 MCP tools,
  scheduler, combining patterns).
- Configuration reference: `auto_promote`, `auto_consolidate` config keys.
- Web UI guide: Hooks Sync and i18n sections.

## [0.1.6] — 2026-04-12

### Added
- **Phase D: Claude `settings.json` integration** — new `SettingsGenerator`
  protocol and `ClaudeSettingsGenerator` implementation for merging memtomem
  hooks into `~/.claude/settings.json`. Completes the LTM Manager roadmap
  (Phases A → A.5 → B → C → D) and absorbs context-gateway Phase 4.
  - New `--include=settings` flag for `mm context {generate,sync,diff,detect}`
    (CLI and MCP `mem_context_*` tools).
  - New `mm init` wizard step (Step 8) prompts for Claude Code hooks setup.
  - Canonical source: `.memtomem/settings.json` with a `hooks` record
    (keyed by event name, e.g. `PostToolUse`).
  - Additive-only merge: rules are matched by `(event, matcher)`; on
    collision the user's existing rule wins and a guided warning is emitted.
  - Formatting: `json.dumps(indent=2)` normalization — byte-for-byte
    preservation of hand-edited formatting is explicitly not guaranteed.
  - Malformed `~/.claude/settings.json` is skipped with an error message
    (not silently overwritten).
  - If Claude Code is not installed (`~/.claude/` missing), the settings
    runtime is silently skipped — memtomem never creates `~/.claude/`.
  - Basic concurrent-write guard via mtime comparison between read and write.

## [0.1.5] — 2026-04-12

### Added
- Phase 3.5: canonical slash commands now fan out to Codex as well
  (`~/.codex/prompts/<name>.md`, user-scope). Codex's custom-prompts
  format is a Claude-compatible Markdown + YAML superset — `description`,
  `argument-hint`, and the `$ARGUMENTS` / `$1..$9` / `$NAME` / `$$`
  placeholders are all passed through verbatim; only `allowed-tools`
  and `model` are dropped (reported via the standard `dropped` channel).
  Codex custom prompts are upstream-deprecated — OpenAI recommends
  migrating to skills, which memtomem already fans out to Codex via
  `.agents/skills/` in Phase 1 — but fan-out is provided for parity
  with the existing Claude + Gemini pipeline. The `mem_context_*` MCP
  tools and the `mm context {generate,sync,diff} --include=commands`
  CLI pick up the new `codex_commands` runtime automatically via the
  registry (no new tools or flags). `extract_commands_to_canonical`
  intentionally still skips Codex — user-scope paths span projects,
  matching the Phase 2 Codex sub-agent policy.

## [0.1.4] — 2026-04-11

### Added
- `examples/notebooks/` — six scenario-based Jupyter notebooks that walk
  through the Python API (`create_components()`, `search_pipeline.search()`,
  `index_engine.index_path()`, storage mixins, and `MemtomemStore` for
  LangGraph). Covers hello-memory, bulk indexing + filters, session /
  scratch / recall, search tuning, a two-node LangGraph agent, and the
  full memory lifecycle (hash-diff incremental re-index on edit,
  single-chunk delete via `storage.delete_chunks`, orphan cleanup via
  `delete_by_source`, and `force=True` full re-embed). Each notebook
  runs against a throwaway temp directory so it cannot touch the user's
  real `~/.memtomem/` setup.
- Notebook 02 includes a "Korean with the kiwipiepy tokenizer" section
  that prints the token stream produced by `unicode61` vs. `kiwipiepy`
  side by side and runs the same query under each configuration.
- `examples/notebooks/README.md` now has a "How memories are stored"
  section that explains the file-backed (`index_file` path used by
  notebooks 01/02/04/05/06) vs DB-only (`create_session`, `scratch_set`,
  … used by notebook 03) storage paths and the shared temp directory
  layout every notebook relies on.
- `docs/guides/hands-on-tutorial.md` gained steps 3.6 / 3.7 covering the
  file lifecycle from the MCP side: reading `mem_index` `Indexed` /
  `Skipped (unchanged)` / `Deleted (stale)` stats after a file edit,
  `mem_index force=true` full re-embed for model swaps, and
  `mem_do action="orphans"` (dry-run → apply) to clean up chunks whose
  source file was deleted. Step 1.2 now also documents the
  `MEMTOMEM_TOOL_MODE` env var and which tutorial steps use the `mem_do`
  routing vs top-level calls.

### Changed
- `SqliteBackend.clear_embedding_mismatch()` is now a public method
  (refactor 15136a0). The `needs_reindex_ids` and `needs_embed_ids`
  tracking sets were previously reset via direct attribute mutation
  through the protected `_backend` accessor, which leaked internal
  state across module boundaries. Four writers (`_finalize_write`,
  `_reset_all_state`, `web/app.py`'s force-reindex handler, and the
  FTS rebuild path) now go through the public method, and the
  protected-attribute touch is no longer needed outside storage.
- STM decoupling CSS sweep — removed ~164 lines of orphan dashboard
  CSS from `packages/memtomem/src/memtomem/web/static/style.css`.
  The `.stm-*` block (59 lines, #15) and the parallel `.proxy-*`
  plus `.trend-*` block (105 lines, #16, covering Proxy Settings,
  Proxy Diff View, and Compression Trend Chart) had no HTML/JS
  consumers — any rendering path for these selectors had already
  moved to the external `memtomem-stm` package when STM was split
  out. The six `--bg-*` / `--text-*` CSS aliases they previously
  shared are retained since `.harness-*` sections still consume
  them; the comment on `style.css` line 24 that documented their
  purpose was rewritten to match the current consumers. The
  `.health-*` rules are kept intact — `app.js` still uses them for
  the generic system-health summary, which is unrelated to proxy.
- `app_lifespan(server: FastMCP)` → `app_lifespan(_server: FastMCP)`
  in `packages/memtomem/src/memtomem/server/lifespan.py`. The MCP
  framework requires the parameter in the callback signature but
  memtomem's lifespan never reads it; the underscore prefix makes
  the "intentionally unused by framework contract" nature explicit
  and silences dead-code detectors.

### Fixed
- `docs/guides/user-guide.md` tab-overview table listed an **STM**
  row (`Proxy monitoring — compression metrics, server status, call
  history (only when STM installed)`) that described the dashboard
  UI removed with the STM decoupling. The actual
  `packages/memtomem/src/memtomem/web/static/index.html` has seven
  tabs (Home, Search, Sources, Index, Tags, Timeline, More) — no
  STM tab, and the styles backing the removed row were already
  gone after #15 and #16. Dropped the stale row from the table.
  The separate "STM: Proactive Memory Surfacing (Optional)" section
  further down the same file is intentionally kept since it
  correctly documents the external `memtomem-stm` package as a
  cross-reference, not a core UI feature.
- `MemtomemStore.index()` (LangGraph adapter) and the `mm` shell `index`
  command called a nonexistent `IndexEngine.index_directory()` method and
  would crash at runtime. Routed both to `index_path()` and added
  regression tests in `tests/test_langgraph.py`.
- `docs/guides/hands-on-tutorial.md` steps 3.2 / 3.3 / 3.4 used to call
  `mem_batch_add` / `mem_edit` / `mem_delete` as top-level tools, but
  those are non-core actions — readers following the tutorial with the
  default MCP config (`MEMTOMEM_TOOL_MODE=core`) would hit "tool not
  found" errors. All three call sites now go through
  `mem_do(action="...", params={...})`, matching the default tool set.
- `docs/guides/hands-on-tutorial.md` `mem_status` / `mem_stats` example
  outputs had drifted from the real formats in
  `server/tools/status_config.py`. Step 1.3 showed a one-line
  `Chunks: 0 | Sources: 0` form that the code has never produced;
  step 3.5 showed `Chunks: 12 | Sources: 4 | Storage: sqlite` as the
  `mem_stats` response. Both now show the actual multi-line output
  (`memtomem Status` header with `Storage` / `DB path` / `Embedding` /
  `Dimension` / `Top-K` / `RRF k` and an `Index stats` section for
  `mem_status`; `Memory index statistics:` header plus bullet list
  for `mem_stats`).
- `docs/guides/user-guide.md` `mem_index` examples likewise did not
  match the real "`Indexing complete: ...`" block — the Index-a-directory
  response was `"Indexed 47 files (312 chunks)"` and the Incremental
  re-indexing response used a `"3 new, 2 updated, 1 deleted"` phrasing
  that does not correspond to any code path. Both now use the real
  multi-line format (`Files scanned` / `Total chunks` / `Indexed` /
  `Skipped (unchanged)` / `Deleted (stale)` / `Duration`) and the
  section now explains that an edited section contributes to **both**
  `Indexed` (new hash) and `Deleted (stale)` (old hash) because the
  diff is hash-based.
- Broad docs-vs-source audit (commit after 75d7146) found the same
  class of drift in several more places. Fixed:
  - `docs/guides/agent-memory-guide.md` — every non-core tool call
    (`mem_scratch_set/get/promote`, `mem_session_start/end`,
    `mem_procedure_save`, `mem_consolidate(_apply)`, `mem_reflect(_save)`,
    `mem_eval`, `mem_agent_register/share/search`, `mem_fetch`) was
    shown as a top-level call, which fails in the default
    `MEMTOMEM_TOOL_MODE=core`. Every call is now routed through
    `mem_do(action="...", params={...})`, with a tool-mode note at the
    top of Scenario 1 pointing at the existing Tool Mode Configuration
    section. The companion example outputs were also rewritten to
    match the real return strings from `session.py`, `scratch.py`,
    `procedure.py`, `consolidation.py`, `reflection.py`,
    `evaluation.py`, `multi_agent.py`, and `url_index.py` (e.g. the
    `- ` dash prefixes on `Session started`/`Agent registered`
    outputs, the extra "Use namespace='...' for ..." two-line hint in
    `agent_register`, the real `Memory added to ... / - Chunks
    indexed / - File` shape from `mem_add` including in the template
    scenarios).
  - `docs/guides/user-guide.md` Google Drive section had another
    `"Indexed 47 files (312 chunks)"` one-liner alongside the one
    already fixed in section 1. Now uses the canonical
    `Indexing complete:` block.
  - `docs/guides/use-cases.md` Coding Tools section showed
    `mem_stats() > "Total chunks: 0, Storage backend: sqlite"` and
    `mem_index(path="...") > "Indexed 47 files, 1284 chunks"`. Both
    replaced with the real multi-line responses.
  - `docs/guides/integrations/claude-code.md` and
    `docs/guides/integrations/claude-desktop.md` First-Indexing
    examples both showed `→ "Indexed 47 files, 1284 chunks in 3.2s"`
    — the `in 3.2s` suffix never existed in the code. Replaced with
    the real `Indexing complete:` block (`Duration: 3200ms`).
  - `docs/guides/integrations/claude-code.md` UserPromptSubmit and
    PostToolUse hook examples called `memtomem search` / `memtomem
    index` as shell commands, but the installed CLI binary is `mm`
    (the `memtomem` entry point is for the MCP server). Copying the
    config as-is would have produced `command not found`. Changed
    both the `command:` values and the Hook Event Summary table to
    use `mm search` / `mm index`.
  - `docs/guides/hands-on-tutorial.md` Step 3.1 `mem_add` example
    showed `"Added 1 chunk (saved to ...)\nTags: python, typing"`
    which also does not match the real `memory_crud.py:116` return
    (`Memory added to ... / - Chunks indexed / - File`). Updated.

## [0.1.3] — 2026-04-10

Quality & security audit: 79+ fixes across nine audit rounds.

### Security
- Path traversal guard on source validation and symlink resolution.
- Webhook SSRF protection (private IP / internal host blocking).
- Recursion depth limit for structured-data (JSON/YAML/TOML) chunking.
- Binary file detection so non-text files are skipped during indexing.
- Namespace validation and shell crash guard.
- File size limit enforcement during ingestion.

### Fixed
- Cache race conditions and invalidation gaps in the search pipeline.
- Index lock handling and rollback consistency on partial failures.
- WAL checkpoint handling to prevent DB growth.
- Retention policy correctness and persistence reliability.
- Batch query correctness under concurrent access.
- Resource leaks (file handles, DB connections, embedder clients).
- Float epsilon handling in scoring; overlap cap enforcement in chunking.
- Cache TTL snapshot and lock-timeout races.

## [0.1.2] — 2026-04-10

### Added
- Session and activity tracking CLI: `mm session start/end/list/events`,
  `mm activity log`, and `mm session wrap -- CMD` to wrap headless
  processes with a session lifecycle.
- PostToolUse and Stop hooks for automatic activity logging.
- Timezone config: `MEMTOMEM_TIMEZONE=Asia/Seoul` (display only, storage
  stays UTC).
- Web UI sessions panel with event type badges, expandable metadata, and
  client-side filtering.
- `parent_context` and `file_context` metadata on chunks for better
  retrieval context.

### Changed
- Sibling heading sections (same parent) merge when short to reduce chunk
  fragmentation. Top-level `mem_add` entries stay independent of sibling
  merge.
- Token estimation uses a dynamic ratio: 4 for English, 2 for Korean.

### Fixed
- SQLite `busy_timeout=10` prevents "database is locked" when the CLI and
  MCP server access storage concurrently.
- MCP server PID lock warns about duplicate instances instead of silently
  racing on writes.

## [0.1.1] — 2026-04-10

### Added
- `mm init --non-interactive` mode for CI and automation.
- Project-scoped install support via `uv add memtomem`.

### Changed
- README optimized as a GitHub profile landing page (163 → 115 lines);
  PyPI badge and ecosystem section added.
- `mm init` docs clarified to drop the unneeded `uv run` prefix after
  `uv tool install`; README Quick Start leads with explicit install +
  wizard.

### Fixed
- `mem_add` produced duplicate chunks because `index_entry` and
  `index_file` were two separate indexing paths. Removed `index_entry`
  and routed all ingestion through `index_file`.
- `mm init` wrote `MEMORY_DIRS` as a plain string into `.mcp.json`,
  which crashed the server on startup. The wizard now serialises list
  env vars as JSON (#13).
- `mm web` surfaces an actionable error when the `[web]` extra is
  missing instead of failing with a bare `ModuleNotFoundError` (#14).

## [0.1.0.post1] — 2026-04-10

Metadata-only re-release; no code changes.

### Changed
- Corporate ownership recorded as DAPADA Inc. alongside the memtomem
  contributors in package authors and `LICENSE`.
- `Issues` URL added to PyPI project metadata (#12).

## [0.1.0] — 2026-04-08

Initial open-source release.

### Core (memtomem)
- MCP server with 72 tools + `mem_do` meta-tool (65 actions, aliases)
- CLI (`memtomem` / `mm`): init, search, add, recall, index, config, context, shell, web, watchdog
- Web UI dashboard: search, sources, tags, sessions, health report
- Hybrid search pipeline: BM25 (FTS5) + dense vectors (sqlite-vec) + RRF fusion
- Multi-stage pipeline: query expansion → parallel retrieval → RRF → time-decay → reranking → MMR → access boost → context-window expansion
- Context-window search (small-to-big retrieval): `search(context_window=N)` + `mem_expand` action
- Tool modes: `core` (9 tools), `standard` (~32), `full` (72)

### Storage
- SQLite with FTS5, sqlite-vec, WAL mode, read pool (3 connections)
- Mixin architecture: Session, Scratch, Relation, Analytic, History, Entity, Policy
- Incremental indexing with SHA-256 content hashing

### Chunking
- Markdown: heading-aware sections with frontmatter/wikilink support
- Python: AST-based splitting at function/class boundaries
- JavaScript/TypeScript: tree-sitter parsing
- JSON/YAML/TOML: structure-aware splitting

### Embedding
- Ollama (local, default `nomic-embed-text` 768-dim)
- OpenAI (cloud)
- `bge-m3` recommended for multilingual (KR/EN/JP/CN)

### Agent Memory
- Episodic (sessions), working (scratchpad with TTL), procedural (workflows)
- Multi-agent namespaces, cross-references, entity extraction
- Memory policies (auto-archive/expire/tag), consolidation/reflection

### Integrations
- LangGraph adapter (`MemtomemStore`)
- Claude Code plugin (experimental)
- OpenClaw plugin (experimental)

### Security
- XSS: DOMPurify sanitization
- SSRF: private IP/internal host blocking
- Path traversal: source validation, symlink rejection
- SQL injection: all queries parameterized

### Testing
- 886 automated tests
- CI: GitHub Actions (lint, typecheck, test)

### Related projects
- [**memtomem-stm**](https://github.com/memtomem/memtomem-stm) — Short-Term Memory proxy gateway with proactive memory surfacing. Distributed as a separate package; communicates with memtomem core entirely through the MCP protocol.
