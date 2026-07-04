# Data, config & the CLI

Back up, restore, and ingest memories; inspect and change settings; and the full `mm` command reference.

[← memtomem Reference](../reference.md)

**On this page**

- [6. Data — mem_export, mem_import](#6-data--mem_export-mem_import)
- [7. Config — mem_stats, mem_status, mem_config, mem_embedding_reset](#7-config--mem_stats-mem_status-mem_config-mem_embedding_reset)
- [CLI Reference](#cli-reference)
- [Moving artifacts between tiers and projects](#moving-artifacts-between-tiers-and-projects)

---

## 6. Data — `mem_export`, `mem_import`

### Backup

```
mem_export(output_file="~/backup.json")
mem_export(output_file="~/work-backup.json", namespace="work")
mem_export(output_file="~/recent.json", since="2026-03-01")
```

Exports chunks with content, metadata, tags, and embeddings as a JSON bundle.

### Restore

```
mem_import(input_file="~/backup.json")                                # skip duplicates (default)
mem_import(input_file="~/backup.json", namespace="imported")
mem_import(input_file="~/backup.json", on_conflict="update")          # overwrite metadata
mem_import(input_file="~/backup.json", on_conflict="duplicate")       # pre-v2 behaviour
mem_import(input_file="~/backup.json", preserve_ids=True)             # keep bundle UUIDs
```

Import re-embeds all chunks, so it works across different embedding models or machines.

**Conflict resolution** (`on_conflict`, default `"skip"`):

- `"skip"` — drops records whose content already exists in the DB. Re-importing
  the same bundle is a no-op, and merging bundles with overlapping content
  adds only the unique side. Recommended for cross-PC sync.
- `"update"` — records matching an existing content hash overwrite that row's
  metadata (tags, namespace, heading hierarchy, source_file). The existing
  UUID is preserved.
- `"duplicate"` — no hash check; every record is inserted with a fresh UUID.
  Pre-v2 behaviour, produces row-level duplicates when re-importing.

`preserve_ids=True` reuses the bundle's original chunk UUIDs for new inserts
(v2 bundles only; UUID-identity across instances). Ignored in `"duplicate"`
mode.

### Importing from Obsidian

```
mem_do(action="import_obsidian", params={"vault_path": "~/obsidian-vault", "namespace": "notes"})
```

How Obsidian files are processed:
- **YAML frontmatter**: `tags` are automatically extracted and applied as memtomem chunk tags. Other frontmatter fields are included in the searchable content.
- **Wikilinks**: `[[target|alias]]` is resolved to `alias`, `[[target]]` to `target` — the `[[` brackets are removed during chunking so search results show clean text.
- **Heading-based chunking**: Each `##` heading becomes a separate chunk, just like native memtomem files.
- **Output**: Imported files are copied to `~/.memtomem/memories/_imported/obsidian/`.

> For *continuous* sync of an Obsidian vault as your live `memory_dirs` (rather
> than one-shot ingest), see [Multi-device sync § Obsidian as editor](../multi-device-sync.md#obsidian-as-editor-on-top-of-git-transport).

### Importing from Notion

```
mem_do(action="import_notion", params={"path": "~/notion-export.zip", "namespace": "notion"})
```

### Ingesting Claude Code auto-memory

`mm ingest claude-memory` takes a **read-only snapshot** of a Claude Code
auto-memory directory (`~/.claude/projects/<slug>/memory/`) and makes it
searchable under namespace `claude-memory:<slug>`. Unlike the Obsidian/Notion
importers, source files are **not copied** — `source_file` points at the
original absolute path, so the files stay under Claude's control.

```
mm ingest claude-memory --source ~/.claude/projects/<slug>/memory/ --dry-run
mm ingest claude-memory --source ~/.claude/projects/<slug>/memory/
```

How Claude memory files are processed:
- **Namespace**: `claude-memory:<slug>`, where `<slug>` is the directory
  name under `~/.claude/projects/`. Characters outside the namespace
  allowlist are replaced with `_`.
- **Tags**: every chunk gets `claude-memory`. Files matching a known
  prefix also get a type tag: `feedback_*` → `feedback`, `project_*` →
  `project`, `user_*` → `user`, `reference_*` → `reference`.
- **Excluded**: `MEMORY.md` and `README.md` are skipped — the first is a
  table of contents, the second is meta documentation. Indexing either
  would pollute search with a high-score duplicate on every query.
- **Delta on re-run**: `mm ingest claude-memory` uses content-hash
  comparison just like `mem_index`, so re-running on the same directory
  only re-indexes files whose content actually changed.
- **One-way**: there is no sync back. memtomem never writes to the source
  directory. If you edit a feedback note in memtomem's web UI, Claude's
  auto-memory on disk is unchanged — and vice versa, new Claude memories
  appear only after you re-run `mm ingest claude-memory`.

**Multi-slug discovery**: passing a parent directory instead of a single
`memory/` folder auto-discovers every `<slug>/memory/` underneath:

```
mm ingest claude-memory --source ~/.claude/projects/
```

Per-slug results are printed individually, followed by an aggregate total.

### Ingesting Gemini CLI memory

`mm ingest gemini-memory` indexes a Gemini CLI `GEMINI.md` file. Global
memories live at `~/.gemini/GEMINI.md`; per-project memories sit in the
project root. The Antigravity CLI (`agy`, Gemini CLI's successor) reads the
same `GEMINI.md`, so this command serves Antigravity users unchanged.

```
mm ingest gemini-memory --source ~/.gemini/GEMINI.md --dry-run
mm ingest gemini-memory --source ~/.gemini/
```

- **Namespace**: `gemini-memory:<slug>`. `~/.gemini/GEMINI.md` becomes
  `gemini-memory:global`; a project-root file uses the parent directory name.
- **Tags**: every chunk gets `gemini-memory`.
- **Source**: a single `GEMINI.md` file (or a directory containing one).

### Ingesting Codex CLI memory

`mm ingest codex-memory` indexes a Codex CLI memories directory. The
default location is `~/.codex/memories/`.

```
mm ingest codex-memory --source ~/.codex/memories/ --dry-run
mm ingest codex-memory --source ~/.codex/memories/
```

- **Namespace**: `codex-memory:<slug>`. `~/.codex/memories/` becomes
  `codex-memory:global`; a custom directory uses its name as the slug.
- **Tags**: every chunk gets `codex-memory`.
- **Excluded**: `README.md` is skipped. Hidden files and non-markdown files
  are ignored. Discovery is flat (non-recursive).
- **Delta on re-run**: same content-hash dedup as the Claude ingest.

### MCP `mem_ingest` tool

All three ingest commands are also available as an MCP action:

```
mem_do(action="ingest", params={
    "source": "~/.claude/projects/",
    "source_type": "claude",   # or "gemini", "codex"
    "dry_run": true
})
```

This is useful for agent-driven ingestion without the CLI. Multi-slug
discovery works the same way for `source_type="claude"`.

---

## 7. Config — `mem_stats`, `mem_status`, `mem_config`, `mem_embedding_reset`

> **Tool mode note:** `mem_config`, `mem_embedding_reset`, and `mem_reset` require `MEMTOMEM_TOOL_MODE=full`. In `core` or `standard` mode, use `mm config` / `mm embedding-reset` (CLI) or the Web UI Settings tab.

### `mem_stats` / `mem_status`

```
mem_stats()
→ Total chunks: 444, Sources: 104, Storage: sqlite

mem_status()
→ Storage: sqlite, DB path: ~/.memtomem/memtomem.db
  Embedding: ollama/bge-m3 (1024d), Top-K: 10, RRF k: 60
  Total chunks: 444, Source files: 104
```

When configuration drift is detected (most commonly an embedding
dimension mismatch between the DB and the runtime config), `mem_status`
appends a `Warnings` block whose entries follow a stable schema so
uptime probes and dashboards can pattern-match on the keys:

| Key | Description |
|-----|-------------|
| `kind` | Open enum; current values include `embedding_dim_mismatch` (consumers must tolerate unknown kinds). |
| `fix` | Canonical CLI command to resolve the warning (e.g. `mm embedding-reset --mode apply-current`). |
| `doc` | Relative path into `docs/guides/` with the full remediation flow (see [`configuration.md#reset-flow`](../configuration.md#reset-flow)). |
| `stored` / `configured` | Present for embedding-mismatch entries; echoes the DB vs runtime provider/model/dimension. |

### `mem_config` — View and modify settings

```
mem_config()                                         # show all settings
mem_config(key="search.default_top_k")               # read one value
mem_config(key="search.default_top_k", value="20")   # change (persists to ~/.memtomem/config.json)
```

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `search.default_top_k` | `10` | Search result count |
| `search.enable_bm25` | `true` | Keyword search (exact word match) |
| `search.enable_dense` | `true` | Meaning search (similar concepts) |
| `search.rrf_k` | `60` | Result merging balance (higher = smoother) |
| `indexing.max_chunk_tokens` | `512` | Max tokens per chunk |
| `indexing.min_chunk_tokens` | `128` | Short chunk merge threshold |
| `decay.enabled` | `false` | Time-based score decay |
| `mmr.enabled` | `false` | Result diversification |
| `namespace.enable_auto_ns` | `false` | Auto-derive namespace from folders |

### `mem_embedding_reset` — Resolve model mismatch

**MCP:**
```
mem_embedding_reset()                       # check status (default)
mem_embedding_reset(mode="apply_current")   # reset DB to current model (requires re-index)
mem_embedding_reset(mode="revert_to_stored") # switch runtime to match DB
```

**CLI:**
```bash
mm embedding-reset                          # check status (default)
mm embedding-reset --mode apply-current     # reset DB to current model (requires re-index)
mm embedding-reset --mode revert-to-stored  # switch runtime to match DB
```

---

## CLI Reference

`mm` is a shorthand alias for `memtomem`. All commands support `-h` and `--help`.

```bash
# Setup
mm init                                # preset picker; `--advanced` opens the full 10-step wizard (b: back, q: quit)

# Core (daily use)
mm search "deployment"                 # hybrid search (keywords + meaning)
mm search --as-of 2024-Q3 "deploy"     # temporal-validity query (date-only or YYYY-QN)
mm index ~/notes                       # manual one-shot index (seed pre-existing files)
mm index --debounce-window 5 PATH      # record PATH; drain entries silent ≥5s (hook callers)
mm index --flush                       # synchronously drain queue (correctness primitive)
mm index --status                      # snapshot queue depth + oldest entry
mm add "note" --tags "tag1"            # add a memory
mm recall --since 2026-03-01           # recall by date (Validity column shown when chunks have valid_from/valid_to)

# Configuration
mm config show                         # view all settings
mm config set search.default_top_k 20  # change a setting
mm config unset mmr.enabled            # drop a pinned override
mm embedding-reset                     # check/resolve embedding model mismatch
mm reset                               # delete all data and reinitialize the DB (refuses while the server or web UI is running)
mm reset --yes                         # skip confirmation prompt (safety gates still apply)
mm reset --backup                      # snapshot the DB to <db>.pre-reset-<ts>.bak before wiping
mm reset --force                       # bypass the liveness/write-lock gates (stale-pid recovery)
mm upgrade                             # stop the running server, clear the stale pid, reinstall with --refresh
mm upgrade --version 0.3.1 --dry-run   # preview a pinned upgrade (also: --grace, --extras, -y/--yes, --json)

# Tags — bulk tag maintenance (mutations are dry-run unless --apply; --yes skips the prompt)
mm tags list                           # list every tag with its chunk count
mm tags rename old new --apply         # rename a tag across all chunks
mm tags merge a b --into c --apply     # fold tags a, b into c
mm tags delete stale --apply           # drop a tag from all chunks

# Multi-agent memory — per-agent scopes (see the MCP server's multi-agent workflow)
mm agent register planner --description "research agent"  # register an agent id (optional --color)
mm agent list                          # list registered agents (--json for scripting)
mm agent share <chunk_id> --target shared    # copy a chunk into the shared scope (default target: shared)
mm agent migrate                       # migrate legacy agent records into the registry (--dry-run to preview)

# Wiki — host-global canonical library (~/.memtomem-wiki); ADR-0008 / ADR-0027
# Author, check, and commit canonical artifacts here, then project them into a
# project with `mm context install`. The wiki is GLOBAL; install/status are
# project-scoped and need a git or pyproject project root.
mm wiki init                           # create ~/.memtomem-wiki (writes a README documenting the layout)
mm wiki list                           # list canonical assets (--type skills|agents|commands)

# Minimal first asset (canonical-only) — empty wiki to installed project copy:
#   create ~/.memtomem-wiki/skills/<name>/SKILL.md   (see the wiki README for the layout)
mm wiki skill lint <name>              # CI gate — exits non-zero on errors
mm wiki skill commit <name> --canonical   # ONE isolated commit of just the canonical path
cd <your project>                      # wiki is global; install/status are project-scoped
mm context install skill <name>        # snapshot the wiki asset (at HEAD; commit-true — refuses if the asset has uncommitted wiki changes)
mm context update skill <name>         # re-snapshot an installed asset after the wiki changed (--all, --force, --yes)
mm context status                      # installed wiki assets + drift (reads the CURRENT project)

# Add-on — seed a vendor override (only when a runtime needs a divergent render):
mm wiki skill override <name> --vendor claude --editor  # writes overrides/claude.md (+ .bak under --force)
mm wiki skill diff <name> --vendor claude               # canonical render vs this vendor override (--vendor required)
mm wiki skill commit <name> --vendor claude             # override-only commit (add --canonical only for a combined commit)

# Back up / sync the wiki across machines (it is a normal git repo — no new sync protocol):
mm wiki remote git@github.com:you/memtomem-wiki.git    # configure the 'origin' backup remote (no arg = show it)
mm wiki push                                           # back up: git push origin <branch>
mm wiki pull                                           # sync down: git pull origin <branch>
mm wiki init --from <url>                              # restore onto a fresh machine (one-time clone)

# Notes:
#   - commit only records files that already exist on disk (create canonical / seed override first)
#   - `mm wiki <type> commit` makes ONE isolated commit of the selected paths; it never
#     sweeps unrelated staged changes (no `git add . && git commit`)
#   - agents/commands mirror skill: `mm wiki agent ...`, `mm wiki command ...`
#   - push/pull are thin git wrappers: they surface git's own errors and own no conflict
#     resolution — resolve merge conflicts / divergent histories with ordinary git, and avoid
#     embedding credentials in the remote URL (prefer SSH keys or a git credential helper)

# Agent context sync
mm context detect                      # find agent config files
mm context init                        # create unified context.md (project_shared default)
mm context init --scope user           # seed user-tier canonical (~/.memtomem/{agents,skills,commands}/)
mm context init --scope project_local  # seed gitignored draft tier + auto-append .gitignore
mm context init --scope project_shared --confirm-project-shared       # Gate B: explicit opt-in
mm context init --include=agents --scope user --force-unsafe-import   # bypass Gate A on existing leaks
mm context init --include=skills --only my-skill   # import ONE named runtime artifact (skips context.md + dir seeding)
mm context generate --agent all        # generate all agent files
mm context generate --include=agents --label production # generate files using the 'production' labeled snapshot (agents/commands only)
mm context diff                        # check sync status
mm context status                      # installed wiki assets + their drift state (read-only)
mm context status --all-projects       # aggregated drift across enrolled on-disk projects (read-only; same data as GET /api/context/status-all)
mm context sync                        # sync context.md → agent files (project_shared default)
mm context sync --scope user           # fan out from ~/.memtomem/... → ~/.{claude,gemini,codex,kimi}/... (Codex: skills → ~/.agents/skills, commands → ~/.codex/prompts)
mm context sync --include=skills --scope user --force-unsafe   # bypass Gate A on a reviewed false positive (user/project_local only; project_shared hard-refuses)
mm context sync --include=skills --scope project_local   # NO_FANOUT skip (no runtime per ADR §3)
mm context sync --include=agents,commands --label production # sync using the 'production' labeled version (agents/commands only)
mm context sync --all-projects --yes   # batch over every enrolled on-disk project (project_shared only, ADR-0025)
mm context generate --include=settings # merge hooks → ~/.claude/settings.json
mm context diff --include=settings     # check hook sync status

# Context Versioning (ADR-0022)
mm context version create agents my-agent --note "stable"          # snapshot the current working canonical as a new immutable version
mm context version promote agents my-agent --to production --version v1 # move a label pointer (e.g. production) to a specific version (e.g. v1)
mm context version delete-label agents my-agent production         # drop a label pointer (absent label = no-op; 'latest' is reserved)
mm context version list agents my-agent                            # list all versions and label pointers for an artifact
mm context version enable agents my-agent                          # adopt a flat-layout artifact into directory layout so it can be versioned

# Multi-project registry (same registry the web portal manages)
mm context projects list               # discovered scopes: scope_id, health, enrollment
mm context projects list --json        # same fields as GET /api/context/projects
mm context projects add ~/work/proj --label "My Project"  # register (idempotent)
mm context projects pause <scope_id|path>   # exclude from --all batches / web Sync
mm context projects resume <scope_id|path>  # re-include
mm context projects remove <scope_id|path>  # unregister (project files untouched)

# Cross-project / cross-tier transfer (ADR-0023; see "Moving artifacts" below)
mm context copy agents foo --to project_local                  # tier copy inside this project (dry-run preview by default)
mm context move agents foo --to project_shared --apply --confirm-project-shared  # tier move; git-tracked landing needs the extra flag
mm context copy agents foo --to-project <scope_id> --apply     # copy to another registered project, keeping the source tier
mm context copy agents foo --to-project ~/work/other --as foo2 --apply  # path destination (CLI-only consent valve) + renamed copy
mm context copy mcp-servers pg --to-project <scope_id> --apply --confirm-project-shared  # copy one MCP server definition
mm context move agents foo --from user --to project_local --apply       # --from disambiguates a multi-tier source

# Note: cursor / codex / copilot fold ## Rules + ## Style into a single block;
# `generate` warns on stderr when both sections are populated. context.md is
# the source of truth — edit there, not in generated files.
#
# `--scope` semantics (ADR-0011 PR-E2 init / PR-E3 sync):
#   user            → seeds ~/.memtomem/{agents,skills,commands}/; init imports from
#                     ~/.claude/agents, ~/.gemini/agents, ~/.claude/skills, etc.;
#                     sync fans canonical out to those same runtime roots.
#   project_shared  → seeds <proj>/.memtomem/{agents,skills,commands}/; imports
#                     from <proj>/.claude/agents etc.; git-tracked. Requires
#                     --confirm-project-shared when --scope is explicit on init.
#   project_local   → seeds <proj>/.memtomem/{agents,skills,commands}.local/;
#                     auto-appends .memtomem/*.local/ + .memtomem/.staging/ to
#                     <proj>/.gitignore (idempotent). No runtime fan-out
#                     by design (ADR §3) — nothing to import or sync.
#
# Gate A on `init --include=...` import path: every source file is re-scanned
# for secrets via enforce_write_guard. user / project_local destinations
# can bypass with --force-unsafe-import (audit-logged); project_shared
# destinations hard-refuse on any hit (no force bypass available).
#
# Gate A on `sync` write path (PR-E3): same per-file scan. user /
# project_local destinations skip-and-warn on hits with PRIVACY_BLOCKED, or
# bypass with `--force-unsafe` (audit-logged) for a reviewed false positive
# — the fan-out mirror of init's `--force-unsafe-import` (#1386). project_shared
# destinations raise ClickException regardless of the flag (ADR §5: git history
# is forever), with a remediation hint pointing at `mm context migrate` (PR-E4)
# for moving the artifact to a writable tier first. Skills fan-out uses
# staging-dir-first scan + atomic os.replace promote so a blocked sync leaves
# the existing dst tree unchanged.
#
# Web Context Gateway missing-canonical remediation:
#   project_shared → web Import can initialize from detected runtime files;
#                    CLI bootstrap requires explicit git-tracked-tier confirmation.
mm context init --include=agents,commands,skills --scope project_shared --confirm-project-shared
mm context sync --include=agents,commands,skills --scope project_shared
#
#   user           → web UI is read-only for user-tier canonical operations.
mm context init --include=agents,commands,skills --scope user
mm context sync --include=agents,commands,skills --scope user
#
#   project_local  → gitignored draft tier; sync reports no runtime fan-out.
mm context init --include=agents,commands,skills --scope project_local
mm context sync --include=agents,commands,skills --scope project_local

# Sessions & activity
mm session start                                              # start a tracked session
mm session start --idempotent --agent-id claude-code          # resume active session for that agent (SessionStart hooks)
mm session start --idempotent --auto-end-stale 24h            # additionally close any active session older than 24h first
mm session end                                                # end session with auto-summary
mm session list                                               # list sessions
mm session events <id>                                        # show events for a session
mm session wrap -- CMD                                        # wrap a command with session lifecycle
mm activity log                                               # log agent activity event
# Scripting: list/events/log support --json (see CONTRIBUTING.md → CLI output convention)

# Health
mm watchdog status                     # show latest health check results
mm watchdog run                        # run all health checks immediately
mm watchdog history <check>            # show historical results for a check

# Ingest (cross-tool memory import)
mm ingest claude-memory --source PATH  # index Claude Code auto-memory
mm ingest gemini-memory --source PATH  # index Gemini CLI memory
mm ingest codex-memory --source PATH   # index Codex CLI memory

# Utilities
mm shell                               # interactive REPL
mm web                                 # launch Web UI (prod surface)
mm web --dev                           # Web UI with opt-in maintainer pages
```

Install the CLI: `uv tool install 'memtomem[all]'` (PyPI) or `uv run mm ...` (source).
All commands support `-h` and `--help`.

---

## Moving artifacts between tiers and projects

> New to the Store → Sync → Runtime model? Start with the
> [Context Gateway](../context-gateway.md) walkthrough; this section covers the
> transfer verbs.

Three verbs share the transfer engine (ADR-0023). Pick by what should
happen to the source:

| | `move` | `copy` | `migrate` |
|---|---|---|---|
| Source afterwards | consumed; its stale runtime fan-out is cleaned (divergent files get a `.bak` snapshot first) | untouched | consumed (`--to` is the within-project move alias) |
| Cross-project (`--to-project`) | yes | yes | no — within-project only |
| Renamed copy (`--as`) | no | yes | no |
| flat→dir layout adoption | no | no | yes (its original job, `--to` omitted) |
| MCP server definitions (`mcp-servers`) | no | yes (cross-project only) | no |

Shared rules, all verbs: destination collisions always refuse (no
`--force` valve); destination runtime fan-out is **not** generated —
the result prints the exact follow-up `mm context sync` command (for
`mcp-servers`, `cd <dst> && mm context sync --include=mcp-servers
--scope project_shared`, with web Sync as an equivalent option); a
`project_shared` landing runs the privacy scan (Gate A,
no bypass) and requires `--confirm-project-shared` with `--apply`;
default is always a dry-run preview.

### Cross-project walkthrough

```bash
# One-time: register the destination so it has a scope_id (or pass a
# filesystem path to --to-project — typing a path is the consent valve
# for unregistered destinations, CLI-only).
mm context projects add ~/work/other-proj --label "Other"
mm context projects list                  # → p-1a2b3c4d5e6f  Other  ok

# Preview, then execute. --to omitted keeps the source tier.
mm context copy agents reviewer --to-project p-1a2b3c4d5e6f
mm context copy agents reviewer --to-project p-1a2b3c4d5e6f --apply --confirm-project-shared

# The result names the follow-up — destination fan-out is sync's job:
cd ~/work/other-proj && mm context sync --scope project_shared
```

A paused destination refuses (`mm context projects resume <scope_id>`
re-enables it), and a cross-project destination must already be a
memtomem project (`mm context init` there first).

### Headless agents (MCP)

`mem_context_artifact_transfer` is the same engine via `mem_do` —
core mode needs no extra tools:

```python
mem_do(action="context_artifact_transfer", params={
    "asset_type": "agents", "name": "reviewer", "mode": "copy",
    "to_project_scope_id": "p-1a2b3c4d5e6f",   # from `mm context projects list`
})  # dry-run preview; the footer names the flags apply will need

mem_do(action="context_artifact_transfer", params={
    "asset_type": "agents", "name": "reviewer", "mode": "copy",
    "to_project_scope_id": "p-1a2b3c4d5e6f",
    "apply": True, "confirm_project_shared": True,
})
```

The MCP surface is deliberately stricter than the CLI (ADR-0023 §13):
destinations are registered `scope_id`s only (no path valve), paused
**and** never-enrolled destinations refuse with the remediation
command, and a `user`-tier landing — a host write outside any project
root — needs `allow_host_writes=True` in addition to `apply=True`.
Refusals come back prefixed (`error:` / `refused:` /
`needs confirmation:` / `privacy block:`) so agents can branch.

---
