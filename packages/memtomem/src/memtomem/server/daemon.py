"""Shared daemon: one MCP session per unix-socket connection, one engine (#3381).

A daemon serves every connection that reaches its socket from one component
graph: one store writer, one file watcher, one embedding model. Each connection
is its own MCP session with its own session state and its own client working
directory. ``memtomem-server --daemon NAME`` starts one, and a client reaches it
through ``memtomem-server --shared NAME``, the stdio bridge in
:mod:`memtomem._bridge`, which also starts one when none is running.

A connection is admitted only when the bridge's fields
(:mod:`memtomem._admission`) equal the ones this daemon computed at start;
otherwise it is answered ``mismatch`` with the names of what differs.

What a daemon holds, and from when:

* a lifetime lock, ``server-daemon-<name>.pid``, from before anything else, so a
  second daemon under the same name exits before it can open a store;
* its store's ordinary pid lock, ``server-<digest>.pid``, taken as a standalone
  server takes it, so the store-scoped probes of ``mm reset`` and
  ``mm uninstall`` see a daemon as they see a server;
* a shared lifecycle barrier from before the first connection is admitted until
  exit, independent of the owner's own, which an initialization rollback
  releases;
* one runtime reference of its own, so the engine outlives the gaps between
  connections until the idle timeout.

The store is fixed when the daemon starts. A rebuilt configuration that selects
another store, or any relative configured path, ends the daemon rather than
being opened. POSIX only.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import logging
import os
import socket
import struct
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from memtomem._daemon_paths import NAME_PATTERN, lifetime_lock_path, log_path, socket_path

logger = logging.getLogger(__name__)

# 2: the preamble carries the admission fields, and ``accepted`` says they were
# compared. Version 1 was never released.
PROTOCOL_VERSION = 2
PREAMBLE_LIMIT = 64 * 1024
PREAMBLE_TIMEOUT_S = 5.0
FRAME_LIMIT = 32 * 1024 * 1024
DEFAULT_IDLE_TIMEOUT_S = 600.0
TICK_S = 1.0
# How long teardown waits for answers already on their way before it closes the
# sessions' streams. It waits for the session tasks themselves without a bound:
# the engine and the barrier must not be released under a running handler.
CONNECTION_DRAIN_S = 10.0

State = Literal["starting", "accepting", "draining", "exiting"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


class DaemonStartError(Exception):
    """The daemon refuses to start; the message says why."""


# ``sizeof(sun_path)`` less the terminating NUL: 104 on macOS and the BSDs, 108 on Linux.
_SUN_PATH_MAX = 103 if sys.platform == "darwin" or "bsd" in sys.platform else 107


def check_socket_path(path: Path) -> None:
    if len(os.fsencode(path)) > _SUN_PATH_MAX:
        raise DaemonStartError(
            f"the socket path {str(path)!r} is longer than this platform's limit of "
            f"{_SUN_PATH_MAX} bytes"
        )


def validate_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise DaemonStartError(
            f"daemon name {name!r} is not valid: use 1 to 32 characters from a-z, 0-9 "
            "and '-', starting with a letter or digit"
        )
    return name


# ── peer credentials ─────────────────────────────────────────────────────────

_SOL_LOCAL = 0  # macOS <sys/un.h>
_XUCRED_SIZE = 76  # u_int version, uid_t uid, short ngroups (+pad), gid_t groups[16]


def peer_uid(sock: socket.socket) -> int | None:
    """The effective uid of the process on the other end, or ``None`` if unknown.

    ``None`` means refuse: a platform with no credential query, or a query that
    failed, is not evidence that the peer is this user.
    """
    try:
        if hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return parse_ucred(raw)
        local_peercred = getattr(socket, "LOCAL_PEERCRED", None)
        if local_peercred is not None:
            raw = sock.getsockopt(_SOL_LOCAL, local_peercred, _XUCRED_SIZE)
            return parse_xucred(raw)
    except OSError:
        return None
    return None


def parse_ucred(raw: bytes) -> int:
    """``struct ucred`` (Linux): pid, uid, gid."""
    _pid, uid, _gid = struct.unpack("3i", raw[: struct.calcsize("3i")])
    return uid


def parse_xucred(raw: bytes) -> int:
    """``struct xucred`` (macOS): version, then uid."""
    _version, uid = struct.unpack("=II", raw[:8])
    return uid


# ── preamble ─────────────────────────────────────────────────────────────────


class PreambleError(ValueError):
    pass


@dataclass(frozen=True)
class Preamble:
    cwd: Path
    fields: dict[str, Any]


def parse_preamble(line: bytes) -> Preamble:
    """The client working directory and admission fields of a connection's first line."""
    from memtomem._admission import malformed

    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise PreambleError("the first line is not JSON") from exc
    if not isinstance(payload, dict):
        raise PreambleError("the first line is not a JSON object")
    if payload.get("v") != PROTOCOL_VERSION:
        raise PreambleError(f"unsupported bridge protocol version {payload.get('v')!r}")
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise PreambleError("cwd is missing")
    if "\x00" in cwd:  # ``Path.resolve`` raises a bare ``ValueError`` on it
        raise PreambleError("cwd contains a NUL character")
    given = Path(cwd)
    if not given.is_absolute():
        raise PreambleError("cwd must be an absolute path")
    try:
        resolved = given.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreambleError("cwd does not exist") from exc
    if not resolved.is_dir():
        raise PreambleError("cwd is not a directory")
    fields = payload.get("admission")
    if (reason := malformed(fields)) is not None:
        raise PreambleError(reason)
    assert isinstance(fields, dict)  # narrowed by ``malformed``
    return Preamble(resolved, fields)


def _request_id(message: Any) -> object | None:
    """The id of a validated JSON-RPC request; ``None`` for anything else."""
    root = getattr(message, "root", message)
    if getattr(root, "method", None) is None:
        return None
    return getattr(root, "id", None)


def _response_id(message: Any) -> object | None:
    """The id of a JSON-RPC response or error; ``None`` for anything else."""
    root = getattr(message, "root", message)
    if getattr(root, "method", None) is not None:
        return None
    return getattr(root, "id", None)


# ── the daemon ───────────────────────────────────────────────────────────────


@dataclass(eq=False)
class _Connection:
    """One admitted connection's session, from admission to the end of its task."""

    id: int
    stream: Any
    # Requests read and not yet answered, by JSON-RPC id. Teardown waits for this
    # to empty before closing the stream, so an answer already on its way, the
    # error of the call that ended the daemon included, still reaches its client.
    pending: dict[object, int] = field(default_factory=dict)
    quiet: Any = None  # anyio.Event, set while ``pending`` is empty


@dataclass(eq=False)
class Daemon:
    """One named daemon process. Built by :func:`main`; one per process.

    The lifecycle is nested so that each release can only run after what
    depends on it has ended, on every exit path, an exception or a cancellation
    included:

    * the listener and the barrier, in an ``AsyncExitStack`` (the listener is
      closed and the socket removed last, the barrier released just before);
    * inside it, the task group that accepts and answers every connection, so
      a client is answered ``retry`` or ``refused`` until the listener closes;
    * inside that, the engine (the daemon's runtime reference), released in a
      shielded ``finally``;
    * inside that, the task group of admitted sessions, whose exit is the join:
      the engine is released only once every session task has ended.
    """

    name: str
    idle_timeout_s: float
    frozen_store: Path
    runtime_dir: Path
    sigterm_targets: list[Any]
    # This process's admission fields (:func:`memtomem._admission.local_fields`),
    # computed once at start; a joining bridge's must equal them.
    admission: dict[str, Any] = field(default_factory=dict)
    barrier: Any = None
    state: State = "starting"
    terminal_reason: str | None = None
    exit_reason: str | None = None
    _lock: Any = None
    _wake: Any = None
    _runtime_state: Any = None
    _owner: Any = None
    _sessions: Any = None
    _deadline: float | None = None
    _admitted: int = 0
    _connections: dict[int, _Connection] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _socket_identity: tuple[int, int] | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def socket_path(self) -> Path:
        return socket_path(self.runtime_dir, self.name)

    # ── terminal notification (called by the owner, under its _init_lock) ──

    def notify_terminal(self, reason: str) -> None:
        """Record a failure that ends the daemon. Synchronous: no lock, no teardown.

        The state moves to ``exiting`` at once, so no further connection is
        admitted; the supervisor starts the teardown, which waits for answers
        already on their way (:class:`_Connection` ``pending``).
        """
        if self.terminal_reason is None:
            self.terminal_reason = reason
            logger.error("shared daemon %r cannot continue: %s", self.name, reason)
        self.state = "exiting"
        if self._wake is not None:
            self._wake.set()

    def configure_owner(self, owner: Any) -> None:
        owner._frozen_store = self.frozen_store
        owner._on_terminal = self.notify_terminal

    # ── socket ──

    def _socket_is_ours(self) -> bool:
        if self._socket_identity is None:
            return False
        try:
            st = os.lstat(self.socket_path)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == self._socket_identity

    def unlink_socket_if_ours(self) -> None:
        """Remove the socket path only while it still names the socket bound here.

        Safe in a signal handler: one ``lstat`` and one ``unlink``, no lock.
        """
        if self._socket_is_ours():
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)

    def bind_socket(self) -> socket.socket:
        """Bind and listen, synchronously, while the caller defers SIGTERM.

        Bound, identified and enrolled in the SIGTERM cleanup in one span, so a
        signal can never find a socket that nothing will remove.
        """
        path = self.socket_path
        # The lifetime lock is held, so no live daemon owns a socket at this path.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            previous = os.umask(0o177)
            try:
                sock.bind(str(path))
            finally:
                os.umask(previous)
            # Owned from here: every later failure removes it.
            st = os.lstat(path)
            self._socket_identity = (st.st_dev, st.st_ino)
            os.chmod(path, 0o600)
            # First in the live SIGTERM list, so the socket goes while the
            # lifetime lock, a later entry, is still held.
            self.sigterm_targets.insert(0, self.unlink_socket_if_ours)
            sock.listen(128)
            sock.setblocking(False)
        except BaseException:
            self.unlink_socket_if_ours()
            sock.close()
            raise
        return sock

    # ── admission ──

    def _arm_deadline(self) -> None:
        self._deadline = time.monotonic() + self.idle_timeout_s if self.idle_timeout_s > 0 else None

    async def _admit(self) -> Literal["accepted", "retry", "refused"]:
        async with self._lock:
            if self.state == "accepting":
                self._admitted += 1
                self._deadline = None
                return "accepted"
            return "refused" if self.state == "exiting" else "retry"

    async def _release_admission(self) -> None:
        import anyio

        with anyio.CancelScope(shield=True):
            async with self._lock:
                self._admitted -= 1
                if self._admitted == 0 and self.state == "accepting":
                    self._arm_deadline()

    def _reply(self, state: str, **extra: object) -> bytes:
        payload: dict[str, object] = {
            "v": PROTOCOL_VERSION,
            "state": state,
            "store": str(self.frozen_store),
            "prefix": sys.prefix,
            "pid": os.getpid(),
        }
        payload.update(extra)
        return json.dumps(payload).encode("utf-8") + b"\n"

    def _differences(self, theirs: dict[str, Any]) -> list[str]:
        from memtomem._admission import differences

        return differences(self.admission, theirs)

    # ── connections ──

    async def _handle(self, stream: Any) -> None:
        """Answer one accepted connection; an admitted one continues as a session.

        Runs in the answering task group. Nothing it raises reaches another
        connection. Ownership of an admitted stream passes to the session task.
        """
        import anyio
        from anyio.abc import SocketAttribute
        from anyio.streams.buffered import BufferedByteReceiveStream

        handed_over = False
        try:
            raw = stream.extra(SocketAttribute.raw_socket)  # nosec B610 - anyio, not Django
            if peer_uid(raw) != os.geteuid():
                logger.warning("refused a connection from another user")
                return
            buf = BufferedByteReceiveStream(stream)
            line: bytes | None = None
            with anyio.move_on_after(PREAMBLE_TIMEOUT_S):
                try:
                    line = await buf.receive_until(b"\n", PREAMBLE_LIMIT)
                except (anyio.EndOfStream, anyio.IncompleteRead, anyio.DelimiterNotFound):
                    return
            if line is None:
                return  # no preamble in time
            try:
                preamble = parse_preamble(line)
            except PreambleError as exc:
                await stream.send(self._reply("error", error=str(exc)))
                return
            cwd = preamble.cwd
            differs = self._differences(preamble.fields)
            if differs:
                # Before admission: nothing is counted and the idle timer is
                # untouched. A daemon that is not admitting says so first, so
                # the bridge waits for its successor, which may well match.
                if self.state == "accepting":
                    reply = self._reply("mismatch", differs=differs)
                else:
                    reply = self._reply("refused" if self.state == "exiting" else "retry")
                await stream.send(reply)
                return
            verdict = await self._admit()
            if verdict != "accepted":
                await stream.send(self._reply(verdict))
                return
            conn = _Connection(next(self._ids), stream, quiet=anyio.Event())
            conn.quiet.set()
            self._connections[conn.id] = conn
            # Admitted only while ``accepting``, which holds only while the
            # session group is open (``_run_engine``).
            try:
                self._sessions.start_soon(self._session, conn, buf, cwd)
            except RuntimeError:
                # The session group is closing; this admission never started.
                self._connections.pop(conn.id, None)
                await self._release_admission()
                return
            handed_over = True
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
            pass
        except Exception:
            logger.exception("shared daemon connection failed before its session")
        finally:
            if not handed_over:
                with anyio.CancelScope(shield=True):
                    with contextlib.suppress(Exception):
                        await stream.aclose()

    async def _session(self, conn: _Connection, buf: Any, cwd: Path) -> None:
        """One admitted session, in the session group. Its failure is its own."""
        import anyio

        try:
            # Sent here, not by the handler: the stream belongs to this task now.
            await conn.stream.send(self._reply("accepted", compared=True))
            await self._run_session(conn, buf, cwd)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
            pass
        except Exception:
            logger.exception("shared daemon connection %d failed", conn.id)
        finally:
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(Exception):
                    await conn.stream.aclose()
            self._connections.pop(conn.id, None)
            await self._release_admission()

    async def _run_session(self, conn: _Connection, buf: Any, cwd: Path) -> None:
        import anyio
        import mcp_types as types
        from mcp.shared._context_streams import create_context_streams
        from mcp.shared.message import SessionMessage

        from memtomem.server import mcp as server
        from memtomem.server.lifespan import connection_client_cwd

        read_w, read_s = create_context_streams[SessionMessage | Exception](0)
        write_s, write_r = create_context_streams[SessionMessage](0)
        ends = (
            anyio.EndOfStream,
            anyio.IncompleteRead,
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            anyio.DelimiterNotFound,
        )

        async def reader() -> None:
            async with read_w:
                while True:
                    try:
                        line = await buf.receive_until(b"\n", FRAME_LIMIT)
                    except ends:
                        # An oversized frame ends this connection, not the daemon.
                        return
                    try:
                        json.loads(line)
                    except ValueError:
                        logger.warning("shared daemon connection sent a line that is not JSON")
                        return
                    try:
                        message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                    except Exception as exc:
                        await read_w.send(exc)
                        continue
                    request_id = _request_id(message)
                    if request_id is not None:
                        conn.pending[request_id] = conn.pending.get(request_id, 0) + 1
                        if conn.quiet.is_set():
                            conn.quiet = anyio.Event()
                    await read_w.send(SessionMessage(message))

        async def writer() -> None:
            async with write_r:
                async for item in write_r:
                    data = item.message.model_dump_json(by_alias=True, exclude_unset=True)
                    try:
                        await conn.stream.send(data.encode("utf-8") + b"\n")
                    except ends:
                        return
                    answered_id = _response_id(item.message)
                    if answered_id is not None and answered_id in conn.pending:
                        conn.pending[answered_id] -= 1
                        if conn.pending[answered_id] <= 0:
                            del conn.pending[answered_id]
                        if not conn.pending:
                            conn.quiet.set()

        low = server._lowlevel_server
        # ``Server.run`` enters the lifespan in this task, which is how the
        # connection's working directory reaches its context.
        token = connection_client_cwd.set(cwd)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                tg.start_soon(writer)
                await low.run(read_s, write_s, low.create_initialization_options())
                tg.cancel_scope.cancel()
        finally:
            connection_client_cwd.reset(token)

    async def _accept_loop(self, listener: Any, answering: Any) -> None:
        import anyio

        while True:
            try:
                stream = await listener.accept()
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                return  # the listener was closed
            except OSError:
                logger.warning("shared daemon accept failed", exc_info=True)
                await anyio.sleep(0.1)
                continue
            answering.start_soon(self._handle, stream)

    # ── supervision ──

    async def _supervise(self) -> None:
        """Return once the daemon has decided to end; the state says why."""
        import anyio

        while True:
            with anyio.move_on_after(TICK_S):
                await self._wake.wait()
            async with self._lock:
                if self.terminal_reason is not None:
                    self.state = "exiting"
                    self.exit_reason = self.terminal_reason
                    return
                if not self._socket_is_ours():
                    self.state = "exiting"
                    self.exit_reason = "the socket path no longer names this daemon's socket"
                    return
                if (
                    self.state == "accepting"
                    and self._admitted == 0
                    and self._deadline is not None
                    and time.monotonic() >= self._deadline
                ):
                    self.state = "draining"
                    self.exit_reason = "idle"
                    return

    async def _stop_sessions(self) -> None:
        """Let answers already on their way go out, then end every session.

        Closing a session's stream ends its reader, and its ``Server.run``
        returns normally, which runs its lifespan's release. A session that
        does not end then is still waited for, by the session group's exit.
        """
        import anyio

        conns = list(self._connections.values())
        # Everything keeps flowing, client responses and cancellations included;
        # only the stream close waits for the answers already owed.
        with anyio.move_on_after(CONNECTION_DRAIN_S):
            for conn in conns:
                await conn.quiet.wait()
        for conn in conns:
            with contextlib.suppress(Exception):
                await conn.stream.aclose()

    async def _run_engine(self) -> None:
        """Hold the engine for the daemon's life; release it after every session."""
        import anyio

        from memtomem.server import mcp as server
        from memtomem.server.lifespan import _acquire_server_runtime, _release_server_runtime

        self._runtime_state, self._owner = await _acquire_server_runtime(
            server, configure_owner=self.configure_owner
        )
        try:
            async with anyio.create_task_group() as sessions:
                self._sessions = sessions
                async with self._lock:
                    if self.state == "starting":
                        self.state = "accepting"
                        self._arm_deadline()
                try:
                    await self._supervise()
                finally:
                    with anyio.CancelScope(shield=True):
                        # On every path out, the supervisor's exception included:
                        # nothing is admitted once the sessions are being stopped.
                        async with self._lock:
                            if self.state in ("starting", "accepting"):
                                self.state = "exiting"
                                self.exit_reason = self.exit_reason or "the daemon failed"
                        await self._stop_sessions()
            # The session group has joined every session here.
        finally:
            with anyio.CancelScope(shield=True):
                state, owner = self._runtime_state, self._owner
                cancelled = await _release_server_runtime(state)
                if cancelled is not None:
                    self.failures.append("closing the engine was cancelled")
                if state.close_failed:
                    self.failures.append("closing the engine failed")
                if state.references != 0:
                    self.failures.append(
                        f"{state.references} engine reference(s) outlived their sessions"
                    )
                # ``AppContext.close`` keeps its barrier when it cannot confirm
                # that storage closed; ``_stop_quietly`` only logs that.
                if owner is not None and getattr(owner, "_lifecycle_barrier", None) is not None:
                    self.failures.append("the store could not be confirmed closed")

    def _release_barrier(self) -> None:
        if self.barrier is not None:
            self.barrier.release()
            self.barrier = None

    async def _close_listener(self, listener: Any) -> None:
        try:
            await listener.aclose()
        finally:
            self.unlink_socket_if_ours()

    async def serve(self, sock: socket.socket) -> int:
        """Admit until idle or terminal, then end; the process exit code."""
        import anyio
        from anyio.abc import SocketListener

        self._lock = anyio.Lock()
        self._wake = anyio.Event()
        try:
            async with contextlib.AsyncExitStack() as stack:
                try:
                    listener = await SocketListener.from_socket(sock)
                except BaseException:
                    sock.close()
                    raise
                stack.push_async_callback(self._close_listener, listener)  # runs last
                stack.callback(self._release_barrier)
                async with anyio.create_task_group() as answering:
                    answering.start_soon(self._accept_loop, listener, answering)
                    try:
                        await self._run_engine()
                    finally:
                        answering.cancel_scope.cancel()
        except Exception as exc:
            logger.exception("shared daemon %r failed", self.name)
            self.failures.append(str(exc))
        if self.exit_reason not in (None, "idle"):
            logger.error("shared daemon %r exiting: %s", self.name, self.exit_reason)
        if self.failures:
            logger.error("shared daemon %r: %s", self.name, "; ".join(self.failures))
        elif self.terminal_reason is not None and self.exit_reason == "idle":
            logger.error("shared daemon %r exiting: %s", self.name, self.terminal_reason)
        return self.exit_code()

    def summary(self) -> dict[str, object]:
        """What ``mem_status`` shows: name, pid and open connections."""
        return {"name": self.name, "pid": os.getpid(), "connections": self._admitted}

    def exit_code(self) -> int:
        """The process exit code once :meth:`serve` has torn everything down.

        A terminal failure fails the exit even when it was reported after the
        idle exit had been chosen, during teardown.
        """
        if self.failures or self.terminal_reason is not None:
            return EXIT_FAILED
        return EXIT_OK if self.exit_reason == "idle" else EXIT_FAILED

    def status(self) -> dict[str, object]:
        """What a test or a later ``mm daemon status`` reads."""
        state = self._runtime_state
        owner = self._owner
        components = getattr(owner, "_components", None) if owner is not None else None
        watcher = getattr(owner, "_watcher", None) if owner is not None else None
        return {
            "name": self.name,
            "pid": os.getpid(),
            "state": self.state,
            "admitted": self._admitted,
            "references": state.references if state is not None else 0,
            "components_id": id(components) if components is not None else None,
            "watcher_id": id(watcher) if watcher is not None else None,
            "store": str(self.frozen_store),
            "barrier_held": self.barrier is not None,
        }


# ── start ────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m memtomem.server.daemon",
        description=(
            "Run a shared memtomem daemon (internal: use memtomem-server --daemon NAME, "
            "or --shared NAME, which starts one)."
        ),
    )
    parser.add_argument("name")
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_S,
        help="seconds with no connection before exiting; 0 never exits",
    )
    # Set by the bridge's spawn, whose daemon has no stderr of its own.
    parser.add_argument("--log-to-runtime", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.idle_timeout < 0:
        parser.error("--idle-timeout must be 0 or more")
    return args


def _resolve_frozen_store() -> tuple[Path, Path]:
    """``(pid-naming path, frozen identity)``, or :class:`DaemonStartError`."""
    from memtomem._store_identity import StoreNotShareable, resolve_shared_store
    from memtomem.server import _resolve_store_db_path

    try:
        return resolve_shared_store(_resolve_store_db_path())
    except StoreNotShareable as exc:
        raise DaemonStartError(str(exc)) from exc


def _lock_lifetime(path: Path, targets: list[Any], process_started: str) -> Any:
    """Take the name's lifetime lock and enroll it in cleanup at once."""
    import atexit

    from memtomem._pid_lock import lock_pid_file

    # The default bounded retry, not ``0``: a stale-file cleanup that briefly
    # locks this file to delete it delays a starting daemon instead of ending it.
    fp = lock_pid_file(path, label="shared daemon")
    if fp is None:
        raise DaemonStartError(f"another daemon is already running under this name ({path})")
    targets.append(path)

    def _cleanup() -> None:
        # POSIX only: unlink while the flock is held, then release it.
        path.unlink(missing_ok=True)
        fp.close()

    # Enrolled before the payload is written, so a failed write still removes it.
    atexit.register(_cleanup)
    fp.seek(0)
    fp.truncate()
    fp.write(f"{os.getpid()}\n\n{process_started}\n")
    fp.flush()
    return fp


def _log_to_runtime(path: Path) -> None:
    """Send this process's stderr, the log included, to ``path`` (0600, truncated).

    Opened only once the shared barrier is held, as the socket is bound: a
    concurrent ``mm uninstall`` removes these logs inside its exclusive hold,
    when no daemon can be this far.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        sys.stderr.flush()
        os.dup2(fd, 2)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _published_summary(daemon: Daemon) -> Iterator[None]:
    """Let ``mem_status`` see *daemon* while it serves, and only then."""
    from memtomem.server import context as server_context

    previous = server_context._shared_daemon_summary
    server_context._shared_daemon_summary = daemon.summary
    try:
        yield
    finally:
        server_context._shared_daemon_summary = previous


def main(argv: list[str] | None = None) -> int:
    """Run one shared daemon; returns the process exit code."""
    process_started = datetime.now(UTC).isoformat()
    if os.name == "nt":
        print("memtomem shared daemon: POSIX only", file=sys.stderr)
        return EXIT_REFUSED
    args = _parse_args(argv)

    import anyio

    from memtomem._instance_registry import acquire_server_lifecycle_barrier
    from memtomem._runtime_paths import ensure_runtime_dir, server_pid_path
    import memtomem.server as server_package
    from memtomem._admission import local_fields
    from memtomem.config import refuse_relative_config_paths
    from memtomem.server import (
        _claim_store_pid_file,
        _install_sigterm_handler,
        _sigterm_deferred,
    )
    from memtomem.server.context import _set_server_transport
    from memtomem.server.lifespan import _load_dotenv, _setup_logging

    _load_dotenv()
    _setup_logging()
    targets: list[Any] = []
    try:
        name = validate_name(args.name)
        refuse_relative_config_paths()
        with _sigterm_deferred():
            _install_sigterm_handler(targets)
            db_path, frozen = _resolve_frozen_store()
            admission = local_fields(tools=list(server_package._TOOL_SURFACE_REQUEST), store=frozen)
            runtime = ensure_runtime_dir()
            check_socket_path(socket_path(runtime, name))
            _lock_lifetime(lifetime_lock_path(runtime, name), targets, process_started)
            pid_file = runtime / server_pid_path(db_path).name
            _claim_store_pid_file(db_path, pid_file, process_started, targets)
        barrier = acquire_server_lifecycle_barrier()
    except DaemonStartError as exc:
        print(f"memtomem shared daemon: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:
        logger.exception("shared daemon could not start")
        print(f"memtomem shared daemon: could not start: {exc}", file=sys.stderr)
        return EXIT_FAILED

    daemon = Daemon(
        name=name,
        idle_timeout_s=args.idle_timeout,
        frozen_store=frozen,
        runtime_dir=runtime,
        sigterm_targets=targets,
        admission=admission,
        barrier=barrier,
    )
    if args.log_to_runtime:
        try:
            _log_to_runtime(log_path(runtime, name))
        except OSError as exc:
            daemon._release_barrier()
            print(f"memtomem shared daemon: cannot open its log: {exc}", file=sys.stderr)
            return EXIT_FAILED
    try:
        with _sigterm_deferred():
            sock = daemon.bind_socket()
    except OSError as exc:
        daemon._release_barrier()
        print(f"memtomem shared daemon: cannot bind {daemon.socket_path}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        # A neutral working directory: no project lives in the runtime
        # directory, so a process-cwd read this code still makes finds nobody's files.
        os.chdir(runtime)
        _set_server_transport("stdio")
        with _published_summary(daemon):
            return anyio.run(daemon.serve, sock)
    except Exception as exc:
        logger.exception("shared daemon %r failed", name)
        print(f"memtomem shared daemon: failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        daemon._release_barrier()
        daemon.unlink_socket_if_ours()


if __name__ == "__main__":
    raise SystemExit(main())
