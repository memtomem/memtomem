# Share one server between sessions

Every MCP client session that speaks stdio starts its own `memtomem-server`, and each
one is a full stack: a store writer, a file watcher and an embedding model. With
`--shared NAME`, the sessions that ask for the same name are served by one background
process, the **shared daemon**, and each client's `memtomem-server` becomes a small
**bridge** that passes messages to it.

> **Opt-in, POSIX only.** Nothing is shared unless a client passes `--shared`. The option
> is not a recommended default: search latency while another session runs a long index in
> the same process, memory with an embedding model loaded, and behaviour inside a real
> client session have not been measured. On Windows the option is accepted and the
> session is served standalone. The design record is
> [ADR-0045](../adr/0045-shared-daemon-and-stdio-bridge.md).

## When it helps

It saves memory, not CPU. On one development Mac, 23 `memtomem-server` processes held
about 1.8 GB between them, 8 of them on the same store, while the busiest averaged about
0.4 % CPU ([#3381](https://github.com/memtomem/memtomem/issues/3381)). It also removes
the friction of several writers on one store, such as the
[`concurrent_server_writers`](mcp-clients.md#one-server-at-a-time) warning.

It does not make sessions more robust. A daemon that crashes or is stopped ends every
session that shares it.

## Turn it on

Add `--shared NAME` to the arguments of the server your client already launches. For
Claude Code:

```bash
claude mcp add memtomem -s user -e MEMTOMEM_EXTRA_TOOLS=mem_update_coverage -- memtomem-server --shared main
```

`mm serve --shared NAME` does the same for a client that launches `mm serve`.

- **The name** is 1 to 32 characters from `a-z`, `0-9` and `-`, starting with a letter or
  digit. It belongs to your operating-system user, not to `HOME` or to a project.
- **The first session starts the daemon** and later sessions join it. The daemon keeps
  running between sessions and exits after 600 seconds with no connection.
- **Every client that should share must launch the server the same way.** A session
  joins only when it [matches the daemon](#what-has-to-match). Two clients whose
  `MEMTOMEM_*` variables differ once `.env` is applied, or come in a different order, do
  not share.
- **Stdio only.** `--shared` cannot be combined with a network `--transport`.

## Check that a session joined

Ask that session for `mem_status`. A joined session shows the daemon:

```text
Shared daemon:   main (pid 18983, 2 connection(s))
```

A session that asked and did not join shows why, and is served as a standalone server:

```text
Shared daemon:   main not joined (daemon main differs from this session: env (MEMTOMEM_SEARCH__DEFAULT_TOP_K))
```

A session that did not ask has no `Shared daemon` line. Only the session's own
`mem_status` says whether it joined; the commands below report what is running.

## See what is running

```bash
mm daemon status              # every daemon; exit 0 if one runs, 3 if none, 1 if they cannot be listed
mm daemon status main --json  # one daemon as JSON
mm doctor                     # the shared-daemons check counts each daemon's sessions
```

```text
main  running  pid=18983  started=2026-11-17T07:52:11.223633+00:00
```

`mm doctor` reports `1 shared daemon(s) running: main (pid 18983, 2 bridge(s))`. It
counts bridges by the name they asked for, so the number is an inventory, not proof
that a particular session joined.

## Why a session did not join

The reason in `mem_status` names what failed and never a configured value.

| Reason shown | Cause | What to do |
|---|---|---|
| `daemon NAME differs from this session: <fields>` | A [compared field](#what-has-to-match) differs from the daemon's | Launch the clients the same way, or use another name. If the daemon is the one that is out of date, `mm daemon stop NAME` |
| `shared daemons are POSIX only` | Windows | Nothing; the session is standalone |
| `a shared daemon cannot use a relative configured path: <fields>` | A configured path is relative, and a daemon has no working directory of its own | Make the named settings absolute |
| `the configured store (storage.sqlite_path) cannot be named, so it cannot be shared` | The store is `:memory:` or a `file:` URI | Use a file path to share |
| `the configured store (storage.sqlite_path) cannot be resolved (<error type>)` | The store path cannot be resolved | Fix `storage.sqlite_path` |
| `the configured store (storage.sqlite_path) resolves two ways; refusing to share it` | Two readings of the configuration give different store paths | Set `storage.sqlite_path` in one place |
| `the configuration is not valid: <fields>` | A setting failed validation | Fix the named settings |
| `the configuration could not be read (<error type>)` | The configuration could not be loaded | Fix `config.json` or the environment |
| `a configured path cannot be checked (<error type>)` | A path cannot be expanded, an unknown `~user` for example | Fix the path |
| `the runtime directory is not usable: <error>` | The per-user runtime directory failed its checks | Follow the error |
| `cannot connect to <socket>: <error>` | The daemon's socket could not be reached | Follow the error |
| `daemon NAME did not accept within 10 s (log: <path>)` | No daemon accepted the session in time | Read the log. If there is none, run `memtomem-server --daemon NAME` in a terminal to see the error |
| `the launcher of daemon NAME could not be started (<error type>)` | The process that starts the daemon did not start | Follow the error |
| `the launcher of daemon NAME did not finish within 5 s` | The process that starts the daemon hung | Retry |
| `the launcher of daemon NAME failed (exit <code>)` | The process that starts the daemon failed | Run `memtomem-server --daemon NAME` in a terminal to see the error; a daemon that never started leaves no log |
| `daemon NAME refused the session: <the daemon's text>` | The daemon rejected the request and its text says why: a working directory that does not exist or is not a directory, or a request from another version | Fix what the text names. For a version error, `mm daemon stop NAME` |
| `daemon NAME did not compare the session (an older daemon)` | A daemon from an earlier version is running under that name | `mm daemon stop NAME` |
| `joining daemon NAME failed (<error type>)` | Anything else | Report it with the error type |

## What has to match

A session joins when these five fields equal the daemon's.

| Field | What is compared |
|---|---|
| `env` | Every `MEMTOMEM_*` and `LANGFUSE_*` variable, by name, value and order, after `.env` is applied |
| `tools` | The tool mode and the extra tools the process was started with |
| `home` | `HOME` |
| `store` | The resolved path of the store |
| `install` | The Python environment, the location the package was loaded from, and its version |

Other inputs are not compared: proxy and TLS variables, `FASTEMBED_CACHE_PATH` and the
Hugging Face variables. Giving sessions one name asserts that they want the same server.
A joined session uses the daemon's live configuration, not a fresh read of
`config.json`.

## What still opens the store

One name is not one writer. These open the store as separate processes:

- a session that asked for a name and did not join,
- a session that did not ask,
- a daemon under another name,
- `mm index` and other CLI writers, and `mm web`.

Two joined sessions on one store produce no `concurrent_server_writers` warning. Add a
session that did not join and the warning appears in all three, the joined ones
included.

## Stopping, and what else ends a daemon

```bash
mm daemon stop main   # one daemon
mm daemon stop        # every running daemon
```

`stop` prints `stopped main pid=18983`. Every session connected to that daemon ends: its
`memtomem-server` exits with status 1. Whether a new server starts by itself or needs a
reconnect or a restart is up to the client. A session that was served standalone is not
affected.

The stop is a signal: SIGTERM, then SIGKILL after `--grace` seconds (default 5) if the
process is still there. The daemon does not drain its sessions or close the store first,
which is what happens to a standalone server too; SQLite recovers its write-ahead log on
the next open.

A daemon also ends in these cases:

- **Idle.** After 600 seconds with no connection it shuts down in order and exits 0. A
  session that leaves normally exits 0 and does not disturb the others.
- **It cannot continue.** The daemon builds its engine when the first session needs it,
  or right after it starts when warmup is enabled.
  If the configuration it reads then selects another store than the one it started on,
  names a store that cannot be resolved, or holds a relative path, the daemon logs the
  reason and exits non-zero, and its sessions end.
- **It crashes.** Every session ends.

A change to `config.json` after the engine is built does not end the daemon. A relative
memory directory added then is refused and the previous directories stay.

A daemon that would start with a relative configured path does not start (exit 2).

## Upgrade, reset, uninstall

- **`mm upgrade`** stops a daemon as it stops a server, once, and removes the lock files
  it holds: its name's, and its store's unless another server on that store held that
  one first. Its sessions end, and the next session starts a daemon on the new version.
- **`mm reset`** and **`mm uninstall`** refuse while a daemon runs, and say which:

  ```text
  Shared daemon 'main' (pid 18983) is running; stop it with `mm daemon stop main`.
  ```

  `--force` does not override the refusal.

## After replacing the store file or editing the install

A running daemon keeps the file it opened and the code it loaded. Run
`mm daemon stop NAME` after you

- put another file at the store's path, restoring a backup for example, or
- edit an editable install in place.

A package moved to another version or another location is noticed without that: `install`
differs, so new sessions do not join the old daemon and say so.

## Limits

- **No reconnect.** When its daemon goes away a session ends with status 1.
- **More than 32 MiB waiting in either direction ends the session** with status 1: daemon output
  the client has not read, or client input the daemon has not taken.
- **One message from the client larger than 32 MiB ends that session.**
- **One process does every session's work.** A long index in one session and a search
  in another share that process.

## Usage errors

These are rejected when the server starts, with exit status 2, and are not turned into
a standalone session:

- `--shared and --daemon cannot be combined`
- `--shared is stdio only`
- `--daemon takes no transport options: <options>`
- `--idle-timeout needs --daemon`
- a name that does not fit the rule above (`is not a daemon name`)

## Running a daemon yourself

```bash
memtomem-server --daemon main --idle-timeout 0
```

This runs the daemon in the foreground with the environment of that shell, logging to
its standard error, and `--idle-timeout 0` keeps it running with no sessions. A daemon
that a session starts writes `daemon-NAME.log` in the per-user runtime directory
instead (`/tmp/memtomem-<uid>/` on macOS and Linux), beside its socket
`daemon-NAME.sock` and its lock `server-daemon-NAME.pid`. The log is replaced each time
the daemon starts and stays after it stops; `mm uninstall` removes it when it deletes
state.

The socket is readable and writable by your user only, and the daemon checks that the
process connecting is yours. There is no network listener.
