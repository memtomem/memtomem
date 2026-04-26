# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

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
  left in place; memtomem just stops writing there.

  `detect_agent_dirs` now also picks up `.codex/agents/<name>.toml`
  alongside Claude/Gemini, so `mm context detect` and the Web UI
  Context Gateway list project-scope Codex agents in their inventory.

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
