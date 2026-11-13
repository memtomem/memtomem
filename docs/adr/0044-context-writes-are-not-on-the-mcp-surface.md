# ADR-0044: Context-gateway writes are not on the MCP surface

**Status:** Accepted
**Date:** 2026-11-13

## Context

The context gateway keeps other coding agents' instruction files, skills, sub-agents,
commands, hook settings and MCP server lists in step with a canonical copy under
`.memtomem/`. Its verbs had MCP tools beside the CLI and the web UI, and several ADRs refer
to them: sync (ADR-0021), memory migration and Pull (ADR-0011), version snapshots and
labels (ADR-0022), artifact migration and cross-project transfer (ADR-0008, ADR-0023), and
Pull again (ADR-0030). Each MCP tool guarded its riskier writes with a consent argument: `allow_host_writes` for a write
outside the project root, `confirm_project_shared` for the git-tracked tier, `apply` for
anything beyond a preview.

Over MCP the caller is an agent. An agent that is told, by text it reads, to sync settings
with `allow_host_writes=true` sends exactly the call a person would have approved, and an
agent that does ask the person can only relay what the tool returned, which named the
destination but not the hook commands about to be written. So a project's
`.memtomem/settings.json` hooks could reach `~/.claude/settings.json`,
`~/.codex/hooks.json` and `~/.gemini/settings.json`, and a project symlink such as
`.claude -> ~/.claude` redirected a default-scope skill sync into the user's home. One
chain needed no consent argument at all: migrating a project skill to the user tier
(`apply=true` is the dry-run switch, not a confirmation), then a user-scope skill sync,
installed it for every runtime.

Three attempts to fix this by listing which write routes MCP may or may not take were each
found incomplete by review, and patching flag by flag would leave the flag-free chain open.

## Decision

The context-gateway verbs that write have no MCP tool: init, generate, sync, pull, memory
migration, artifact migration, transfer, and version create/enable/promote/label
deletion. Version listing, a read, went with them, because it was an action of the same
`mem_context_version` tool; `mm context version list` and the web keep it. None of these
is a tool in any tool mode or a `mem_do` action, and `MEMTOMEM_EXTRA_TOOLS` cannot expose
them, because it adds only registered names.

The two verbs that only read stay: `mem_context_detect` and `mem_context_diff`.

Every write stays available from the `mm context` CLI and, where a route exists, the web
UI. An agent with a shell can still run the CLI; the command then goes through the client's
own command approval rather than an argument inside a memory tool call. This ADR does not
claim more than that: it removes the MCP route, it does not confine what a process running
as the user can write.

This joins the posture ADR-0008 already recorded for install, update and the projects
registry (no MCP verb), and extends it to every context write.

`tests/test_context_writes_off_mcp.py` pins it: the registered context actions are exactly
the two reads, every removed action and former alias answers as unknown, and a full-mode
server given a project with a marker hook and a skill refuses every removed tool and leaves
the runtime homes unchanged.

## Consequences

- An agent or workflow that called one of these actions over MCP gets
  `Error: unknown action '...'` and has to use `mm context ...`. The full-mode tool count
  went from 102 to 93.
- The MCP-only parts of the earlier designs no longer exist: ADR-0023 §13's stricter MCP
  transfer surface (registered `scope_id`s only), ADR-0030's MCP Pull with its
  `needs confirmation:` translation, and the MCP vocabulary of the remediation hints.
- Asking the person through the MCP client itself (elicitation), with the content to be
  written shown in the request, is a possible later route back for some writes. It would be
  a new decision; a plan digest echoed back by the agent is not approval, because the agent
  can echo it.

## Alternatives considered

- **Harden each consent flag.** Rejected: any value the agent can pass, it can pass
  without the person, and the migrate-then-sync chain used no flag.
- **Enumerate the allowed or forbidden write destinations at a single check.** Rejected
  after three review rounds each found routes the list missed.
- **An audit hook that refuses protected host paths in the server process.** Kept only as
  possible later hardening: it does not see the application's own `ctypes` renames or
  SQLite's native writes, so it cannot be the guarantee.
