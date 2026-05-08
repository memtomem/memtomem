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
