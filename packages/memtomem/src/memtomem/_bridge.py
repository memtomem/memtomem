"""``memtomem-server --shared NAME``: join a shared daemon over stdio (#3381).

The bridge is a byte pump between an MCP client's stdio and one connection to
the daemon called NAME, starting that daemon when none is running. Before it
connects it computes its admission fields (:mod:`memtomem._admission`); the
daemon admits the session only when they equal its own. Whenever the session
cannot join — a Windows host, a store that cannot be shared, a relative
configured path, a daemon that differs, an older daemon, or none that accepts
in time — :func:`run_bridge` returns the reason and the same process serves the
client as a standalone server, so ``--shared`` never costs a client its
server.

Nothing here imports :mod:`memtomem.server`: a joined session costs the
interpreter, the configuration and this module, not the MCP tool surface.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import socket
import subprocess  # nosec B404 - a fixed argv of this interpreter and module
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

PROTOCOL_VERSION = 2
SPAWN_DEADLINE_S = 10.0
CONNECT_INTERVAL_S = 0.05
REPLY_TIMEOUT_S = 5.0
REPLY_LIMIT = 64 * 1024
HELPER_TIMEOUT_S = 5.0
# After a launcher that overran is killed, how long its exit is waited for.
HELPER_REAP_S = 1.0
# How long a session goes on after the first sign of its end (a side closed,
# or a read or write failed) while the rest is passed on: the daemon's own
# teardown bound.
CONNECTION_DRAIN_S = 10.0
# The most the bridge holds in one direction: what the daemon sent and the
# client has not taken, or what the client sent and the daemon has not. Reading
# one side never waits for the other to take it (the reader's EOF would go
# unseen), so past this the session ends instead of the bridge growing.
BUFFER_LIMIT = 32 * 1024 * 1024
_CHUNK = 64 * 1024


class _Fallback(Exception):
    """The session cannot join; the message is the reason ``mem_status`` shows."""


def run_bridge(name: str) -> str:
    """Serve this process's stdio through the daemon *name*; return only to fall back.

    The return value is the reason the session did not join. Everything up to
    a daemon's acceptance runs under one handler, so whatever goes wrong
    there, foreseen or not, is a reason and never the end of the client's
    server. Once a daemon has accepted, the process ends here with the
    session (:func:`_pump`).
    """
    if os.name == "nt":
        return "shared daemons are POSIX only"
    try:
        fields = _admission_fields()
        sock, rest = _join(name, fields)
    except _Fallback as exc:
        return str(exc)
    except Exception as exc:
        # The type, not the message: a message can quote a configured value.
        return f"joining daemon {name} failed ({type(exc).__name__})"
    _pump(name, sock, rest)


# ── admission fields ─────────────────────────────────────────────────────────


def _admission_fields() -> dict[str, Any]:
    """This process's fields, computed with ``.env`` applied and then undone.

    The environment is put back exactly as it was, so a daemon this bridge
    starts and the standalone server it falls back to both see what a plain
    standalone launch sees, and load ``.env`` themselves.
    """
    from memtomem._admission import local_fields, tool_surface_request
    from memtomem._dotenv import load_dotenv

    # Read before ``.env``, as ``memtomem.server`` reads it at import.
    tools = tool_surface_request(os.environ)
    snapshot = dict(os.environ)
    try:
        load_dotenv()
        store = _shareable_store()
        return local_fields(tools=tools, store=store)
    finally:
        _restore_environ(snapshot)


def _restore_environ(snapshot: dict[str, str]) -> None:
    for key in [k for k in os.environ if k not in snapshot]:
        del os.environ[key]
    for key, value in snapshot.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def _config_error(exc: Exception) -> str:
    """Name what failed, never a value: the reason is logged and shown by ``mem_status``.

    A validation error's text quotes the input it rejected, which can be any
    ``MEMTOMEM_*`` value, so only the field locations and error types are kept.
    """
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        problems = sorted(
            {".".join(str(part) for part in e["loc"]) + f" ({e['type']})" for e in exc.errors()}
        )
        return "the configuration is not valid: " + ", ".join(problems)
    return f"the configuration could not be read ({type(exc).__name__})"


def _shareable_store() -> Path:
    from memtomem._store_identity import (
        StoreNotShareable,
        configured_store_path,
        resolve_shared_store,
    )
    from memtomem.config import relative_config_paths
    from memtomem.config_signature import build_fresh_config

    try:
        config = build_fresh_config(migrate=False, strict_overrides=False)
    except Exception as exc:
        raise _Fallback(_config_error(exc)) from exc
    try:
        relative = relative_config_paths(config)
    except (RuntimeError, ValueError, OSError) as exc:
        # A path that cannot be expanded, such as an unknown ``~user``.
        raise _Fallback(f"a configured path cannot be checked ({type(exc).__name__})") from exc
    if relative:
        raise _Fallback(
            "a shared daemon cannot use a relative configured path: " + ", ".join(relative)
        )
    try:
        _named, frozen = resolve_shared_store(configured_store_path())
    except StoreNotShareable as exc:
        # The summary, not the message: the message quotes the configured value.
        raise _Fallback(exc.summary) from exc
    return frozen


# ── connecting ───────────────────────────────────────────────────────────────


def _join(name: str, fields: dict[str, Any]) -> tuple[socket.socket, bytes]:
    """The connection a daemon accepted, and the bytes that followed its reply.

    Raises :class:`_Fallback` when none accepts before the deadline or one
    answers that the session cannot join.
    """
    from memtomem._daemon_paths import lifetime_lock_path, log_path, socket_path
    from memtomem._runtime_paths import ensure_runtime_dir

    try:
        runtime = ensure_runtime_dir()
    except OSError as exc:
        raise _Fallback(f"the runtime directory is not usable: {exc}") from exc
    sock_path = socket_path(runtime, name)
    lock_path = lifetime_lock_path(runtime, name)
    preamble = (
        json.dumps({"v": PROTOCOL_VERSION, "cwd": os.getcwd(), "admission": fields}).encode()
        + b"\n"
    )
    deadline = time.monotonic() + SPAWN_DEADLINE_S
    spawned = False
    while True:
        # What is left of the deadline bounds the connect and the wait for the
        # reply, so a daemon that neither accepts nor answers cannot hold the
        # client past it. The last round still gets one interval.
        sock = _connect(sock_path, max(deadline - time.monotonic(), CONNECT_INTERVAL_S))
        if sock is None:
            # Each bridge starts at most one daemon; the lifetime lock leaves
            # one running when several start at once.
            if not spawned and not _lock_held(lock_path):
                _spawn(name)
                spawned = True
        else:
            accepted = False
            try:
                left = max(deadline - time.monotonic(), CONNECT_INTERVAL_S)
                reply, rest = _handshake(
                    sock, preamble, time.monotonic() + min(REPLY_TIMEOUT_S, left)
                )
                state = reply.get("state") if reply is not None else None
                if state == "accepted" and reply is not None and reply.get("compared") is True:
                    accepted = True
                    return sock, rest
                if state == "mismatch":
                    differs = reply.get("differs") if reply is not None else None
                    listed = (
                        ", ".join(str(d) for d in differs) if isinstance(differs, list) else "?"
                    )
                    raise _Fallback(f"daemon {name} differs from this session: {listed}")
                if state == "error":
                    error = reply.get("error") if reply is not None else None
                    raise _Fallback(f"daemon {name} refused the session: {error}")
                if state == "accepted":
                    raise _Fallback(f"daemon {name} did not compare the session (an older daemon)")
                # ``retry``, ``refused`` (a daemon draining or exiting), no answer:
                # the next round finds it gone, or its successor.
            finally:
                if not accepted:
                    sock.close()
        if time.monotonic() >= deadline:
            raise _Fallback(
                f"daemon {name} did not accept within {SPAWN_DEADLINE_S:g} s "
                f"(log: {log_path(runtime, name)})"
            )
        time.sleep(CONNECT_INTERVAL_S)


def _connect(path: Path, timeout: float) -> socket.socket | None:
    """A connection to the daemon's socket, or ``None`` when none is to be had yet.

    Not yet: no socket, nobody listening, or a listener that did not take the
    connection within *timeout* (its backlog is full).
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, BlockingIOError):
        sock.close()
        return None
    except OSError as exc:
        sock.close()
        raise _Fallback(f"cannot connect to {path}: {exc}") from exc
    return sock


def _left(deadline: float) -> float:
    """The seconds until *deadline*; a deadline that has passed is a timeout."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _handshake(
    sock: socket.socket, preamble: bytes, deadline: float
) -> tuple[dict[str, Any] | None, bytes]:
    """The daemon's reply and whatever followed it, or ``None`` with no reply by *deadline*.

    The deadline covers the send and all the receives together, so a reply
    that arrives a few bytes at a time does not outlast it.
    """
    buffered = b""
    try:
        sock.settimeout(_left(deadline))
        sock.sendall(preamble)
        while b"\n" not in buffered:
            if len(buffered) > REPLY_LIMIT:
                return None, b""
            sock.settimeout(_left(deadline))
            chunk = sock.recv(_CHUNK)
            if not chunk:
                return None, b""
            buffered += chunk
    except OSError:  # a timeout included
        return None, b""
    line, rest = buffered.split(b"\n", 1)
    if len(line) > REPLY_LIMIT:
        return None, b""
    try:
        reply = json.loads(line)
    except (ValueError, RecursionError):
        return None, b""
    if not isinstance(reply, dict):
        return None, b""
    sock.settimeout(None)
    return reply, rest


def _lock_held(path: Path) -> bool:
    """Whether a daemon holds its lifetime lock; unknown counts as held."""
    import fcntl
    import stat

    try:
        # Without blocking and not through a link: opening a FIFO would wait
        # for its writer, and only a regular file is a lock this can probe.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return True
        # The daemon takes its lock through portalocker, which is ``flock`` on POSIX.
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # releases the probe's lock
    return False


def _spawn(name: str) -> None:
    """Start the daemon through a launcher child, waited for here within its bound."""
    try:
        helper = subprocess.Popen(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-P", "-m", "memtomem._spawn_helper", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise _Fallback(
            f"the launcher of daemon {name} could not be started ({type(exc).__name__})"
        ) from exc
    try:
        code = helper.wait(timeout=HELPER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        helper.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            helper.wait(timeout=HELPER_REAP_S)
        raise _Fallback(
            f"the launcher of daemon {name} did not finish within {HELPER_TIMEOUT_S:g} s"
        ) from None
    if code != 0:
        raise _Fallback(f"the launcher of daemon {name} failed (exit {code})")


# ── the session ──────────────────────────────────────────────────────────────

_terminated = False


def _on_sigterm(signum: int, frame: object) -> None:
    global _terminated
    _terminated = True


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


class _Buffer:
    """Bytes read from one side and not yet written to the other, up to a limit."""

    def __init__(self, limit: int) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._lock = threading.Lock()
        self._held = 0
        self._limit = limit

    def put(self, data: bytes) -> bool:
        """Queue *data*, or return ``False`` and queue nothing when it would pass the limit."""
        with self._lock:
            if self._held + len(data) > self._limit:
                return False
            self._held += len(data)
        self._queue.put(data)
        return True

    def close(self) -> None:
        self._queue.put(None)

    def get(self) -> bytes | None:
        """The next bytes to write, or ``None`` once closed and emptied."""
        return self._queue.get()

    def written(self, count: int) -> None:
        with self._lock:
            self._held -= count


def _fill(read: Callable[[], bytes], buffer: _Buffer, events: queue.Queue[str], eof: str) -> None:
    """Move what *read* returns into *buffer* until its EOF, an error or the limit."""
    try:
        while True:
            data = read()
            if not data:
                # Reported before the buffer is closed. Closing the client's
                # buffer is what shuts the socket's write half, and the daemon's
                # EOF that answers it must find the client's already recorded:
                # the first of the two decides the exit code.
                events.put(eof)
                return
            if not buffer.put(data):
                events.put("overflow")
                return
    except OSError:
        events.put("error")
    finally:
        buffer.close()


def _drain(
    buffer: _Buffer,
    write: Callable[[bytes], object],
    events: queue.Queue[str],
    finish: Callable[[], object],
    failed: str = "error",
) -> None:
    """Write *buffer* out until it is closed and empty, then call *finish*.

    A write that fails is reported as *failed* and nothing is finished.
    """
    try:
        while (data := buffer.get()) is not None:
            write(data)
            buffer.written(len(data))
    except OSError:
        events.put(failed)
    else:
        finish()


def _start_session(sock: socket.socket, rest: bytes, stdin: int, stdout: int) -> queue.Queue[str]:
    """Start copying both ways; the returned queue reports how the session ends.

    Four daemon threads, a reader and a writer for each direction with a
    :class:`_Buffer` between them. A reader never waits for the other side to
    take what it read, so the client's EOF is seen while the daemon is not
    reading and the daemon's while the client is not; a side that leaves more
    than :data:`BUFFER_LIMIT` untaken is reported as ``overflow``.

    Each thread reports one outcome, and :func:`_wait_for_end` decides on
    them. The readers: ``stdin-eof`` from the client, ``socket-eof`` from the
    daemon. The writers, once the reader they follow has ended and all it
    read is written: ``sent`` to the daemon (the write half is shut with it),
    ``flushed`` to the client. A failed write to the client is
    ``stdout-error``, which is how a client that has left looks. Every other
    failed read or write is ``error``: bytes were lost on the way to or from
    the daemon.
    """
    events: queue.Queue[str] = queue.Queue()
    incoming, outgoing = _Buffer(BUFFER_LIMIT), _Buffer(BUFFER_LIMIT)
    if rest:
        outgoing.put(rest)

    def sent() -> None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
        events.put("sent")

    threads: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "from-client",
            lambda: _fill(lambda: os.read(stdin, _CHUNK), incoming, events, "stdin-eof"),
        ),
        ("to-daemon", lambda: _drain(incoming, sock.sendall, events, sent)),
        ("from-daemon", lambda: _fill(lambda: sock.recv(_CHUNK), outgoing, events, "socket-eof")),
        (
            "to-client",
            lambda: _drain(
                outgoing,
                lambda data: _write_all(stdout, data),
                events,
                lambda: events.put("flushed"),
                "stdout-error",
            ),
        ),
    )
    for label, target in threads:
        threading.Thread(target=target, name=f"bridge-{label}", daemon=True).start()
    return events


def _pump(name: str, sock: socket.socket, rest: bytes) -> NoReturn:
    """Serve the session (:func:`_start_session`) until it ends, then end the process.

    The daemon closing first means it is gone: exit 1, no reconnect. stdin
    closing first means the client is gone: the write half is shut once what
    the client sent has been passed on, and the exit is 0 once that is done
    and the daemon's last answers are written, as it is on SIGTERM. A read or
    write that fails on the way to or from the daemon makes the exit 1 as
    well, after what the daemon already sent has been written. Either way the
    wait for the rest is bounded by :data:`CONNECTION_DRAIN_S`, and the
    process ends with ``os._exit``, so a thread blocked on a read or a write
    cannot hold it.
    """
    from memtomem._instance_registry import register_bridge

    signal.signal(signal.SIGTERM, _on_sigterm)
    registration = register_bridge(name)
    code = _wait_for_end(_start_session(sock, rest, 0, 1))
    if registration is not None:
        registration.cleanup()
    with contextlib.suppress(OSError):
        sock.close()
    os._exit(code)


def _wait_for_end(events: queue.Queue[str]) -> int:
    """The exit code: 0 when the client ended the session and nothing was lost, else 1.

    The session's four threads report one outcome each
    (:func:`_start_session`). Of stdin's EOF and the daemon's EOF, the one
    reported first says who ended it. An ``error`` means bytes were lost on
    the way to or from the daemon, so the exit is 1 whoever ended it.

    Nothing is decided while a report still to come could change the code.
    Exit 0 takes all four reports, none of them an ``error``: until the
    writer to the daemon has reported, what the client wrote before it left
    can still fail to reach the daemon. Exit 1 is certain once the daemon
    ended the session or bytes were lost; it then waits only for the writer
    to the client, so that what the daemon already sent is still written.

    From the first report on, the wait lasts at most
    :data:`CONNECTION_DRAIN_S`. A thread that has not reported by then is
    stuck on a side that neither takes nor gives anything, and the code is
    the one the reports so far give. An ``overflow`` ends the session at
    once: bytes were dropped and the side that should take them is not
    taking any.
    """
    seen: set[str] = set()
    first: str | None = None
    deadline: float | None = None
    while True:
        if _terminated:
            return 0
        try:
            event = events.get(timeout=0.1)
        except queue.Empty:
            # Only here: a report already made is read before the deadline
            # counts, however late this thread comes to it.
            if deadline is not None and time.monotonic() >= deadline:
                break
            continue
        if event == "overflow":
            return 1
        seen.add(event)
        if event in ("stdin-eof", "socket-eof"):
            first = first or event
        if deadline is None:
            deadline = time.monotonic() + CONNECTION_DRAIN_S
        if not seen & {"flushed", "stdout-error"}:
            continue  # the writer to the client has not reported
        if "error" in seen or first == "socket-eof":
            break
        if seen >= {"stdin-eof", "sent", "socket-eof"}:
            break
    return 0 if first == "stdin-eof" and "error" not in seen else 1
