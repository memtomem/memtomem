# ADR-0045: Client sessions share one engine through a named daemon and a stdio bridge

**Status:** Accepted
**Date:** 2026-11-17

## Context

An MCP client that speaks stdio launches its own `memtomem-server`, and each of those
processes is a complete stack: a store writer, a file watcher and an embedding model.
Several editor sessions against one store therefore run several stacks, and `mem_status`
reports it as `concurrent_server_writers`.

Measured on one development Mac on 2026-11-15
([#3381](https://github.com/memtomem/memtomem/issues/3381)): 23 `memtomem-server` processes,
8 of them on the same store, about 1.8 GB of resident memory in total, and an average of
about 0.4 % CPU for the busiest one. The cost is memory, then friction from many writers
(writer-busy retries, lifecycle commands that refuse while other servers are alive). It is
not CPU and it is not duplicate embedding: with three servers watching one store, a file
written from outside was embedded once.

The client configuration is the part that cannot change shape. The clients this decision
is about launch a command and talk to it over stdio.

## Decision

A client still launches `memtomem-server` over stdio. With `--shared NAME` that process is
a **bridge**: it passes bytes between its stdio and one unix-socket connection to the
**daemon** called NAME, and starts that daemon when none is running. The daemon runs one
MCP session per connection against one store writer, one file watcher and one embedding
model.

1. **Sharing is explicit and named.** `memtomem-server --shared NAME` and
   `mm serve --shared NAME` are the only way in. Without the option nothing changes. A
   name is 1 to 32 characters from `a-z`, `0-9` and `-`, starting with a letter or digit,
   and it belongs to the operating system user: the runtime directory that holds the
   daemon's socket does not depend on `HOME`.
2. **A session that cannot join is served standalone.** The bridge decides before it
   imports the server. When the session does not join, for any reason, the same process
   continues as an ordinary standalone server and `mem_status` shows
   `Shared daemon: NAME not joined (<reason>)`. `--shared` never costs a client its
   server.
3. **A session joins only when five fields equal the daemon's.** Each side computes them
   once with the same functions:

   | Field | What is compared |
   |---|---|
   | `env` | The name and a SHA-256 of the value of every `MEMTOMEM_*` and `LANGFUSE_*` variable (any case), in environment order, after `.env` |
   | `tools` | The tool mode and the extra tool names, from the environment the process was started with (before `.env`) |
   | `home` | `HOME`, which locates `config.json` |
   | `store` | The resolved store path |
   | `install` | `sys.prefix`, the directory the package was loaded from, and its version |

   Environment values travel only as digests; the other fields go to the daemon as they
   are, over the socket of point 12. The reason shown for a session that did not join
   names the fields that differ and, for `env`, the variables, never a value.
4. **The name asserts the rest.** Proxy and TLS variables, `FASTEMBED_CACHE_PATH` and the
   Hugging Face variables are not compared. Giving two sessions one name says they want
   the same server.
5. **A joined session uses the daemon's live configuration.** Equal `HOME` identifies
   the configuration file, not the settings in use.
6. **The daemon's store is fixed when it starts.** The path is compared, the file behind
   it is not: a daemon keeps serving the file it opened when another is put at that path,
   as a standalone server does. A store that cannot be named (`:memory:`, a `file:` URI)
   and a relative configured path cannot be shared.
7. **The working directory belongs to the connection.** Project context and relative
   path arguments resolve against the client's directory, which the bridge sends when it
   connects.
8. **A bridge ends with its daemon.** No reconnect and no replay: the client sees its
   stdio server exit with status 1, as it does when a standalone server dies. A bridge
   also ends the session, with status 1, when more than 32 MiB waits in either direction.
9. **An idle daemon exits** after 600 seconds with no connection (`--idle-timeout`, `0`
   for never). That exit, and one the daemon takes because it cannot continue, run the
   ordinary teardown.
10. **Stopping is a signal.** `mm daemon stop [NAME]` sends SIGTERM, escalates to SIGKILL
    after `--grace`, and is confirmed by the daemon's lifetime lock being released, never
    by a reply. SIGTERM skips the teardown, as it does for a standalone server; SQLite
    recovers the write-ahead log on the next open.
11. **Lifecycle commands treat a daemon as a server.** A daemon holds a lifetime lock
    under its name and the lifecycle barrier from before it admits a connection until it
    exits. It also takes its store's ordinary pid lock the way a standalone server does,
    so it goes without that one when another server on the store holds it already.
    `mm reset` and `mm uninstall` refuse while a daemon runs and name `mm daemon stop`;
    `mm upgrade` stops it once, however many lock files it holds. Bridges are recorded in a directory of their own, which `mm doctor` counts and
    no lifecycle command consults before it acts.
12. **POSIX only, and no network surface.** On Windows `--shared` is accepted and the
    server runs standalone. The socket has mode 0600 inside the owner-checked runtime
    directory and the daemon checks the peer's uid, so the boundary is the same user, as
    it is for stdio. What the bridge sends when it connects is asserted by the client;
    that is acceptable on this local transport only. ADR-0029 is unchanged.

The guide is [Share one server between sessions](../guides/shared-daemon.md).

## Consequences

- A daemon that crashes or is stopped ends every session that shares it, where a
  standalone crash ends one.
- One process carries every session's CPU-bound work.
- One name is not one writer. A session that does not join opens the store as a
  standalone server, and so do daemons under other names, `mm index`, `mm web` and
  standalone servers. The defences against several processes on one store stay.
- Two sessions whose `MEMTOMEM_*` and `LANGFUSE_*` variables differ once `.env` is
  applied, in name, value or order, do not share. This can only turn away a session that
  could have joined.
- The standalone path stays for good: no option, a field that differs, a store that
  cannot be shared, a daemon that does not answer in time, Windows.
- Releases that predate the daemon do not list the bridge directory, so an older
  `mm doctor` under-reports sessions and an older `mm upgrade` is not blocked by them.

## Alternatives considered

- **Identify the store by the file, not the path** (an inode digest beside the path).
  Every point at which the daemon could read the identity of the file SQLite has open was
  some other moment than the open itself, and each left a window in which the file could
  be replaced. Rejected for the path alone, with the consequence in point 6.
- **Compare a digest of the whole loaded configuration.** It compared outputs the daemon
  is allowed to change while it runs (point 5), and it could not tell a value supplied by
  the environment from one supplied by `config.json`. Rejected for the fixed field list,
  where the variable name carries the provenance.
- **Compare the bytes of the configuration file.** `HOME` already identifies the file,
  and the daemon reads it again at initialization, so comparing bytes would only add a
  race with an edit made between two sessions' starts.
- **Reconnect a bridge to a new daemon, or replay its session.** Session state lives in
  the daemon; a new daemon has none of it. A server that exits is what a client already
  sees when a standalone server dies.
- **Backpressure instead of a buffer limit.** A bridge that stops reading one side until
  the other takes what is waiting cannot see that side close. Each direction is read
  without waiting and bounded at 32 MiB instead.
- **Start the daemon by forking the bridge.** A fork carries the bridge's open
  descriptors and process state into the daemon. A separate helper process starts the
  daemon detached and exits.
- **Record bridges with the other servers.** `mm upgrade` refuses a live registered
  process that holds no pid lock, so a bridge there would block every upgrade. Bridges
  end when their daemon is stopped, so nothing has to signal them.
- **A stop request over the socket**, which would run the teardown. Each form of the
  request that was drafted had defects, and `mm upgrade` already stops every standalone
  server by signal with the same consequence (point 10). A graceful stop can be added
  behind the same command.
- **Carry the same handshake on a network transport.** The fields are the client's own
  assertion about itself; they are not authentication.

## Open questions

1. **Whether the bundled client configurations pass `--shared`.** Not decided, and until
   it is the option stays opt-in and is not recommended as a default. Revisit when both
   hold: search latency while another session runs a long index in the same process has
   been measured, and the option has run in real client sessions long enough to report
   on. Not measured so far: memory with an embedding model loaded, behaviour inside a
   real client session, and that contention. Tracked in
   [#3381](https://github.com/memtomem/memtomem/issues/3381).
