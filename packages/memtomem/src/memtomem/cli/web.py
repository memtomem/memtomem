"""memtomem web — launch the Web UI server."""

from __future__ import annotations

import atexit
import contextlib
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
from typing import Any, Iterator, Literal, cast

import click


_WEB_MODE_CHOICES = ("prod", "dev")
_LOOPBACK_BINDS = {"127.0.0.1", "::1", "localhost"}
_WEB_INFO_NAME = "web.json"
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


def _web_info_file() -> Path:
    return _web_pid_file().with_name(_WEB_INFO_NAME)


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

    info_file = pid_path.with_name(_WEB_INFO_NAME)
    info_payload = {"pid": pid, "port": port, "started": started}
    info_file.write_text(json.dumps(info_payload, sort_keys=True) + "\n", encoding="utf-8")


def _read_web_metadata() -> _WebMetadata:
    from memtomem.cli._liveness import _parse_pid_payload

    pid_file = _web_pid_file()
    try:
        pid, port, started = _parse_pid_payload(pid_file.read_text(encoding="utf-8"))
        if pid is not None or port is not None or started is not None:
            return _WebMetadata(pid=pid, port=port, started=started)
    except OSError:
        pass

    try:
        data = json.loads(_web_info_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _WebMetadata()
    pid = data.get("pid")
    port = data.get("port")
    return _WebMetadata(
        pid=pid if isinstance(pid, int) else None,
        port=port if isinstance(port, int) else None,
        started=data.get("started") if isinstance(data.get("started"), str) else None,
    )


def _cleanup_web_files(pid_file: Path, lock_fp: object | None) -> None:
    info_file = pid_file.with_name(_WEB_INFO_NAME)
    if os.name == "nt":
        if lock_fp is not None:
            with contextlib.suppress(OSError):
                lock_fp.close()  # type: ignore[attr-defined]
        for path in (pid_file, info_file):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
    else:
        for path in (pid_file, info_file):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
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

    async def after_started(server: uvicorn.Server, timeout: float) -> None:
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
            if time.monotonic() >= deadline:
                click.secho(
                    "Warning: Web server did not start within the timeout period; not opening browser.",
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

        await asyncio.gather(
            web_server.serve(),
            after_started(web_server, timeout=float(config.timeout)),
        )

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
    if not _wait_for_tcp(config.host, port, timeout=timeout, child=child):
        tail = _tail_file(log_path)
        if child.poll() is None:
            with contextlib.suppress(OSError):
                child.terminate()
        message = f"Web UI did not start within {timeout:g}s. See log: {log_path}"
        if tail:
            message = f"{message}\n\nLast log output:\n{tail.rstrip()}"
        raise click.ClickException(message)

    click.echo(f"started pid={child.pid} port={port} log={log_path}")
    if config.open_browser:
        import webbrowser

        webbrowser.open(f"http://{config.host}:{port}")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
        if not process:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_file_release(timeout: float) -> bool:
    from memtomem.cli._liveness import probe_pid_file

    deadline = time.monotonic() + timeout
    pid_file = _web_pid_file()
    while time.monotonic() < deadline:
        if not probe_pid_file(pid_file).alive:
            return True
        time.sleep(0.1)
    return not probe_pid_file(pid_file).alive


def _remove_stale_web_files() -> None:
    for path in (_web_pid_file(), _web_info_file()):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _web_status() -> None:
    from memtomem.cli._liveness import probe_pid_file

    state = probe_pid_file(_web_pid_file())
    metadata = _read_web_metadata()
    pid = state.pid if state.pid is not None else metadata.pid
    port = state.port if state.port is not None else metadata.port
    started = state.started if state.started is not None else metadata.started
    if state.alive:
        click.echo(
            f"running  pid={pid if pid is not None else '?'}  "
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
    from memtomem.cli._liveness import probe_pid_file

    state = probe_pid_file(_web_pid_file())
    metadata = _read_web_metadata()
    pid = state.pid if state.pid is not None else metadata.pid
    if not state.alive:
        if state.pid_file is not None:
            _remove_stale_web_files()
            click.echo("stopped  (removed stale pid file)")
            raise SystemExit(2)
        with contextlib.suppress(OSError):
            _web_info_file().unlink(missing_ok=True)
        click.echo("not running")
        raise SystemExit(0)
    if pid is None:
        raise click.ClickException(
            f"Web UI appears to be running, but the pid is unreadable. Inspect {_web_pid_file()}."
        )

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
        if not _wait_for_pid_file_release(10) and _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
            _wait_for_pid_file_release(2)

    if not probe_pid_file(_web_pid_file()).alive:
        _remove_stale_web_files()
        click.echo(f"stopped pid={pid}")
        return
    raise click.ClickException(f"failed to stop pid {pid}")


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
