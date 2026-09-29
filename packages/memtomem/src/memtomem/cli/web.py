"""memtomem web — launch the Web UI server."""

from __future__ import annotations

import atexit
import contextlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal, cast

import click

from memtomem._process_probe import probe_pid

if TYPE_CHECKING:
    from memtomem._instance_registry import RegistrySnapshot
    from memtomem.cli._liveness import ServerState, StalePidUnlink


_WEB_MODE_CHOICES = ("prod", "dev")
_LOOPBACK_BINDS = {"127.0.0.1", "::1", "localhost"}
_WEB_INFO_NAME = "web.json"
# ``procid`` in ``web.json`` is the instance registry's per-process identity
# (``_instance_registry.current_process_id``): 8 lowercase hex characters.
_PROCID_CHARS = frozenset("0123456789abcdef")
_ResolvedMode = Literal["prod", "dev"]


@dataclass(frozen=True)
class _WebRunConfig:
    host: str
    port: int
    open_browser: bool
    timeout: int
    mode: str | None
    dev_flag: bool
    allow_remote_ui: bool
    trusted_origins: tuple[str, ...]
    trusted_hosts: tuple[str, ...]


@dataclass(frozen=True)
class _WebMetadata:
    pid: int | None = None
    port: int | None = None
    started: str | None = None
    procid: str | None = None


def _missing_web_deps() -> str | None:
    """Return the name of the first missing web-UI dependency, or None if all
    required packages are importable. Kept private so the wizard can reuse it.

    Uses ``importlib.util.find_spec`` so the probe is cheap (no module init
    side-effects) and matches the semantic the wizard's
    ``_collect_missing_extras`` uses — both sites now answer the
    "is the package installed" question the same way (#363 Phase 3,
    eliminates the historical ``__import__`` vs ``find_spec`` split)."""
    from importlib.util import find_spec

    for mod in ("fastapi", "uvicorn"):
        try:
            present = find_spec(mod) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            return mod
    return None


def _web_install_hint() -> str:
    """Return the recommended install command for the `[web]` extra. Used by
    both `mm web` errors and the `mm init` wizard's Next Steps section."""
    return 'uv tool install --reinstall "memtomem[web]"'


def _web_pid_file() -> Path:
    from memtomem._runtime_paths import web_pid_path

    return web_pid_path()


def _default_web_log_path() -> Path:
    return Path.home() / ".memtomem" / "logs" / "web.log"


def _write_web_metadata(
    pid_path: Path, pid_file: object, *, pid: int, port: int, started: str
) -> None:
    payload = f"{pid}\n{port}\n{started}\n"
    pid_file.seek(0)  # type: ignore[attr-defined]
    pid_file.truncate()  # type: ignore[attr-defined]
    pid_file.write(payload)  # type: ignore[attr-defined]
    pid_file.flush()  # type: ignore[attr-defined]
    os.fsync(pid_file.fileno())  # type: ignore[attr-defined]

    from memtomem._instance_registry import current_process_id

    info_file = pid_path.with_name(_WEB_INFO_NAME)
    # ``procid`` ties this pid file to the registry sentinel the lifespan
    # publishes from this same process (#2574), so ``mm upgrade`` and
    # ``mem_status`` can tell the Web UI's registration from a server's
    # without trusting a pid alone. An extra key: older readers ignore it.
    info_payload = {"pid": pid, "port": port, "started": started, "procid": current_process_id()}
    info_file.write_text(json.dumps(info_payload, sort_keys=True) + "\n", encoding="utf-8")


def _read_web_metadata(pid_file: Path | None = None) -> _WebMetadata:
    """Read only the bounded sidecar fallback.

    PID-file metadata comes from the verified liveness probe; reopening the
    pid path here would split the bytes from the lock and identity checks.
    """
    from memtomem.cli._liveness import _read_bounded_regular_file

    try:
        info_file = (pid_file or _web_pid_file()).with_name(_WEB_INFO_NAME)
        raw = _read_bounded_regular_file(info_file)
        if raw is None:
            return _WebMetadata()
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _WebMetadata()
    if not isinstance(data, dict):
        return _WebMetadata()
    pid = data.get("pid")
    port = data.get("port")
    procid = data.get("procid")
    return _WebMetadata(
        pid=pid if isinstance(pid, int) else None,
        port=port if isinstance(port, int) else None,
        started=data.get("started") if isinstance(data.get("started"), str) else None,
        procid=procid if _is_procid(procid) else None,
    )


def _is_procid(value: object) -> bool:
    return isinstance(value, str) and len(value) == 8 and set(value) <= _PROCID_CHARS


def verified_web_identity(state: ServerState) -> tuple[int, str] | None:
    """Return the live Web UI's registry identity ``(pid, procid)``, or ``None``.

    ``state`` is a :func:`~memtomem.cli._liveness.check_web_liveness` result.
    The held ``web.pid`` lock proves a Web UI is running; ``web.json`` beside it
    names the process. When the locked payload was readable its pid must agree
    with the sidecar. Windows cannot read a live locked ``web.pid``
    (``pid=None``), so there the sidecar's pid stands alone.

    This is a claim, not a proof: callers must only honour it when a *live*
    registry sentinel carries the same ``(pid, procid)``. A stale sidecar from
    an exited Web UI names a random procid that no live sentinel has, so
    matching keeps every unproven case attributed to nobody (#2574).
    """
    if not state.alive or state.probe_error is not None or state.pid_file is None:
        return None
    metadata = _read_web_metadata(state.pid_file)
    # ``type() is int``: JSON ``true`` parses as a ``bool``, an ``int`` equal to 1.
    if type(metadata.pid) is not int or metadata.pid <= 0 or metadata.procid is None:
        return None
    if state.pid is not None and state.pid != metadata.pid:
        return None
    return metadata.pid, metadata.procid


def _cleanup_web_files(pid_file: Path, lock_fp: object | None) -> None:
    info_file = pid_file.with_name(_WEB_INFO_NAME)
    # Sidecar first, on every platform, while the lock is still held: once the
    # lock is gone a replacement can lock ``web.pid`` and write its own
    # sidecar, which this exit must not delete (#2574 on POSIX, #2610 on
    # Windows — that sidecar is how the replacement's registration is
    # attributed, and ``mm web`` itself never deletes it).
    with contextlib.suppress(OSError):
        info_file.unlink(missing_ok=True)
    if os.name == "nt":
        # NTFS refuses to delete a file with an open handle, so close first. A
        # replacement that opens ``web.pid`` in between keeps it: the delete
        # then fails with a sharing violation and is ignored.
        if lock_fp is not None:
            with contextlib.suppress(OSError):
                lock_fp.close()  # type: ignore[attr-defined]
        with contextlib.suppress(OSError):
            pid_file.unlink(missing_ok=True)
    else:
        with contextlib.suppress(OSError):
            pid_file.unlink(missing_ok=True)
        if lock_fp is not None:
            with contextlib.suppress(OSError):
                lock_fp.close()  # type: ignore[attr-defined]


@contextlib.contextmanager
def _web_pid_lock(port: int) -> Iterator[None]:
    import portalocker

    from memtomem._runtime_paths import ensure_runtime_dir

    pid_file = ensure_runtime_dir() / "web.pid"
    lock_fp = open(pid_file, "a+")
    try:
        portalocker.lock(lock_fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except (portalocker.LockException, BlockingIOError, OSError) as exc:
        lock_fp.close()
        raise click.ClickException(
            f"memtomem Web UI is already running (pid file: {pid_file})"
        ) from exc

    started = datetime.now(UTC).isoformat()
    _write_web_metadata(pid_file, lock_fp, pid=os.getpid(), port=port, started=started)

    cleaned = False
    old_sigterm = None

    def _cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        _cleanup_web_files(pid_file, lock_fp)

    def _handle_sigterm(_signum: int, _frame: object) -> None:
        _cleanup()
        os._exit(0)

    if os.name != "nt":
        old_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_sigterm)
    atexit.register(_cleanup)

    try:
        yield
    finally:
        if os.name != "nt" and old_sigterm is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, old_sigterm)
        _cleanup()


def _check_web_deps() -> None:
    missing = _missing_web_deps()
    if missing is not None:
        click.secho(
            f"Error: Web UI requires the [web] extra (missing: {missing}).",
            fg="red",
        )
        click.echo(
            "The base install does not include web dependencies."
            " To add them, reinstall with the [web] extra:"
        )
        click.echo(f"  {_web_install_hint()}")
        click.echo('  Or, if using pip: pip install "memtomem[web]"')
        raise SystemExit(1)


def _resolve_mode(mode: str | None, dev_flag: bool) -> _ResolvedMode:
    if mode is not None and dev_flag:
        raise click.UsageError("--mode and --dev are mutually exclusive")

    from memtomem.web.app import WebMode, resolve_web_mode_from_env

    resolved_mode: WebMode
    if dev_flag:
        resolved_mode = "dev"
    elif mode is not None:
        resolved_mode = cast(_ResolvedMode, mode.lower())
    else:
        try:
            resolved_mode = resolve_web_mode_from_env(strict=True)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="MEMTOMEM_WEB__MODE") from exc
    return resolved_mode


def _validate_bind(host: str, allow_remote_ui: bool) -> None:
    bind_is_loopback = host in _LOOPBACK_BINDS
    if bind_is_loopback or allow_remote_ui:
        return
    raise click.UsageError(
        f"--host {host} exposes the Web UI off-loopback. Pass "
        "--allow-remote-ui to acknowledge, paired with --trusted-origin "
        "and --trusted-host so the CSRF/Origin/Host allow-list covers "
        "the remote shape. See https://github.com/memtomem/memtomem/issues/787 ."
    )


def _run_foreground(config: _WebRunConfig) -> None:
    """Launch the memtomem Web UI (FastAPI + SPA)."""
    _check_web_deps()
    resolved_mode = _resolve_mode(config.mode, config.dev_flag)
    _validate_bind(config.host, config.allow_remote_ui)

    import asyncio
    import uvicorn

    from memtomem.web.app import _lifespan, create_app

    click.echo(
        f"Starting memtomem Web UI at http://{config.host}:{config.port} (mode={resolved_mode})"
    )

    async def after_started(
        server: uvicorn.Server,
        serve_task: asyncio.Task[None],
        timeout: float,
    ) -> None:
        if not config.open_browser:
            return

        if timeout == 0:
            click.secho(
                "Warning: No timeout for Web opening (timeout is set to 0).",
                fg="yellow",
            )
            deadline = float("inf")
        else:
            deadline = time.monotonic() + timeout
        while not server.started:
            if serve_task.done():
                return
            if time.monotonic() >= deadline:
                click.secho(
                    "Warning: Web server did not start within the timeout period; not opening browser.",
                    fg="yellow",
                )
                return
            await asyncio.sleep(0.1)
        while not await asyncio.to_thread(_readiness_ok, config.host, config.port):
            if serve_task.done() or time.monotonic() >= deadline:
                click.secho(
                    "Warning: Web backend did not become ready; not opening browser.",
                    fg="yellow",
                )
                return
            await asyncio.sleep(0.1)
        import webbrowser

        webbrowser.open(f"http://{config.host}:{config.port}")

    async def start_server() -> None:
        app_instance = create_app(lifespan=_lifespan, mode=resolved_mode)
        # Push the operator-supplied allow-lists into the app state so
        # ``CSRFGuardMiddleware`` (RFC #787) can read them. Done here
        # rather than via env vars so the CLI surface stays the
        # source-of-truth and the app factory keeps being usable
        # standalone (tests, asgi mounts) with the safe defaults.
        if config.trusted_origins:
            app_instance.state.csrf_trusted_origins = frozenset(config.trusted_origins)
        if config.trusted_hosts:
            app_instance.state.csrf_trusted_hosts = frozenset(config.trusted_hosts)
        web_config = uvicorn.Config(
            app_instance,
            host=config.host,
            port=config.port,
        )
        web_server = uvicorn.Server(web_config)

        serve_task = asyncio.create_task(web_server.serve())
        opener_task = asyncio.create_task(
            after_started(web_server, serve_task, timeout=float(config.timeout))
        )
        await asyncio.gather(serve_task, opener_task)
        if not web_server.started:
            raise click.ClickException("Web UI failed during startup. See the startup log above.")

    with _web_pid_lock(config.port):
        asyncio.run(start_server())


def _connect_host(host: str) -> str:
    if host in {"0.0.0.0", ""}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _pick_free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = "::1" if host == "::" else _connect_host(host)
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((bind_host, 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp(host: str, port: int, *, timeout: float, child: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + timeout
    connect_host = _connect_host(host)
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            with socket.create_connection((connect_host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _readiness_ok(host: str, port: int) -> bool:
    conn = http.client.HTTPConnection(_connect_host(host), port, timeout=0.5)
    try:
        conn.request("GET", "/api/readiness")
        response = conn.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _wait_for_readiness(
    host: str,
    port: int,
    *,
    timeout: float,
    child: subprocess.Popen[bytes],
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        if _readiness_ok(host, port):
            return True
        time.sleep(0.1)
    return False


def _tail_file(path: Path, *, limit: int = 4000) -> str:
    try:
        with path.open("rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - limit), os.SEEK_SET)
            return fp.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _child_argv(config: _WebRunConfig) -> list[str]:
    argv = [
        sys.executable,
        "-c",
        "from memtomem.cli import cli; cli()",
        "web",
        "--_internal-foreground",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--timeout",
        str(config.timeout),
        "--mode",
        _resolve_mode(config.mode, config.dev_flag),
    ]
    if config.allow_remote_ui:
        argv.append("--allow-remote-ui")
    for origin in config.trusted_origins:
        argv.extend(["--trusted-origin", origin])
    for trusted_host in config.trusted_hosts:
        argv.extend(["--trusted-host", trusted_host])
    return argv


def _spawn_background(config: _WebRunConfig, log_file: Path | None) -> None:
    _check_web_deps()
    _validate_bind(config.host, config.allow_remote_ui)

    port = _pick_free_port(config.host) if config.port == 0 else config.port
    child_config = _WebRunConfig(
        host=config.host,
        port=port,
        open_browser=False,
        timeout=config.timeout,
        mode=config.mode,
        dev_flag=config.dev_flag,
        allow_remote_ui=config.allow_remote_ui,
        trusted_origins=config.trusted_origins,
        trusted_hosts=config.trusted_hosts,
    )
    log_path = log_file or _default_web_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, Any]
    if os.name == "nt":
        kwargs = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
            "close_fds": True,
        }
    else:
        kwargs = {"start_new_session": True, "close_fds": True}

    with log_path.open("ab", buffering=0) as log_fp:
        child = subprocess.Popen(
            _child_argv(child_config),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            **kwargs,
        )

    timeout = float(config.timeout if config.timeout > 0 else 30)
    if not _wait_for_readiness(config.host, port, timeout=timeout, child=child):
        tail = _tail_file(log_path)
        if child.poll() is None:
            with contextlib.suppress(OSError):
                child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    child.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=2)
        # The child may have failed *because* another Web UI holds web.pid,
        # so only a file nobody holds is removed (#2610).
        cleanup_note = _cleanup_after_failed_start()
        message = f"Web UI did not start within {timeout:g}s. See log: {log_path}"
        if cleanup_note:
            message = f"{message}\n{cleanup_note}"
        if tail:
            message = f"{message}\n\nLast log output:\n{tail.rstrip()}"
        raise click.ClickException(message)

    click.echo(f"started pid={child.pid} port={port} log={log_path}")
    if config.open_browser:
        import webbrowser

        webbrowser.open(f"http://{config.host}:{port}")


def _wait_for_pid_file_release(timeout: float) -> bool:
    from memtomem.cli._liveness import check_web_liveness

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not check_web_liveness().alive:
            return True
        time.sleep(0.1)
    return not check_web_liveness().alive


def _remove_stale_web_pid_file(pid_file: Path | None = None) -> StalePidUnlink:
    """Delete ``web.pid`` only while holding its lock, and only that file (#2610).

    A liveness probe releases its lock before it returns, so a new ``mm web``
    can lock ``web.pid`` between a "not running" answer and the delete. A
    delete by path would then remove the file the new Web UI holds, and every
    pid-file probe would stop seeing it. ``web.json`` is left alone: its
    contents are used only beside a live ``web.pid`` holder (see
    :func:`verified_web_identity`), and the owner's exit
    (:func:`_cleanup_web_files`) or the next start's
    :func:`_write_web_metadata` replaces it.

    Raises what :func:`~memtomem.cli._liveness.unlink_stale_pid_file` raises.
    """
    from memtomem.cli._liveness import unlink_stale_pid_file

    return unlink_stale_pid_file(pid_file or _web_pid_file())


def _stale_unlink_errors() -> tuple[type[BaseException], ...]:
    from memtomem._instance_registry import BarrierTimeout
    from memtomem.cli._liveness import UnsafeProbePathError

    return (UnsafeProbePathError, BarrierTimeout, OSError)


def _shown(value: object) -> str:
    """Render a path or an exception for the terminal (control characters escaped)."""
    from memtomem._runtime_paths import scrub_text

    return scrub_text(str(value))


def _stale_unlink_failure(pid_file: Path, exc: BaseException) -> str:
    from memtomem._instance_registry import BarrierTimeout

    message = f"Cannot remove the stale pid file at {_shown(pid_file)}: {_shown(exc)}."
    if isinstance(exc, BarrierTimeout):
        message += " `mm uninstall` or `mm reset` is running; retry when it finishes."
    return message


def _cleanup_after_failed_start() -> str | None:
    """Remove a stale ``web.pid`` after a failed background start.

    Returns a line for the error message when something was left in place.
    """
    pid_file = _web_pid_file()
    try:
        outcome = _remove_stale_web_pid_file(pid_file)
    except _stale_unlink_errors() as exc:
        return f"Could not clean up {_shown(pid_file)}: {_shown(exc)}"
    if outcome == "held":
        return f"Another Web UI holds {_shown(pid_file)}; its files were left in place."
    if outcome == "replaced":
        return f"{_shown(pid_file)} changed while cleaning up; it was left in place."
    return None


def _stop_reads_pid_from_sidecar() -> bool:
    """Whether ``mm web stop`` must take the pid from ``web.json``.

    Windows cannot read a locked ``web.pid``, so the probe returns no pid
    there and the sidecar is the only source. POSIX reads the locked file.
    A function, not an inline ``os.name`` check, so both shards test both arms.
    """
    return os.name == "nt"


def _registry_confirms(identity: tuple[int, str], snapshot: RegistrySnapshot) -> bool:
    """Whether a live registry sentinel carries this ``(pid, procid)``."""
    live = {(info.pid, info.procid) for info in (*snapshot.instances, *snapshot.presence)}
    return identity in live


def _sidecar_pid_to_signal(state: ServerState) -> int:
    """Return the sidecar's pid only when the instance registry confirms it.

    ``web.json`` may be a previous Web UI's: a new one locks ``web.pid``
    before it rewrites the sidecar, and ``mm web`` never deletes the sidecar
    by path (#2610). Its pid may since have been reused by another process, so
    it is signalled only when a live registry sentinel carries the same
    ``(pid, procid)``, the proof :func:`verified_web_identity` asks for.
    """
    from memtomem._instance_registry import snapshot_all_instances

    identity = verified_web_identity(state)
    snapshot = snapshot_all_instances()
    if identity is not None and snapshot.complete and _registry_confirms(identity, snapshot):
        return identity[0]
    if not snapshot.complete:
        reason = "the instance registry could not be read completely"
    else:
        reason = "its pid has no live registration yet (it may be starting)"
    raise click.ClickException(
        f"Web UI holds its pid file, but {reason}. No signal was sent; retry in a "
        "moment, or stop it through your operating system's process tools."
    )


def _web_status() -> None:
    from memtomem.cli._liveness import check_web_liveness

    state = check_web_liveness()
    metadata = _read_web_metadata(state.pid_file)
    pid = state.pid if state.pid is not None else metadata.pid
    port = state.port if state.port is not None else metadata.port
    started = state.started if state.started is not None else metadata.started
    if state.alive:
        status = "running"
        if state.probe_error is not None:
            status += f" (unverified: {state.probe_error})"
        click.echo(
            f"{status}  pid={pid if pid is not None else '?'}  "
            f"port={port if port is not None else '?'}  "
            f"started={started if started is not None else '?'}"
        )
        raise SystemExit(0)
    if state.pid_file is not None:
        click.echo(f"stopped  (stale pid file at {state.pid_file})")
        raise SystemExit(3)
    click.echo("stopped")
    raise SystemExit(3)


def _web_stop() -> None:
    from memtomem.cli._liveness import check_web_liveness

    state = check_web_liveness()
    if state.probe_error is not None:
        from memtomem._runtime_paths import scrub_text

        tracked_path = state.pid_file if state.pid_file is not None else _web_pid_file()
        raise click.ClickException(
            f"Cannot verify whether the Web UI is running: {state.probe_error}\n"
            "No signal was sent. Stop the Web UI through its service manager "
            "or your operating system's process tools, then repair or remove "
            f"the unsafe runtime/pid entry at {scrub_text(str(tracked_path))} "
            "and retry."
        )
    metadata = _read_web_metadata(state.pid_file)
    pid = state.pid if state.pid is not None else metadata.pid
    if not state.alive:
        if state.pid_file is not None:
            _stop_remove_stale_pid_file(state.pid_file)
        click.echo("not running")
        raise SystemExit(0)
    if pid is None:
        raise click.ClickException(
            f"Web UI appears to be running, but the pid is unreadable. Inspect {_web_pid_file()}."
        )
    if state.pid is None and not _stop_reads_pid_from_sidecar():
        # POSIX reads the pid from the locked file itself. A held lock with no
        # pid in it is a Web UI that has locked but not yet written its pid, and
        # ``web.json`` may still be a killed predecessor's (#2587): its pid
        # could now be anyone's, so do not signal it.
        raise click.ClickException(
            "Web UI holds its pid file but has not recorded its pid yet (it may be "
            "starting). No signal was sent; retry in a moment."
        )
    if state.pid is None:
        # Windows cannot read a locked pid file, so the pid comes from the
        # sidecar, which may be a predecessor's: require registry proof.
        pid = _sidecar_pid_to_signal(state)

    if os.name == "nt":
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except OSError:
            pass
        if not _wait_for_pid_file_release(10):
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], check=False)
            _wait_for_pid_file_release(2)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise click.ClickException(f"cannot signal pid {pid}: {exc}") from exc
        # ``== "alive"``, not ``!= "dead"``: this gates a SIGKILL, and the
        # probe's third answer means "could not tell". Escalating on it would
        # signal a pid we have no evidence is still the process we started —
        # and on a recycled pid, someone else's (#2234). Failing to kill is
        # reported honestly by the liveness re-check below.
        if not _wait_for_pid_file_release(10) and probe_pid(pid) == "alive":
            os.kill(pid, signal.SIGKILL)
            _wait_for_pid_file_release(2)

    final_state = check_web_liveness()
    if not final_state.alive:
        click.echo(f"stopped pid={pid}")
        # The stop itself succeeded; a cleanup that could not run is a warning.
        if state.pid_file is not None:
            try:
                outcome = _remove_stale_web_pid_file(state.pid_file)
            except _stale_unlink_errors() as exc:
                click.echo(_stale_unlink_failure(state.pid_file, exc), err=True)
            else:
                if outcome == "held":
                    click.echo(
                        f"A new Web UI has since locked {_shown(state.pid_file)}; left in place.",
                        err=True,
                    )
                elif outcome == "replaced":
                    click.echo(
                        f"{_shown(state.pid_file)} changed after the stop; left in place.", err=True
                    )
        return
    raise click.ClickException(f"failed to stop pid {pid}")


def _stop_remove_stale_pid_file(pid_file: Path) -> None:
    """``mm web stop`` found no holder: remove the stale ``web.pid`` or explain.

    Exits 2 after removing it and 0 when it was already gone. Fails without
    removing anything when a Web UI locked it after the probe (``held``) or
    the path changed (``replaced``), or when the removal cannot run.
    """
    try:
        outcome = _remove_stale_web_pid_file(pid_file)
    except _stale_unlink_errors() as exc:
        raise click.ClickException(
            f"{_stale_unlink_failure(pid_file, exc)} Nothing was removed."
        ) from exc
    if outcome == "removed":
        click.echo("stopped  (removed stale pid file)")
        raise SystemExit(2)
    if outcome == "held":
        raise click.ClickException(
            f"A Web UI locked {_shown(pid_file)} after the liveness check (it may be starting). "
            "Nothing was removed; run `mm web stop` again to stop it."
        )
    if outcome == "replaced":
        raise click.ClickException(
            f"The pid file at {_shown(pid_file)} changed after the liveness check. Nothing was "
            "removed; run `mm web status` and retry."
        )


@click.group("web", invoke_without_command=True)
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to bind to")
@click.option(
    "--open", "open_browser", is_flag=True, help="Open the Web UI in your browser after startup."
)
@click.option(
    "--timeout", default=30, type=int, help="Timeout for web opening (seconds). Zero is no timeout."
)
@click.option(
    "--mode",
    type=click.Choice(_WEB_MODE_CHOICES, case_sensitive=False),
    default=None,
    help="UI surface to expose. 'prod' (default) shows the polished page set; "
    "'dev' adds opt-in maintainer pages. Overrides MEMTOMEM_WEB__MODE.",
)
@click.option(
    "--dev",
    "dev_flag",
    is_flag=True,
    help="Shortcut for --mode dev. Mutually exclusive with --mode.",
)
@click.option(
    "--allow-remote-ui",
    is_flag=True,
    help="Acknowledge that --host is exposing the Web UI off-loopback. "
    "Required when --host is non-loopback (RFC #787) — startup refuses "
    "otherwise. Pair with --trusted-origin / --trusted-host so the CSRF / "
    "Origin / Host allow-list has explicit entries for the remote shape.",
)
@click.option(
    "--trusted-origin",
    "trusted_origins",
    multiple=True,
    metavar="HOST",
    help="Add a hostname to the CSRF Origin/Referer allow-list. Loopback "
    "(127.0.0.1, ::1, localhost) is always trusted; anything else has to be "
    "named explicitly. Repeat the flag for multiple hosts.",
)
@click.option(
    "--trusted-host",
    "trusted_hosts",
    multiple=True,
    metavar="HOST",
    help="Add a hostname to the CSRF Host-header allow-list. Defends DNS "
    "rebinding when running with --allow-remote-ui. Loopback is always "
    "trusted. Repeat for multiple hosts.",
)
@click.option("-b", "--background", is_flag=True, help="Run the Web UI in the background.")
@click.option(
    "--log-file",
    type=click.Path(path_type=Path, dir_okay=False, writable=True),
    default=None,
    help="Log file for --background. Defaults to ~/.memtomem/logs/web.log.",
)
@click.option("--_internal-foreground", "internal_foreground", is_flag=True, hidden=True)
@click.pass_context
def web(
    ctx: click.Context,
    host: str,
    port: int,
    open_browser: bool,
    timeout: int,
    mode: str | None,
    dev_flag: bool,
    allow_remote_ui: bool,
    trusted_origins: tuple[str, ...],
    trusted_hosts: tuple[str, ...],
    background: bool,
    log_file: Path | None,
    internal_foreground: bool,
) -> None:
    """Launch and manage the memtomem Web UI (FastAPI + SPA)."""
    if ctx.invoked_subcommand is not None:
        return
    if background and internal_foreground:
        raise click.UsageError("--background and --_internal-foreground are mutually exclusive")
    if log_file is not None and not background:
        raise click.UsageError("--log-file requires --background")

    config = _WebRunConfig(
        host=host,
        port=port,
        open_browser=open_browser,
        timeout=timeout,
        mode=mode,
        dev_flag=dev_flag,
        allow_remote_ui=allow_remote_ui,
        trusted_origins=trusted_origins,
        trusted_hosts=trusted_hosts,
    )
    if background:
        _spawn_background(config, log_file)
    else:
        _run_foreground(config)


@web.command("status")
def web_status() -> None:
    """Show whether the Web UI daemon is running."""
    _web_status()


@web.command("stop")
def web_stop() -> None:
    """Stop a background or foreground Web UI process tracked by pid file."""
    _web_stop()
