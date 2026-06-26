# Context Gateway: one Store, synced to all your AI tools

> **Audience:** developers who use more than one AI coding tool (Claude Code,
> Codex, Kimi, Antigravity, …) and want one project's skills, commands, and
> subagents to live in a single place and show up in every tool — instead of
> hand-copying files into each tool's config directory.

memtomem keeps your master copies in one **Store** (the `.memtomem/` tree) and
**Syncs** them out to your **Runtimes** — the AI tools detected on your machine.
**Import** is the reverse: it pulls a copy that already exists in a runtime back
into the Store. The flow is one-way by default: edit in the Store, then Sync; a
runtime's copy is overwritten on the next Sync.

This guide walks through that model and the first task most people want — getting
a project's skills into their AI tools — from both the Web UI and the CLI. It
links out to [`reference.md`](reference.md#cli-reference) for the full command matrix and to
[`configuration.md#context-gateway`](configuration.md#context-gateway) for the
environment variables; it does not re-document those here.

## Prerequisites

The CLI path (`mm context …`) works on any install. The Web UI path needs the
`web` extra:

```bash
uv tool install --reinstall 'memtomem[web]'   # or 'memtomem[all]'
```

A BM25-only minimal install has the CLI but no `mm web`. Either path operates on
the same Store, so you can mix them.

## The model — Store, Runtimes, Sync, Import

Four words carry the whole feature:

- **Store** — your master copies, under `.memtomem/` (e.g.
  `.memtomem/skills/`, `.memtomem/commands/`, `.memtomem/agents/`). This is the
  one place you edit.
- **Runtimes** — the AI tools detected on the machine (Claude Code, Codex, Kimi,
  Antigravity, and others), each with its own config directory
  (`.claude/`, `.codex/`, `.gemini/`, `.kimi/`, …).
- **Sync** — sends your stored copies *out* to the runtimes configured for the
  Store. This is the common direction.
- **Import** — brings a runtime's existing copy *back in* to the Store. Use it
  once, when an artifact only lives in a tool and you want memtomem to own it.

Because Sync is one-way, the Store is the source of truth: edit there, Sync, and
every runtime gets the same copy. A change you make directly in a runtime's
config directory is overwritten the next time you Sync that artifact — Import it
first if you want to keep it.

## Where copies live — the "Stored in" tiers

Every stored artifact sits at one of three tiers (the **Stored in** axis in the
Web UI). The display label is on the left; the literal token you pass to
`--scope` on the CLI is in parentheses:

| Tier | Where it lives | Synced to runtimes? | Shared with your team? |
|---|---|---|---|
| **User** (`user`) | `~/.memtomem/<artifact>/` | Yes | No — every project on this machine, just you |
| **Project (shared)** (`project_shared`) | `<project>/.memtomem/<artifact>/` | Yes | Yes — committed to the project's git repo |
| **Project (local)** (`project_local`) | `<project>/.memtomem/<artifact>.local/` | **Never** | No — gitignored draft |

A few load-bearing details:

- **Project (shared)** means *git-tracked* (you commit it so teammates get it on
  `git pull`). It does not mean "shared between tools" — every tier Syncs to the
  tools; only the git-sharing differs.
- **Project (local)** is a gitignored scratch tier. It is never Synced out to a
  runtime, by design — it is your private draft.
- Writing a secret into **Project (shared)** is blocked outright (see
  [Privacy and git safety](#privacy-and-git-safety) below) because git history is
  permanent.

## The wiki — a separate global library, not the Store

The names look alike, so state it plainly: **`~/.memtomem-wiki` is not
`~/.memtomem`.** They are different things.

- **`~/.memtomem/`** is your memtomem home — `config.json`, the database, and the
  **User**-tier Store at `~/.memtomem/<artifact>/`. It is part of the Store model
  above.
- **`~/.memtomem-wiki/`** is the **wiki**: an *optional*, host-global **git
  library** of *canonical* skills, subagents, and commands you author once and
  reuse across every project. It is a normal git repo you can back up and clone
  (`mm wiki push` / `pull`). It is **not** a Store tier and is never synced to a
  runtime directly.

The wiki sits one step *upstream* of the Store — you pull from it explicitly:

```
wiki (~/.memtomem-wiki)  ──install──▶  project Store (<project>/.memtomem/)  ──sync──▶  runtime (.claude/ …)
```

- **Install** — `mm context install <type> <name>` (or the **Install** button in
  the Web UI's Wiki section) snapshots one canonical asset from the wiki into the
  **current project's** `.memtomem/` Store, pinned to the wiki's HEAD commit.
- **Update** — `mm context update <type> <name>` refreshes an installed snapshot
  to the wiki's latest HEAD.

Install and Update only ever write the **project** Store. They do **not** edit
`~/.memtomem/config.json` or the `~/.memtomem/<artifact>/` User tier — so a wiki
install never changes your machine-wide settings or User-tier copies. Once
installed, the snapshot is an ordinary Store artifact: you Sync it out to your
runtimes like any other.

In the Web UI the wiki panel is **read-only** (browse + Install); authoring
canonical or vendor-override files is done with the `mm wiki …` CLI (or the
in-browser editor in dev mode). See [`reference.md`](reference.md#cli-reference)
for the full `mm wiki` / `mm context install` command matrix.

## Walkthrough — get this project's skills into your AI tools

Say this project has a skill in its Store that isn't in your tools yet. Two
equivalent paths.

### From the Web UI

```bash
mm web      # http://127.0.0.1:8080
```

Open **Settings → Context Gateway**. The default **Simple view** shows one row
per artifact type — find the **Skills** row:

- If the skill isn't in your tools yet the row reads **Needs sync** and offers a
  **Sync** button. Click it.
- A confirmation appears first — it tells you exactly what will change, e.g.
  *"This will create N missing and overwrite M out-of-sync runtime files in:
  …"*. Read it, then confirm.

That's it — the skill is now in the runtimes configured for this Store.

### From the CLI

```bash
mm context detect --include=skills      # see which runtime skill dirs are detected here
mm context init --include=skills        # seed the Store + context.md (project_shared by default)
mm context diff --include=skills        # preview what's in/out of sync
mm context sync --include=skills        # send the stored skills out to the runtimes
```

`mm context diff` is the read-only CLI view of the status badges; `mm context
sync` applies the changes. Syncing skills, agents, or commands writes directly —
unlike the Web UI, which shows a confirmation for every Sync, the CLI does not
prompt. (The one exception is a `settings` sync that writes files outside the
project, which prompts unless you pass `--yes`.)

To put a skill in your **User** tier instead (available to every project on this
machine):

```bash
mm context sync --include=skills --scope user
# fans out from ~/.memtomem/skills/ → ~/.claude/skills/, ~/.gemini/skills/, ~/.agents/skills/ (Codex), ~/.kimi/skills/
```

## Sync vs Import — reading the status

The **Advanced** view labels each artifact with one of these precise statuses
(the default Simple view collapses them into a single per-row verdict + fix
button). Map the status to the action that resolves it:

| Status | What it means | Action |
|---|---|---|
| **In sync** | The Store and the runtime copies match. | Nothing to do. |
| **Out of sync** | A runtime copy drifted from the Store. | **Sync** (the Store copy wins). |
| **Not in runtime** | The Store has it; a runtime doesn't. | **Sync** (creates it there). |
| **Not yet imported** | A runtime has it; the Store doesn't. | **Import** (brings it in). |
| **Parse error** | A file (Store or runtime) is malformed. | Open it, fix the file, then refresh. |

The rule of thumb: if the Store should win, **Sync**; if a tool has something the
Store is missing, **Import** it once and then the Store owns it.

When the same artifact exists in more than one runtime, Import takes the first
copy it finds, scanning runtimes in a fixed order (Claude first, then
Antigravity; skills also import from Codex and Kimi). Other runtimes are
export-only and are never read back.

## From the Web UI — what the tab shows

The Context Gateway tab has two views, toggled by the **Simple view** switch:

- **Simple view** (the default) — a one-line overview per type with a single fix
  button on each row, and a **Manage** link that drops you into Advanced for
  anything without a one-click fix.
- **Advanced** — per-type sections (Skills, Subagents, Custom Commands, MCP
  servers, settings/hooks), each with a status badge and per-row Sync/Import
  buttons, plus a control bar to filter by artifact type, the **Stored in** tier,
  project, and sync state. **Sync All** pushes everything in the current Store at
  once (with the same create/overwrite confirmation). The **Simple view** toggle
  switches back; your choice persists.

The **Projects** portal lists the project roots memtomem knows about: the folder
the server is running in (marked *current folder*), any roots you registered, and
— if you opt in — roots discovered under `~/.claude/projects/`. Each runtime
shows a detection badge (*Not installed* / *Installed* / *Registered*).

> Note: the `gemini` runtime is shown as **Antigravity** in the UI.

## From the CLI — the core loop

The five commands you'll use most:

```bash
mm context detect      # list detected runtime config files
mm context init        # seed the Store and write .memtomem/context.md
mm context generate    # write runtime files from the Store (per --agent / --include)
mm context diff        # show in-sync / out-of-sync state
mm context sync        # send the Store out to every detected runtime
```

Useful flags (see [`reference.md`](reference.md#cli-reference) for the full
list):

- `--include=<kind>` — narrow to `skills`, `agents`, `commands`, or `settings`.
  `mm context sync` *also* accepts `--include=mcp-servers` (sync-only, opt-in).
- `--scope user|project_shared|project_local` — pick the tier (see the table
  above). `project_local` never reaches a runtime.
- `--all-projects` — batch a Project (shared) sync over every eligible project
  (enrolled or discovered on disk; paused/ineligible ones are skipped).
- `--yes` — skip the confirmation prompt shown before writing `settings` files
  outside the project (it has no effect when syncing skills, agents, or commands).

## Other artifact types and projects

Skills are the walkthrough example, but the same Store → Sync → Runtime flow
covers **subagents** (`agents`), **custom commands** (`commands`), and
**settings/hooks** (`settings`). MCP server definitions also Sync (into a
project's `.mcp.json`), but as a sync-only, opt-in kind (`--include=mcp-servers`).

To manage more than one project, register its root so it shows up in the
Projects portal and `--all-projects` batches:

```bash
mm context projects add ~/work/my-project --label "My Project"
mm context projects list
```

To **move** or **copy** an artifact between tiers or projects, use the transfer
verbs documented in
[`reference.md#moving-artifacts-between-tiers-and-projects`](reference.md#moving-artifacts-between-tiers-and-projects)
— this guide doesn't repeat that matrix.

## Privacy and git safety

memtomem scans artifacts for likely secrets (API keys and similar) before writing
them out:

- **Project (shared)** writes hard-refuse on a detected secret and **cannot** be
  overridden — `--force-unsafe` is rejected for `project_shared` because git
  history is permanent. Remove the flagged line, then re-run.
- **User** and **Project (local)** writes can be overridden with
  `--force-unsafe` (CLI) or *Sync anyway* (Web) after you've reviewed a false
  positive — e.g. a type annotation like `api_key: str`.

See [`configuration.md#context-gateway`](configuration.md#context-gateway) for
the related environment variables.

## See also

- [Reference](reference.md#cli-reference) — every `mm context` command and flag,
  plus the [move/copy/migrate matrix](reference.md#moving-artifacts-between-tiers-and-projects).
- [Configuration → Context Gateway](configuration.md#context-gateway) — the
  `MEMTOMEM_CONTEXT_GATEWAY__*` environment variables and the project-scan caveats.
- [Multi-device sync](multi-device-sync.md) — committing your Project (shared)
  Store files so they follow you (and your team) through git.
