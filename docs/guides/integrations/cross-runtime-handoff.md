# Cross-runtime sequential handoff

This workflow lets Claude Code, Codex CLI, and Kimi Code take turns on one Git
project and exchange a compact checkpoint through one local memtomem store.
It is designed for one trusted macOS user and sequential sessions. It is not a
chat sync, task queue, lock service, or concurrent multi-agent coordinator.

For a copy-ready Korean workflow where the user reviews each checkpoint and
approves every agent's next action, see the
[user-controlled Handoff guide](user-controlled-handoff-ko.md).

## 1. Prepare one store

Use memtomem `0.4.0` for every runtime. On a fresh machine, initialize the
user-owned store once without adding an extra MCP registration:

```bash
uvx --from 'memtomem==0.4.0' mm init --preset minimal --non-interactive --mcp skip
uvx --from 'memtomem==0.4.0' mm status
```

From the project root, initialize the Git-ignored project tier:

```bash
cd /path/to/project
uvx --from 'memtomem==0.4.0' mm mem init --scope project_local
```

Keep `.memtomem/memories.local` untracked. Do not use `mm context sync` for
this pilot: handoff records use memory tools directly and do not need runtime
instruction files regenerated.

## 2. Connect the three runtimes

- Claude Code: install the base memtomem plugin or keep one manual MCP entry.
- Codex CLI: install the memtomem plugin or keep one manual
  `[mcp_servers.memtomem]` entry.
- Kimi Code: run `mm init --mcp kimi`, or create `~/.kimi-code/mcp.json` by
  hand as shown in the [Kimi Code integration guide](kimi-code.md). Releases
  up to `0.3.14` wrote the legacy `~/.kimi/mcp.json` layout, which current
  Kimi Code does not read — move the entry if you registered with one.

Keep exactly one entry named `memtomem` per client. A plugin plus a
differently named manual entry can start duplicate servers. Separate stdio
processes are expected; they share the configured SQLite and Markdown store.

Load the generated `memtomem-handoff` skill in each runtime. Repository-local
development paths are:

| Runtime | Skill source or installation target |
|---|---|
| Claude Code | `packages/memtomem-claude-plugin/skills/handoff` or `~/.claude/skills/memtomem-handoff` |
| Codex CLI | `plugins/memtomem/skills/memtomem-handoff` or `~/.agents/skills/memtomem-handoff` |
| Kimi Code | `packages/memtomem-kimi-skills/skills/memtomem-handoff` or `~/.kimi-code/skills/memtomem-handoff` |

Start a new session in each runtime after installation.

## 3. Run the round trip

In Claude Code:

```text
/memtomem:handoff save to codex-cli
```

In Codex CLI:

```text
Use $memtomem-handoff to resume the newest handoff for codex-cli.
```

After the intended Codex work, save for Kimi:

```text
Use $memtomem-handoff to save this work for kimi-code.
```

In Kimi Code:

```text
Use the memtomem-handoff skill to resume the newest handoff for kimi-code.
```

After the intended Kimi work, save for Claude with `to_runtime=claude-code`,
then resume it in Claude Code with `/memtomem:handoff resume`.

Each save must report its `handoff_id`, `project_local` scope,
`shared:<project-slug>` namespace, written file, and indexed chunk count. Each
resume must report whether the stored project root, Git commit, and worktree
summary agree with the live repository. Resolve divergence from Git before
continuing.

## 4. Pilot acceptance checklist

- Every client reports the same database path and project root through
  `mem_status`.
- Each runtime resumes only a record addressed to itself or `any`.
- Repeating the same save with its idempotency key does not append a duplicate.
- No credentials, transcript dumps, or patch bodies appear in the Markdown
  memory file.
- `git status --short` shows only intended tracked project changes;
  `.memtomem/memories.local` remains ignored.

Durable reviewed decisions may be saved separately with the existing remember
workflow and an explicitly confirmed `project_shared` scope. Handoff stays
private and append-only by default.

## Deferred parallel mode

Concurrent workers require a separate design for agent-private namespaces,
task ownership, claims and leases, acknowledgement, read-after-write cache
behavior, and Git conflict policy. This sequential workflow intentionally
implements none of those semantics.
