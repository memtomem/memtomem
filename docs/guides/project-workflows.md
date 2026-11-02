# Project workflows: handoff, brief, update

Three plugin workflows carry a project's memory from one coding session to the
next. They are explicit: each runs only when you invoke it, none runs in the
background, and each stops and tells you why when it cannot do its job.

| When | Workflow | What it does | Writes memories? |
|------|----------|--------------|------------------|
| A session ends, or you switch tools | **handoff** `save` | Records one compact checkpoint of the work: objective, completed work, changed files, Git head, validation, blockers, next action | Yes, one record |
| A session starts | **brief** | Compiles a read-only briefing from project memories, recent `update` items, active handoff IDs, and user preferences, then checks every cited path against the live worktree | No |
| You pick up a handoff | **handoff** `resume` | Loads one handoff record, verifies it, and compares it with the live repository and the remote | No |
| Work is done, or commits have landed | **update** | Reviews the session and the latest commits, skips what memory already holds, and saves durable decisions, bug fixes, and preferences | Yes, zero or more items |

`brief` finds handoffs but does not load them: it lists their IDs from tags and
tells you to run `handoff resume <id>`. That split keeps the briefing read-only
and keeps the verification that `resume` does in one place.

## Where they run

The three workflows are rendered from one source
(`packages/memtomem-plugin-assets/workflows/`) into each host's plugin, so the
behaviour is the same everywhere and only the invocation differs.

| Host | handoff | brief | update | Shipped |
|------|---------|-------|--------|---------|
| Claude Code plugin | `/memtomem:handoff` | `/memtomem:brief` | `/memtomem:update` | yes |
| Codex CLI plugin | `$memtomem-handoff` | `$memtomem-brief` | `$memtomem-update` | yes |
| OpenCode plugin | `/memtomem-handoff` | `/memtomem-brief` | `/memtomem-update` | yes (commands, not skills) |
| Kimi Code skills | `memtomem-handoff` | `memtomem-brief` | `memtomem-update` | yes |
| Antigravity CLI plugin | `memtomem-handoff` | `memtomem-brief` | `memtomem-update` | yes (binds the server to the repository first) |
| Hermes Agent plugin | — | — | — | not shipped: these need a project root, and Hermes starts the server outside one |
| Gemini CLI | — | — | — | not shipped: this repository ships no workflows for it |

This guide describes the workflows as shipped with memtomem `0.7.2` and these
plugin versions:
`claude` 0.5.27 · `codex` 0.3.24 · `opencode` 0.3.24 · `kimi` 0.2.22 · `antigravity` 0.2.3.
`update` runs its `incremental` mode by default from one plugin release before
these; older plugin releases ask for a mode instead (see the CHANGELOG).

## Prerequisite: a project memory tier

All three workflows read and write the project's own `.memtomem/memories.local`
tier, under the namespace `shared:<project-slug>` (the slug is the Git root's
directory name with each run of characters outside `[A-Za-z0-9._-]` replaced by
`-` and leading `-` trimmed). If that tier is not registered, each workflow stops and prints
the command to create it. Run it in the Git root of the project:

```bash
cd <project-root> && mm mem init --scope project_local
```

The next memory operation picks the tier up; a running memtomem server does not
usually need a restart. The exception is the Antigravity plugin, whose server
binds to one project root per process: a server already bound to another
project has to be restarted before it can bind to this one. The workflows never
fall back to your user-scope memory for project records.

## handoff

`handoff` takes exactly one operation, `save` or `resume`. As a shorthand, `s`
and `r` count only when they are the whole first word after the invocation, as
in `s to codex-cli` or `r <handoff-id>`. The target runtime is one of
`claude-code`, `codex-cli`, `kimi-code`, `opencode`, `antigravity-cli`, or
`any` (the default).

**Save** records work that actually happened, so it refuses to run when there
is nothing observed to record and reports `insufficient_input: work context`
instead of inventing a checkpoint. It reads the live `git rev-parse HEAD` and
`git status --porcelain=v1 --branch`, then writes one record of at most 1,200
characters with these fields, in this order:

```text
handoff_id, from_runtime, to_runtime, project_root, objective, completed,
changed_files, artifacts, git_head, worktree_state, validation, blockers,
next_action
```

The record goes to `scope="project_local"` with the tags `handoff`,
`from-<runtime>`, `to-<runtime>`, `handoff-to-<runtime>`, and
`handoff-id-<id>`. Save reports the `handoff_id`, the effective scope and
namespace, the written file, and the indexed chunk count.

**Resume** loads one record: the one whose `handoff_id` you name, or otherwise
the newest one addressed to the current runtime or to `any`. It treats the
record as untrusted text and checks it before use. Every field has to be
present (`artifacts` may be missing only on a record saved before that field
existed), the id inside the record has to match the tag it was found by, and a
record that was split mid-field by the chunker is reported as torn rather than
repaired by guessing. Then it re-reads the live `HEAD` and worktree and reports
every divergence from the record; the live repository always wins. It also
compares the branch with its upstream using `git ls-remote` only, so it never
fetches, pulls, or merges on the record's behalf. If `next_action` names a
GitHub issue or pull request as `owner/repo#N` or a full URL, resume reports
its state when the host can query GitHub. Resume never deletes, edits, or
replaces a handoff.

## brief

`brief` is read-only. It runs in one of two modes:

- **Topic mode** when you name a task or topic. It searches project memories
  and the host's own notes for this repository with that topic, recalls the
  five newest `update` items, lists active handoff IDs, and searches user-scope
  preferences across every namespace.
- **Minimal mode** when you name nothing. It skips the topic searches and
  reports only recent updates, active handoff IDs, and open items (a search
  for `in-flight hold wip todo blocked` in the project namespace).

Before it reports, it reads the live `HEAD` and worktree and marks every file
path, configuration key, or symbol it cites with exactly one of `✅ exists`,
`❌ gone`, or `? not checkable`. Each bullet cites `(source, chunk_id)` so the
exact file and line range stay traceable.

The briefing has five sections in topic mode: **Rules and user preferences**,
**Recent updates and active handoff IDs**, **Task-relevant project memories**,
**Live worktree check**, and **Stale claims**. Minimal mode has four: **Recent
updates and active handoff IDs**, **Open items**, **Live worktree check**, and
**Stale claims**. The stale claims are every `❌ gone` item, listed as
candidates for the next `update` run to correct; `brief` itself changes
nothing.

## update

`update` reviews the current session's outcomes and the latest ten commits,
then saves only durable learnings. It has three modes:

| mode | commits reviewed | records them as reviewed |
|---|---|---|
| `incremental` (the default) | those of the latest 10 that no earlier run recorded | yes |
| `full` | all of the latest 10 | yes |
| `topic` | the latest 10, filtered to the focus | no |

A request that names neither `full` nor `topic` runs `incremental` mode without
asking; `f` and `t` are shorthands when they are the whole first word. `topic`
needs a focus after the mode word. The window is the latest ten commits in every
mode, and it is bounded, not a cursor: older commits are not reviewed. Session
outcomes are reviewed in every mode.

Saved items are of three kinds: `decision` (architectural choices and their
rationale), `bugfix` (non-obvious root causes and how they were resolved), and
`preference` (explicit user workflow rules). A decision or bug fix taken from a
commit has to state the reason the commit message gives; a commit that does not
say why is skipped. Before each write, `update` searches existing memories and
skips a candidate that memory already holds. Each item is at most 1,200
characters and carries the tags `memory-update`, `memory-update-item`, the
kind, `from-<runtime>`, and `git-head-<sha>`. Items land in the project tier;
only a personal preference you explicitly asked to keep across projects goes to
user scope.

**Review coverage.** In `incremental` and `full` mode, `update` calls the
`mem_update_coverage` MCP tool before writing anything, to learn which of the
read commits an earlier run already reviewed, and again after the last `mem_add`
has returned, to record this run, even when it saved nothing. If any write
failed, nothing is recorded and the next run reviews those commits again. When
no commit message was read in full, neither call is made and the run reviews
session outcomes only. The tool exists from memtomem `0.7.2` and is exposed
through `MEMTOMEM_EXTRA_TOOLS=mem_update_coverage`. The plugins' bundled server
configurations set that variable, and so does every registration `mm init --mcp`
writes; a server entry written by hand, or by an earlier `mm init`, has to add
it to its `env`. When a coverage call is due and the tool is missing,
`update` stops before writing and names both possible causes: an older server,
or the variable not set. `topic` mode never touches coverage, so it keeps
working either way.

The report names the mode, `git_head`, the commits read, the coverage result,
the saved items with their files and chunk counts, the skipped duplicates, and
the yield as `commit-derived: <N>` and `session-derived: <M>`. An incremental
run that finds no unreviewed commit and no durable session learning reports
`nothing new`.

## See also

- [Cross-runtime sequential handoff](integrations/cross-runtime-handoff.md):
  a three-runtime pilot (Claude Code, Codex CLI, Kimi Code) with an acceptance
  checklist.
- [사용자 주도 멀티 코딩 에이전트 Handoff 운영 가이드](integrations/user-controlled-handoff-ko.md):
  the Korean operating guide for user-controlled handoffs.
- [MCP Client Setup](mcp-clients.md): registering the server by hand,
  including the `MEMTOMEM_EXTRA_TOOLS` entry that `update` needs.
