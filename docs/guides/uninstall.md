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
mm uninstall --keep-data      # preserve the SQLite DB + ~/.memtomem/memories/ (uploads/ are still removed)
mm uninstall --force          # bypass stale-pid/db-lock heuristics only
```

The command refuses to run while an MCP server or Web UI still has positive
liveness evidence (open WAL handles risk corruption). Stop every memtomem
process first. `--force` can bypass only stale PID and DB-lock heuristics; it
does **not** override a live instance-registry entry, an open handle on Windows,
or a held lifecycle barrier.

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
| Antigravity CLI (`agy`) | `~/.gemini/antigravity-cli/mcp_config.json` |
| Antigravity IDE | MCP Servers panel → remove the memtomem entry |
| Gemini CLI (consumer free/Pro/Ultra service ended 2026-06-18; enterprise licenses and paid API keys remain supported) | `~/.gemini/settings.json` |
| Codex CLI | `~/.codex/config.toml` (remove the `[mcp_servers.memtomem]` section) |
| Kimi | `~/.kimi/mcp.json` (or `$KIMI_SHARE_DIR/mcp.json` if that variable is set) |

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

## 3. Move the data directory aside

All databases, config, session state, and uploaded files live under
`~/.memtomem/` by default. The state directory location is fixed; only the
SQLite database file can be relocated, via `storage.sqlite_path` in
`config.json` or the `MEMTOMEM_STORAGE__SQLITE_PATH` environment variable
(`mm uninstall` cleans up a custom DB path and its `-wal`/`-shm`/`-journal`
siblings too). If the CLI is unavailable, stop every memtomem process and move
the state aside rather than deleting it.

Find the database path **before** you move anything — `config.json` is inside
the directory you are about to relocate. It can come from three places, highest
precedence first:

1. the `MEMTOMEM_STORAGE__SQLITE_PATH` environment variable,
2. `storage.sqlite_path` in `~/.memtomem/config.json`,
3. `storage.sqlite_path` in any `~/.memtomem/config.d/*.json` fragment.

If none of them is set, the database is inside `~/.memtomem/` and the directory
move below already covers it. Otherwise expand any leading `~` yourself and
pass the absolute path as `db=` — leaving a `-wal` behind next to a future
database is what turns a stale sidecar into a corrupt open.

Run it as one block so a failed `mkdir` stops the moves instead of letting them
land somewhere unintended, and so `$backup` is always set:

```bash
set -eu
db=            # absolute path from the lookup above; leave empty if unset
backup=$(mktemp -d "${HOME}/memtomem-uninstall-backup-XXXXXX")
mv ~/.memtomem "$backup"/
if [ -n "$db" ]; then
  for f in "$db" "$db"-wal "$db"-shm "$db"-journal; do
    if [ -e "$f" ]; then mv "$f" "$backup"/; fi
  done
fi
echo "state moved to $backup"
```

This moves aside:

| Path | Contents |
|------|----------|
| `memtomem.db` (+ `-wal`, `-shm`, `-journal`) | SQLite database (chunks, embeddings, sessions, history) |
| `config.json` | Persisted configuration overrides |
| `config.d/*.json` | Integration-installed drop-in fragments (if present) |
| `memories/` | User-created memories from `mem_add` |
| `uploads/` | Files uploaded via the Web UI |
| `.current_session` | Active session marker |
| `.server.pid` | Legacy MCP server advisory lock — no longer created by current servers (#2003); still cleaned up. Blocks uninstall only when held *exclusively* (a genuine pre-0.1.25 server) |

The running server's pid/flock file lives **outside** `~/.memtomem/` at the
stable per-user runtime anchor: `/tmp/memtomem-<euid>/server-<digest>.pid` on
POSIX, or `<LocalAppData Known Folder>\Temp\memtomem-0\server-<digest>.pid`
on Windows.
This location does not depend on `XDG_RUNTIME_DIR`, `TMPDIR`, `TEMP`, or `TMP`,
so a service and an interactive shell rendezvous on the same liveness evidence
(#2037). `<digest>` comes from the resolved SQLite path, so servers on
different stores do not share one pid lock (#1990).

During the transition, `mm uninstall` also inspects safe pre-#2037 runtime
locations it can derive. It inventories only pid files attributable to the
store being deleted (plus the transitional bare `server.pid`) and leaves other
stores' `server-*.pid` files alone. Retained registry and lifecycle-lock
sidecars are volatile and self-clean. Do not use a wildcard runtime-directory
deletion: it can erase active liveness evidence or another store's state.

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

For a data-only reset that preserves configuration, let the CLI take a database
backup and enforce all liveness checks:

```bash
mm reset --backup --yes
```

For a complete state reset, run `mm uninstall -y`, keep the installed binary
instead of following its package-removal suggestion, then re-run `mm init`.
This removes persisted config as well as chunks, embeddings, sessions, uploads,
and user memories while preserving the CLI's fail-closed safety gates. (Use
`mm init --fresh` only against a config you are keeping: it resets
wizard-untouched canonical settings while preserving credentials, endpoints,
and user-curated lists — after `mm uninstall` there is no config left to
reset.) MCP
registrations in each editor are separate — see step 1 above to clean those up
first if you want them regenerated.

---

## Next Steps

- [Reference](reference.md) — Complete feature reference
- [Getting Started](getting-started.md) — Reinstall if you change your mind
