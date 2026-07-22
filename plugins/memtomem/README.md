# memtomem for Codex

This plugin bundles the exact-pinned memtomem MCP server and six focused
skills for search, date-based recall, status, explicit saves, explicit
indexing, and first-time setup.

## Install from this repository

```sh
codex plugin marketplace add /path/to/memtomem
codex plugin add memtomem@memtomem
```

Start a new Codex thread after installation so the skills and MCP server are
loaded. BM25 is the default and requires no embedding provider.

On a completely fresh machine or HOME, initialize the user-owned store once:

```sh
uvx --from 'memtomem==0.3.12' mm init --preset minimal --non-interactive
uvx --from 'memtomem==0.3.12' mm status
```

For project-specific memories, keep the terminal in that project and create
the gitignored local tier explicitly:

```sh
cd /path/to/project
uvx --from 'memtomem==0.3.12' mm mem init --scope project_local
```

The plugin does not self-authorize these trust steps. After initialization,
use `$memtomem-setup` with an explicit path; it performs a one-shot index
without silently registering a watched source.
