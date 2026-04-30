# ADR-0008: Wiki layer for shared canonical artifacts

**Status:** Accepted (PR-A in flight; PR-B/C/D/E sequenced — see PR Breakdown)
**Date:** 2026-04-30
**Context:** Context gateway today is a per-project canonical → multi-runtime
fan-out router (ADR-0001). Users with several projects must re-author the
same skill/agent/command in each `<project>/.memtomem/`. This ADR introduces
a global wiki (`~/.memtomem-wiki/`) holding canonical artifacts in a single
git repository, with `mm context install` snapshotting selected artifacts
into a project. The wiki adds reuse and (via git remotes) cross-machine
sync without altering the existing fan-out invariants.

## Background

ADR-0001 fixed the rules for the per-project pipeline (canonical →
fan-out, on_drop severity, phase independence). It deliberately did not
address sharing across projects — each `<project>/.memtomem/` was its own
source of truth, and re-use happened by hand-copying directories or by
git submodules.

Two recurring user scenarios pushed for a higher layer:

1. *I edit `code-review` skill in project A, want it in project B without
   diverging.* Today: `cp -R` and remember to keep them in sync.
2. *I'm setting up a new machine.* Today: there is no portable record of
   "which artifacts were in which project" — each project's `.memtomem/`
   is recreated by hand or by re-running `mm context init` against
   whatever runtime files happened to be present.

The wiki layer addresses both. It is **additive** — projects without a
wiki continue to work exactly as before.

## Decision

Introduce four new surfaces, each governed by an invariant:

### Invariant 1 — Self-containment of project canonical

`<project>/.memtomem/` MUST work without `~/.memtomem-wiki/` present.

`mm context install` snapshots a wiki artifact as a **directory tree**
(`shutil.copytree` semantics) into the project — including any
`overrides/` subdirectory. Fan-out (`generate_all_skills`,
`generate_all_agents`, `generate_all_commands`) reads only from the
project tree, never from the wiki. CI machines, archived projects, and
machines without the wiki all run fan-out unchanged.

### Invariant 2 — Explicit conflict surface for local edits

`mm context update <type> <name>` MUST detect when project canonical was
modified after install (mtime > `lockfile.installed_at`).

Default behavior: refuse with a clear error. `--force` overwrites and
leaves a `.bak` copy of each clobbered file. This mirrors ADR-0001's
on_drop policy of never silently dropping data.

### Invariant 3 — Wiki is optional, project is authoritative

Absence, corruption, or relocation of `~/.memtomem-wiki/` MUST NOT break
existing commands. Only `mm wiki *` and `mm context {install,update,status}`
require it; all other commands ignore the wiki entirely. When the wiki is
absent, the affected commands fail with a precise message
("`wiki not found at <path>, run \`mm wiki init\`"`) rather than a
traceback.

A project with `lock.json` but no wiki on disk continues to fan out
correctly — `lock.json` is metadata, not a runtime dependency.

### Invariant 4 — Override is full-file replacement (v1)

When `<project>/.memtomem/<type>/<name>/overrides/<vendor>.<ext>` exists,
fan-out MUST byte-copy that file to the runtime directory and skip the
auto-conversion pipeline for that vendor.

Section-level merge is explicitly **not** in v1. Override semantics are
"give me exactly this output, do not transform" — diagnosable by reading
the override file and comparing to the runtime target. Section merge can
be reconsidered in v2 if a real workflow demands it.

## Architecture

```
~/.memtomem-wiki/                    ← global wiki (git repo, optional remote)
├── .git/
├── skills/<name>/
│   ├── SKILL.md                     ← canonical (Agent Skills spec)
│   ├── scripts/, references/, ...   ← spec subdirs
│   └── overrides/<vendor>.<ext>     ← optional, per Invariant 4
├── agents/<name>/agent.md
└── commands/<name>/command.md

         │  mm context install <type> <name>   (copytree + lockfile pin)
         ▼
<project>/.memtomem/                 ← project canonical (Invariant 1)
├── lock.json                        ← { skills: { foo: { wiki_commit, installed_at } } }
├── skills/<name>/SKILL.md + overrides/...
├── agents/<name>/...
└── commands/<name>/...

         │  existing fan-out + override resolution (Invariant 4)
         ▼
.claude/, .gemini/, .agents/, .codex/    ← runtime dirs (unchanged from ADR-0001)
```

## Subcommands

`mm wiki` is nested per asset type so `{edit, override, diff, lint}` form
a single mental group of "manipulate this artifact":

```
mm wiki init [--from <git-url>]
mm wiki list

mm wiki skill   {edit, override, diff, lint} <name> [--vendor <vendor>]
mm wiki agent   {edit, override, diff, lint} <name> [--vendor <vendor>]
mm wiki command {edit, override, diff, lint} <name> [--vendor <vendor>]

mm context install <type> <name>
mm context install --all                # lockfile-driven re-setup
mm context update  <type> <name> [--all]
mm context status
```

`mm context sync --include=skills` (existing, ADR-0001 §3) is unchanged.
The `mm wiki` group is single-asset; `mm context sync` remains the
multi-asset bulk verb.

## Lockfile schema

`<project>/.memtomem/lock.json`:

```json
{
  "version": 1,
  "skills": {
    "foo": {
      "wiki_commit": "abc123def4567890abc123def4567890abc12345",
      "installed_at": "2026-04-30T12:34:56Z"
    }
  },
  "agents":   { "bar": { "wiki_commit": "…", "installed_at": "…" } },
  "commands": { "baz": { "wiki_commit": "…", "installed_at": "…" } }
}
```

`wiki_commit` MUST be the **full 40-character SHA**. Display surfaces
(`mm context status`, `mm wiki list`) may abbreviate to 12 characters
for readability; the stored value is always full-length to avoid
abbreviation collisions across projects that share a wiki and to keep
`git checkout <wiki_commit>` directly usable for forensics.

Reads MUST preserve unknown top-level and per-entry fields (round-trip
through plain `dict` is sufficient). The `version` field is reserved for
schema migrations; future fields (`skill_version`, `compat`, `mode`) can
be added forward-compatibly.

## Vendor format matrix

`OVERRIDE_FORMATS = { (asset_type, vendor): (alias, extension) }` lives
in `packages/memtomem/src/memtomem/context/_names.py`. v1 covers Claude,
Gemini, Codex across skills, agents, commands. Cursor and Copilot are
excluded — their skill/agent/command surfaces are too thin to justify
override slots. They can be added in v2 if their runtime surface grows.

## PR Breakdown

| PR | Surface | Invariants |
|----|---------|-----------|
| **A** | Wiki scaffold: `wiki/store.py`, `mm wiki init [--from]`, `mm wiki list`, this ADR | scaffolding only |
| **B** | `mm context install`, lockfile schema, `shutil.copytree`, lockfile concurrency | Inv 1 (copytree), Inv 3 (graceful absence) |
| **C** | Override resolution, `OVERRIDE_FORMATS`, `mm wiki <type> override` (auto-result fork) | Inv 4 |
| **D** | `mm context {update, install --all, status}`, `mm wiki <type> {diff, lint}`, dirty detection | Inv 2 (refuse-if-dirty + `--force` + `.bak`) |
| **E** | Web UI (mirrors `web/routes/context_*` patterns post-#488) | — |

## Consequences

- **Project tree gets `lock.json`** when wiki-installed. Manual edits to
  files under `.memtomem/` after install are detected (Invariant 2);
  manual edits to projects that never used wiki install continue to
  work without a lockfile.
- **`~/.memtomem-wiki/` is a normal git repo.** Backup, sharing, and
  versioning use git remotes — the same workflow as any private repo.
  No new sync protocol.
- **Vendor overrides are opt-in.** Default (no overrides directory)
  means existing fan-out behavior is unchanged. Override usage is
  surfaced in `mm context update` log lines (`[override applied:
  codex]`) so silent application is visible.
- **`settings.json` is excluded from the wiki.** Settings sync mutates
  host-scope files (`~/.claude/settings.json`); the wiki avoids that
  trust boundary entirely. The existing `mm context sync
  --include=settings` flow (ADR-0001) is unaffected.

## Considered & rejected

- **Reference mode (manifest-only, fetch at build time).** Rejected: it
  breaks Invariant 1 (project would depend on wiki being reachable) and
  loses git-checkout reproducibility (`git log .memtomem/` would not
  show what changed).
- **Symlink / submodule deploy.** Rejected: macOS/Linux/Windows symlink
  permission model differs; submodules carry the well-known "forgot to
  `git submodule update`" failure mode.
- **Per-skill semver in frontmatter + CHANGELOG.** Rejected: with a
  single curator (the wiki owner) version metadata becomes paperwork
  that drifts. Wiki repo git history + lockfile commit pin already
  captures "which version did this project install."
- **Section-level override merge.** Rejected for v1; revisit in v2 only
  if a recurring workflow demands it (Invariant 4 rationale).
- **Cursor / Copilot override slots.** Deferred — runtime surface too
  thin in v1.
- **Settings in wiki.** Rejected — host-scope mutation trust boundary.

## References

- ADR-0001 — context gateway sync policies (this ADR builds on top).
- ADR-0007 — namespace CRUD prod exposure (dev/prod tier pattern that
  PR-E reuses for the wiki Web UI surface).
- `packages/memtomem/src/memtomem/context/skills.py` —
  `generate_all_skills` (fan-out reused by Invariant 1; modified in
  PR-C for override branch).
- `packages/memtomem/src/memtomem/context/_names.py` —
  `OVERRIDE_FORMATS` lives here (PR-C).
- `packages/memtomem/src/memtomem/context/projects.py` —
  `KnownProjectsStore`, `_file_lock` pattern reused in PR-B for
  lockfile concurrency.
