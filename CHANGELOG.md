# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

### Added

- **`mm --version` flag** — Click's idiomatic entry point for version output,
  added via `click.version_option` at the group level. Emits
  `memtomem X.Y.Z`. (#330)
- **`mm config show --json`** — alias of `--format json`, added to align with
  the documented CLI output convention (binary human/machine scenario uses
  `--json`). Both flag forms emit identical output. (#332)

### Changed

- **Documented CLI output convention** — `CONTRIBUTING.md` now spells out
  when to use `--json` (binary scenario) vs `--format [table|json|...]`
  (genuine non-JSON modes like `plain` / `context` / `smart`), with a
  forward-compatibility guidance to prefer `--format` when a command might
  grow additional modes later. (#332)

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
