# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

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
- MCP server with 72 tools + `mem_do` meta-tool (63 actions, aliases)
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
