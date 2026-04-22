# Uninstalling memtomem

## Recommended: `mm uninstall`

Since v0.1.23 the CLI ships an `uninstall` subcommand that handles the
state cleanup (steps 3 below) and prints the package-manager command for
your detected install context. It detects `~/.memtomem/`, custom
`storage.sqlite_path` outside the default dir, and config.d fragments,
then deletes them in a low→high-value order with confirmation. It does
NOT touch external editor configs (step 1) — those are reported and left
for you to clean manually.

```bash
mm uninstall                  # interactive, removes everything
mm uninstall -y               # skip the confirmation prompt
mm uninstall --keep-config    # preserve config.json + config.d/* + backups
mm uninstall --keep-data      # preserve the SQLite DB + ~/.memtomem/memories/
mm uninstall --force          # bypass the running-server safety check
```

The command refuses to run while the MCP server is still alive (open WAL
handles risk corruption); stop the server first or pass `--force`.

After `mm uninstall` finishes, follow the binary-removal command it prints
(varies by install context — `uv tool uninstall memtomem`, `pip
uninstall memtomem`, etc.). Then continue with step 1 below to clean up
editor MCP entries.

If you don't have the CLI available (e.g. the wheel is broken or you
never installed it), follow the manual steps below.

---

## Manual cleanup

## 1. Remove the MCP server from your editor

Remove the `"memtomem"` entry from the `mcpServers` block in your editor's
config file, then restart the editor.

| Editor | Config file |
|--------|------------|
| Claude Code | `claude mcp remove memtomem -s user` (or delete from `~/.claude.json`) |
| Cursor | `~/.cursor/mcp.json` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) |
| Gemini CLI | `~/.gemini/settings.json` |
| Antigravity | MCP Servers panel → remove the memtomem entry |

Also delete any project-level `.mcp.json` files that contain a memtomem server
block.

## 2. Uninstall the Python package

Match the command to how you installed:

```bash
# PyPI global install
uv tool uninstall memtomem    # or: pipx uninstall memtomem

# Project dependency
uv remove memtomem            # or: pip uninstall memtomem

# Source install (editable)
pip uninstall memtomem
```

## 3. Delete the data directory

All databases, config, session state, and uploaded files live under
`~/.memtomem/` by default (or the path set via `MEMTOMEM_HOME`):

```bash
rm -rf ~/.memtomem
```

This removes:

| Path | Contents |
|------|----------|
| `memtomem.db` (+ `-wal`, `-journal`) | SQLite database (chunks, embeddings, sessions, history) |
| `config.json` | Persisted configuration overrides |
| `config.d/*.json` | Integration-installed drop-in fragments (if present) |
| `memories/` | User-created memories from `mem_add` |
| `uploads/` | Files uploaded via the Web UI |
| `.current_session` | Active session marker |
| `.server.pid` | MCP server advisory lock |

## 4. Clean up project-scoped files (optional)

If you used `mm context generate` or `mm init`, remove the project-local
directory and any generated rule files:

```bash
rm -rf .memtomem          # context, skills, agents, commands, settings.json
rm -f .cursorrules        # generated Cursor rules (if created by mm context)
```

## 5. Remove hooks from Claude Code settings (optional)

If you ran `mm context sync --include=settings`, memtomem hooks were merged
into `~/.claude/settings.json`. Open the file and remove any hook entries
whose commands reference `memtomem` or `mm`.

---

## Reinstalling from scratch

Switching presets (e.g. `Minimal` → `Korean-optimized`) leaves the previous
SQLite DB in place, because `mm init` only rewrites `~/.memtomem/config.json`
and the MCP registration. If the new preset uses a different embedding
provider or dimension, the server startup will refuse to open a DB whose
stored embedding metadata doesn't match — `mm init` now detects this and
offers to reset the vector index in place.

To skip the prompt and start from a fully blank slate, delete the data
directory before re-running the wizard:

```bash
rm -rf ~/.memtomem
mm init
```

This wipes chunks, embeddings, sessions, uploads, and persisted config.
MCP registrations in each editor are separate — see step 1 above to clean
those up first if you want them regenerated.

---

## Next Steps

- [Reference](reference.md) — Complete feature reference
- [Getting Started](getting-started.md) — Reinstall if you change your mind
