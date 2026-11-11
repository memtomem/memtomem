# memtomem for OpenClaw

An OpenClaw plugin bundle that gives an OpenClaw agent memtomem's long-term memory:
the memtomem MCP server and six skills (search, recall, remember, index, status,
setup) over one local Markdown-first store, the same store Claude Code, Codex CLI
and Hermes use when they run memtomem.

This is a **bundle**, not a memory plugin. It adds tools and skills; it does not
take `plugins.slots.memory`, so OpenClaw's own memory (`memory-core`) keeps working
beside it. OpenClaw reads it as a Codex-format bundle (`.codex-plugin/plugin.json`).

## Requirements

- OpenClaw. Tested with OpenClaw 2026.11.3.
- [uv](https://docs.astral.sh/uv/): the bundle starts the server with `uvx`, which
  downloads Python 3.12 and the pinned memtomem release on first use. `uvx` must be
  on the **Gateway's** `PATH`; a Gateway run as a service (systemd, launchd) often
  has a shorter `PATH` than your shell, and the server then fails with
  `spawn uvx ENOENT`.

## Install

From a checkout of this repository:

```bash
openclaw plugins install --force --accept-capabilities ./packages/memtomem-openclaw-bundle
```

`--force` confirms an install from a local path, which is outside ClawHub review.
`--accept-capabilities` gives the consent OpenClaw asks for before it installs a
bundle that starts an MCP server; without it the install stops and installs nothing.
The bundle takes effect at the next Gateway start.

Check it:

```bash
openclaw plugins inspect memtomem   # Format: bundle, Bundle format: codex
openclaw skills list                # memtomem-search, memtomem-recall, …
```

## What it adds

- **MCP server `memtomem`**, started by OpenClaw in the plugin's directory with
  `PYTHON_DOTENV_DISABLED=1`, so the server does not read a `.env` file (the one
  it would find is normally `~/.env`). OpenClaw's
  [bundle documentation](https://github.com/openclaw/openclaw/blob/847611d/docs/plugins/bundles.md)
  says a stdio MCP server gets only `HOME`, `LOGNAME`, `PATH`, `TERM` and `USER`
  from the Gateway, plus the bundle's own `env`; memtomem has not measured this, but
  if it holds, `MEMTOMEM_*` variables set for the Gateway do not reach the server.
  With no memtomem configuration the server uses its defaults: BM25 search and the
  store under `~/.memtomem`.
- **Tools**, registered as `memtomem__<tool>`: `mem_search`, `mem_recall`,
  `mem_read`, `mem_list`, `mem_status`, `mem_stats`, `mem_add`, `mem_index`,
  `mem_do`, and `mem_update_coverage`. `mem_add`, `mem_index` and `mem_do` change
  the store; `mem_do` reaches every other memtomem action.
- **Skills** `memtomem-search`, `memtomem-recall`, `memtomem-remember`,
  `memtomem-index`, `memtomem-status` and `memtomem-setup`. They are the skills of
  memtomem's user-scope render: the server runs outside your project, so they
  never initialize or register project memory tiers, and `memtomem-setup` asks
  for an absolute notes directory.

## Messaging and group agents

OpenClaw's
[tool-policy documentation](https://github.com/openclaw/openclaw/blob/847611d/docs/gateway/config-tools/tool-policy.md)
says its `coding` and `messaging` tool profiles
include configured MCP tools by default. In a messaging channel or group chat, anyone
who can message the agent could then have it search and read your memtomem store, not
only write to it. Keep the memtomem tools away from those agents with a deny rule in
OpenClaw's tool policy:

```json5
{
  agents: {
    entries: {
      "family-chat": { tools: { deny: ["memtomem__*"] } },
    },
  },
}
```

`openclaw config validate` accepts this rule. That the rule then keeps the tools out of
the agent's turns is OpenClaw's documented behavior, which memtomem has not yet tested
in an agent turn. Per the same documentation, `tools.deny` at the top level applies to
every agent, and `"bundle-mcp"` in a deny list blocks every MCP server, not only
memtomem. The setting is yours to change: remove the rule for an agent that only you
talk to.

## Uninstall

```bash
openclaw plugins uninstall memtomem
```

It asks for confirmation (`--force` skips the question, which a non-interactive run
needs). Uninstalling removes the bundle's settings, install record and directory, not
your memories: the store under `~/.memtomem` and your notes directories stay.

## License and security

Apache-2.0 ([LICENSE](LICENSE)). Report vulnerabilities as described in
[SECURITY.md](SECURITY.md).
