# Multi-device sync via private git repo

> **Audience:** single users running memtomem on multiple personal devices who
> want their markdown memories to follow them, without standing up a hosted
> server and without trusting a cloud-mount provider with the SQLite database.

memtomem stores its source-of-truth as plain markdown under
`indexing.memory_dirs`. The SQLite database is derived state — `mem_index`
rebuilds it from those files. That gives you a transport-agnostic sync path:
keep the markdown in a private git repo, leave the database local, and let
`git pull` carry edits between machines.

This guide describes the layout, what to commit (and what never to commit),
the workflow around restarting the runtime after a pull, and `mm sync-doctor`
— the read-only validator that catches the common footguns.

## When this fits

- **Single user, multiple devices.** Laptop ↔ desktop ↔ work machine, one
  identity, one git account.
- **You already use git** for code and want memories to use the same
  habits (branches, rebase, conflict resolution in your editor).
- **You don't want a hosted memtomem server** and you don't want a
  cloud-mount client managing your `*.db` file.

If you need *team* sharing instead, this is the wrong page. Sharing a private
repo across users leaks namespace identity, agent IDs, and historical
absolute paths embedded in the markdown. For team sync, use a cloud-mounted
folder with the same `mem_index` workflow — the SQLite caveat below still
applies.

For *single-machine backup*, use `mem_export` / `mem_import`
([Reference](reference.md#6-data--mem_export-mem_import)). The bundle format
embeds absolute paths and is not designed for cross-device restore.

## The layout — namespace-aligned directory tree

Recommended on-disk layout under your local
`indexing.memory_dirs[0]`:

```
~/.memtomem/memories/
  shared/        → namespace "shared:*"     ← synced (all devices)
  personal/      → namespace "default"      ← synced (personal repo)
  work/          → namespace "work:*"       ← synced (work-only repo)
  local/         → namespace "local:*"      ← never synced (.gitignore)
```

Each top-level dir is one namespace family. You can map them to one private
repo with `.gitignore` gating `local/`, or to separate repos per family if
work and personal need different access.

Namespace assignment is purely path-driven via the existing rules system
(see [Namespace rules](configuration.md#namespace-rules-path-based-auto-tagging)).
No explicit `namespace=` argument is required at ingest time.

`~/.memtomem/config.d/10-namespace-rules.json`:

```json
{
  "namespace": {
    "rules": [
      { "path_glob": "~/.memtomem/memories/shared/**",   "namespace": "shared:{parent}" },
      { "path_glob": "~/.memtomem/memories/personal/**", "namespace": "default" },
      { "path_glob": "~/.memtomem/memories/work/**",     "namespace": "work:{parent}" },
      { "path_glob": "~/.memtomem/memories/local/**",    "namespace": "local:{parent}" }
    ]
  }
}
```

The fragment file itself is portable — `~/` expands at load time per the
namespace-rules semantics. **However**, memtomem's loader only reads from
`~/.memtomem/config.d/*.json`. Storing the fragment under your synced repo
at e.g. `~/.memtomem-private/config.d/10-namespace-rules.json` does *not*
make it active on the destination — the rules never apply unless the file
appears at the canonical location. Two ways to bridge:

- **Symlink (recommended).** Edits flow back to the synced repo
  automatically:

  ```bash
  mkdir -p ~/.memtomem/config.d
  ln -sf ~/.memtomem-private/config.d/10-namespace-rules.json \
         ~/.memtomem/config.d/10-namespace-rules.json
  ```

- **Copy.** Plain `cp` after each `git pull`. Simpler but drifts if you
  edit the canonical copy without copying back.

`mm sync-doctor` (below) flags a missing bridge.

## What syncs, what does not

**Sync:**

- `~/.memtomem/memories/{shared,personal,work}/**/*.md` — content. Pure
  markdown, no embedded machine state.
- `~/.memtomem/config.d/*.json` — *selected* fragments only. Pure-policy
  fragments (namespace rules, rerank settings, default `top_k`) are
  portable. Avoid fragments that embed local paths.

**Never sync** — your private repo's `.gitignore`:

```
*.db
*.db-wal
*.db-shm
.server.pid
.current_session
config.json
config.json.bak*
cache/
uploads/
proxy_cache.db*
proxy_metrics.db*
stm_feedback.db*
__pycache__/
.DS_Store
```

Why per category:

- **`*.db` family** (and `cache/`, `proxy_*.db*`, `stm_feedback.db*`) —
  derived state. Rebuilds from markdown via `mem_index`. Embedding model
  versions can diverge across machines; tracking the DB makes that worse,
  not better. SQLite WAL/SHM are inherently process-local; copying them
  mid-write corrupts.
- **`.server.pid`, `.current_session`** — process / session state.
  Nonsensical on a different machine.
- **`uploads/`, `config.json.bak*`** — transient. No long-term value
  across devices.
- **`config.json`** — `memory_dirs` and `sqlite_path` may legitimately
  differ across machines (different storage volumes, different home
  layouts), so the file itself stays machine-local. The `config.d/` layer
  carries portable policy. memtomem already serializes home-rooted paths
  in tilde form on write, so you *could* copy `config.json` between
  matching layouts — but the recommendation is still to keep it local
  and rely on `config.d/` for shared policy. See
  [Moving config.json between machines](configuration.md#moving-configjson-between-machines)
  for the cross-machine semantics.

## Post-pull workflow

memtomem has **no top-level `mm stop` / `mm start` command** — runtime
lifecycle is owned by whatever launched the process:

- **MCP server** (typical case): launched by an MCP client (Claude Code,
  Cursor, etc.) at session start; lifecycle is managed by the client. The
  built-in stop path is `mm upgrade` (which stops the server, reinstalls,
  and restarts via the client's next launch). For sync purposes, exit
  the MCP client session before pulling — or trust `startup_backfill` on
  the next launch.
- **Web UI** (`mm web` foreground): stop with `Ctrl+C`; restart with
  `mm web` after the pull.
- **CLI-only** (no long-running runtime): no concern.

Recommended sequence:

```bash
# 1. Quiesce the runtime (Ctrl+C the foreground mm web; or end the MCP
#    client session; or mm upgrade if you want a forced bounce).
# 2. Pull.
cd ~/.memtomem-private && git pull --rebase
# 3. Restart the runtime (mm web; or relaunch the MCP client).
```

When stopping is impractical (e.g. an active long-running MCP session),
enable `indexing.startup_backfill = true` instead. With backfill enabled,
files added while the server was down (or files the watcher missed) get
re-indexed on next start, so a missed event during the pull window is
recovered automatically. The `mm init` wizard offers this as an opt-in
prompt; flip it on the second machine if pulls happen mid-session there.

The cloud-sync caveat in
[`memory_dirs` reactive watch vs one-shot seed](configuration.md#memory_dirs--reactive-watch-vs-one-shot-seed)
applies symmetrically to git pulls: the watcher's reliability for
`git pull`-delivered files is best-effort, and the same mitigation (manual
`mem_index <dir>` or `startup_backfill`) covers it.

A `post-merge` git hook (user-installed, not shipped by memtomem) is a
reasonable optimization for users who keep the server running. Call
`mm index <affected-dir>` from the hook and redirect stdout to your
hook log (or `/dev/null`). The command exits non-zero on hard failure;
the trailing `Indexed N file(s): …` summary line is the parse target
if you want a one-line record per pull. (`mm index --json` is reserved
for the `--debounce-window` / `--flush` / `--status` paths and does not
apply to plain indexing.)

## Conflict policy

`git config pull.rebase true` in the private repo. Rebase, not merge —
keeps a linear history and surfaces real edit clashes as standard markdown
conflicts you resolve in your editor of choice (`git mergetool` works
fine; memtomem itself has no role here).

For daily-log style content where multi-device same-day edits are likely,
use per-day filenames so most concurrent writes never overlap. Convention,
not enforcement.

## Auto-memory (Claude Code)

Claude Code stores per-project memory at `~/.claude/projects/<slug>/memory/`,
where `<slug>` is the absolute working-directory path with `/` → `-`
(e.g. `-Users-alice-Work-project`). The slug is computed at session-open
time from `cwd`, not configurable.

Two workable strategies, both out of memtomem's control:

- **(a) Stable cwd path on every machine.** If you keep the project at the
  same absolute path everywhere (or your username matches across
  machines, in which case the path matches naturally), the slug is
  identical, and the inner `memory/` dir can be symlinked or copied
  directly into the synced repo.
- **(b) Per-machine symlink to a stable repo location.** On each machine,
  symlink `~/.claude/projects/<machine-specific-slug>/memory` →
  `<private-repo>/claude/projects/<repo-name>/memory`. Claude Code
  reads/writes through the symlink; git tracks the real path under the
  synced repo.

Both work. (a) is simpler if path discipline is feasible; (b) is
username/path-agnostic at the cost of one symlink per project per
machine.

## Obsidian as editor on top of git transport

Obsidian fits cleanly on top of this layout because the source of truth is
already plain markdown — Obsidian opens any folder as a *vault* and edits the
files in place. There is **no separate sync layer** to configure on the
Obsidian side: the private git repo described above is still the transport,
and Obsidian is purely the editor. This guide does not cover Obsidian Sync
(the paid SaaS), iCloud, Dropbox, or other transports as alternatives —
their reliability characteristics (especially fs-watcher behavior) differ
from git's and would invalidate the layout / anti-patterns above.

### Vault layout

Two arrangements work, depending on whether you want the vault root to *be*
the synced repo or to *contain* it:

- **(a) Vault root = synced repo root.** Open `~/.memtomem-private/` itself
  as the vault. The namespace tree (`shared/`, `personal/`, `work/`,
  `local/`) becomes the vault's top-level folders, and the existing
  `path_glob` rules from [The layout](#the-layout--namespace-aligned-directory-tree)
  apply unchanged.
- **(b) Vault contains a `memories/` sub-folder.** If your vault root is
  somewhere else (e.g. `~/Obsidian/`) and the synced memtomem files live at
  `~/Obsidian/memories/{shared,personal,work}/`, adjust the `path_glob`
  fragments accordingly — e.g. `~/Obsidian/memories/shared/**` →
  `shared:{parent}`. The semantics are identical; only the absolute path
  differs.

### Required: exclude `.obsidian/` from indexing

Obsidian stores vault metadata in a top-level `.obsidian/` directory —
workspace layout, plugin state, hotkeys, etc. — much of which is JSON.
memtomem indexes `.json` by default, so without an explicit rule this
metadata ends up in your search results. Add the following fragment to
`~/.memtomem/config.d/` so the rule is portable across machines:

```json
// ~/.memtomem/config.d/30-obsidian.json
{
  "indexing": {
    "exclude_patterns": ["**/.obsidian/**"]
  }
}
```

See [`exclude_patterns`](configuration.md#exclude-patterns) for the broader
semantics (root-relative matching, non-retroactive behavior, layering on
top of built-in denylists).

### `.gitignore` — vault-local state

In addition to the entries in [What syncs, what does not](#what-syncs-what-does-not),
Obsidian-specific paths that are commonly *not* useful to sync:

```
.obsidian/workspace*.json
.obsidian/cache/
```

`workspace.json` and `workspace-mobile.json` track per-device pane layouts
and tend to thrash on every Obsidian launch; the cache dir is reproducible
state. The remaining `.obsidian/` files (`community-plugins.json`,
`themes/`, `hotkeys.json`, `app.json`) are often useful to sync if you
want consistent vault behavior across devices, but this is an Obsidian-side
preference rather than a memtomem one — defer to Obsidian's own guidance.

### Plugin-generated markdown

Two formats produced by common plugins to be aware of:

- **`.canvas`** (Obsidian Canvas, JSON-backed). Not in memtomem's default
  indexed extensions, so it's ignored automatically. No action needed.
- **`.excalidraw.md`** (Excalidraw plugin's plugin-generated markdown
  files). These match the `.md` filter and *will* be indexed unless
  excluded. If you treat them as drawings rather than searchable notes,
  add `**/*.excalidraw.md` to `exclude_patterns`.

### Workflow

Day-to-day flow once the layout is in place:

1. Edit notes in Obsidian. The file watcher picks up `.md` changes and
   re-embeds incrementally. Obsidian writes via temp-file + rename, which
   the watcher already handles correctly — no extra configuration.
2. Commit and push from the synced repo on whatever cadence suits you.
3. On another device, `git pull` and follow [Post-pull workflow](#post-pull-workflow)
   to pick the right re-index strategy for your runtime mode (Web,
   long-running MCP, or per-call).

### Different from one-shot import

If you have an existing Obsidian vault you want to ingest *without*
reorganising it into the synced layout — a one-time copy into
`~/.memtomem/memories/_imported/obsidian/` — use the existing
[Importing from Obsidian](reference.md#importing-from-obsidian) action
(`mem_do(action="import_obsidian", …)`) instead. That's a different use
case: one-shot ingest of a non-synced vault, rather than continuous live
sync of the vault as your `memory_dirs[]`.

## `mm sync-doctor` — read-only validator

Run from inside your private repo's working tree. The command performs six
checks and exits non-zero on any failure (warnings don't fail the exit
code):

```text
$ cd ~/.memtomem-private && mm sync-doctor
✓ no *.db files staged
✓ config.json absent from worktree
✓ config.d/ fragments present (3 files)
✗ ~/.claude/projects/ slug differs from synced layout — see doc
✓ memory_dir paths resolve under $HOME
! cloud-sync mount detected at ~/Library/CloudStorage/ — fs watcher may miss events
  recommend startup_backfill=true
```

What each check means:

| Check | Meaning |
|---|---|
| `no *.db files staged` | The git index does not contain `*.db` / `*.db-wal` / `*.db-shm`. If it does, your `.gitignore` is wrong — propagating these between devices corrupts SQLite under WAL. |
| `config.json absent from worktree` | `~/.memtomem/config.json` is not staged. It's machine-local; only `config.d/` is portable. |
| `config.d/ fragments present` | `~/.memtomem/config.d/` exists on this machine and contains at least one `*.json` fragment. If missing, the synced fragment was never bridged into the canonical location. |
| `~/.claude/projects/ slug` | The current working tree corresponds to a `~/.claude/projects/<slug>/` entry that matches your cwd. Skipped silently when `~/.claude/projects/` is absent. |
| `memory_dir paths resolve under $HOME` | All `indexing.memory_dirs` entries sit under your home dir. Outside-`$HOME` paths are not portable across users. |
| `cloud-sync mount detected` | One of your `memory_dirs` is under a known cloud-sync mount (`~/Library/CloudStorage/`, `~/Library/Mobile Documents/com~apple~CloudDocs/`, `~/Dropbox/`, `~/OneDrive*/`). Watcher reliability there is best-effort — flip `startup_backfill` on, or trigger `mem_index` manually. |

`mm sync-doctor` does not push, pull, or auto-fix. It only reports. Wire it
into a `pre-push` git hook if you want the staged-`*.db` check to gate
pushes.

## Anti-patterns

Failure modes that look reasonable on the surface but produce silent
corruption or identity leaks:

1. **Auto-pull on a timer while the server runs.** Atomic-write protects
   in-process writes, not external file replacement. `git pull` swapping a
   file's inode while the server holds a lock orphans the lock; concurrent
   reads see stale data momentarily. Always quiesce the runtime before
   pulling, or rely on `startup_backfill` to mop up missed events on next
   launch.
2. **Sharing the private repo across users.** Single-user multi-device
   only. Shared repos leak namespace identity (`agent-runtime:<id>`),
   historical absolute paths in markdown, agent IDs, and personal session
   traces.
3. **Syncing `*.db` to "speed up" the second machine.** SQLite WAL
   semantics are process-local. Embedding model versions can diverge
   across machines; copying the DB across them produces searchable
   nonsense. The DB rebuilds from markdown via `mem_index` — that's the
   canonical recovery path and it's fast.
4. **Committing `config.json` itself.** `memory_dirs` and `sqlite_path`
   may legitimately differ across machines. Treat `config.json` as
   machine-local; `config.d/` is the synced policy layer.
5. **Mixing the synced markdown root with other unsynced content under
   the same `memory_dirs[0]`.** Either the whole root is the synced
   repo, or split into multiple `memory_dirs[]` entries with the synced
   one first. Otherwise `mem_add(file=relative-path)` may write outside
   the synced repo silently.

## Verification — first-time setup smoke

End-to-end smoke when you set up sync for the first time:

1. **Single-machine round-trip.** Init the private repo, copy
   `~/.memtomem/memories/shared/` in, write
   `config.d/10-namespace-rules.json`, `git add -A && git status` — confirm
   zero `*.db`, `cache/`, `config.json` staged. Run `mm sync-doctor` —
   all checks pass.
2. **Bidirectional.** `mem_add` on machine A → commit/push → pull on
   machine B → run `mm index` (or rely on `startup_backfill`) → search
   the new note. Reverse direction. Both should return the new content.
3. **Conflict path.** Edit the same daily log on both machines, push from
   A, attempt push from B → rebase, resolve, verify search returns the
   merged content.
4. **`config.json` portability.** Copy `~/.memtomem/config.json` from
   machine A to machine B (different `$HOME`). On a memtomem ≥ 0.1.37
   build the file already contains `~/...` paths and resolves correctly
   under HOME-B. (You typically don't want to do this — keep
   `config.json` machine-local — but the round-trip is symmetric if you
   choose to.)
