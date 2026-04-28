"""CLI: ``mm upgrade`` — stop the running server, then reinstall.

``uv tool install --reinstall memtomem`` only replaces the on-disk bytes;
any ``memtomem-server`` process already imported by an MCP client keeps
running the old code until it exits. That split-brain is exactly what
caused the v0.1.25 → v0.1.26 stale ``.server.pid`` repro that motivated
issue #443. ``mm upgrade`` wraps the reinstall with process-level hygiene:

    probe live server → SIGTERM (escalate to SIGKILL after grace) →
    unlink stale pid file → ``uv tool install --refresh --reinstall``.

There is no ``--skip-pkill``: the kill-then-reinstall ordering is the
whole reason this command exists. On Windows the kill stage is skipped
automatically (POSIX advisory flock + signals are unavailable) and the
user is told to stop the server manually if they observe a split-brain.
"""

from __future__ import annotations

import json as _json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from memtomem.cli._liveness import ServerState, check_server_liveness


def _isatty() -> bool:
    """CliRunner seam (mirrors ``uninstall_cmd._isatty``)."""
    return sys.stdin.isatty()


def _format_path(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return s.replace(home, "~", 1) if s.startswith(home) else s


def _pid_alive(pid: int) -> bool:
    """POSIX liveness check via ``os.kill(pid, 0)``.

    Unix-only; callers gate on ``sys.platform != "win32"``.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it — for our purposes "alive".
        return True
    return True


def _stop_server(state: ServerState, grace: float) -> tuple[list[int], list[Path]]:
    """SIGTERM the live server, escalate to SIGKILL after ``grace`` seconds.

    Returns ``(killed_pids, removed_pid_files)``. Caller is responsible
    for skipping this on Windows / when ``state.alive`` is False.
    """
    killed: list[int] = []
    removed: list[Path] = []

    pid = state.pid
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            # Already gone between probe and kill.
            pid = None
        except PermissionError as exc:
            raise click.ClickException(
                f"cannot signal pid {pid}: {exc}. Stop the server manually and retry."
            ) from exc

        # Poll for exit. server's ``_install_sigterm_handler`` (#439)
        # unlinks its own pid file on a clean SIGTERM, so the file may
        # vanish before grace expires — that's fine.
        deadline = time.monotonic() + grace
        while pid is not None and time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        else:
            if pid is not None and _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Brief settle so the kernel actually reaps it before we
                # try to unlink the lock file.
                time.sleep(0.5)

    # Unconditionally clean stale pid file. Clean SIGTERM teardown usually
    # removes it itself, but the SIGKILL path leaves it behind, and an
    # empty/locked file blocks a fresh post-upgrade server.
    if state.pid_file is not None:
        try:
            state.pid_file.unlink(missing_ok=True)
            removed.append(state.pid_file)
        except OSError as exc:
            raise click.ClickException(
                f"failed to remove stale pid file {state.pid_file}: {exc}"
            ) from exc

    return killed, removed


def _build_install_cmd(version: str | None) -> list[str]:
    pkg = "memtomem" if not version else f"memtomem=={version}"
    # ``--refresh`` invalidates uv's cached PyPI index so a freshly
    # released version isn't masked by the cached resolver result
    # (memo: feedback_uv_index_cache_lag.md).
    return ["uv", "tool", "install", "--refresh", "--reinstall", pkg]


@click.command("upgrade")
@click.option(
    "--version",
    "version",
    default=None,
    metavar="X.Y.Z",
    help="Pin a specific version. Default: latest on the configured index.",
)
@click.option(
    "--grace",
    type=click.FloatRange(min=0.0),
    default=5.0,
    show_default=True,
    help="Seconds to wait after SIGTERM before escalating to SIGKILL.",
)
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "json_out", is_flag=True, help="Emit a structured JSON result.")
@click.option(
    "--dry-run", is_flag=True, help="Print the plan and exit without killing or installing."
)
def upgrade(
    version: str | None,
    grace: float,
    yes: bool,
    json_out: bool,
    dry_run: bool,
) -> None:
    """Stop a running memtomem-server, then reinstall via ``uv tool``.

    The canonical ``uv tool install --reinstall memtomem`` only swaps the
    on-disk bytes; any server already imported by an MCP client keeps
    running the previous version. ``mm upgrade`` adds the missing
    process-level hygiene step around it.
    """
    is_windows = sys.platform == "win32"
    state = check_server_liveness()
    install_cmd = _build_install_cmd(version)
    pkg_target = install_cmd[-1]

    # ----- plan -----
    if not json_out:
        click.echo("memtomem upgrade plan:")
        if is_windows:
            click.secho(
                "  Detected Windows; skipping process termination. "
                "Stop the server manually before rerunning if you see a "
                "split-brain after upgrade.",
                fg="yellow",
            )
        elif state.alive:
            pid_repr = state.pid if state.pid is not None else "?"
            pid_file_repr = _format_path(state.pid_file) if state.pid_file else "?"
            click.echo(f"  Stop running server (pid {pid_repr}, lock {pid_file_repr})")
            click.echo(f"  Wait up to {grace:g}s for graceful exit, then SIGKILL")
            click.echo(f"  Remove stale {pid_file_repr}")
        else:
            click.echo("  No running server detected — reinstall only")
        click.echo(f"  Reinstall: {' '.join(install_cmd)}")

    if dry_run:
        if json_out:
            click.echo(
                _json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "would_kill": [state.pid] if (state.alive and state.pid) else [],
                        "would_remove": (
                            [str(state.pid_file)] if (state.alive and state.pid_file) else []
                        ),
                        "would_install": install_cmd,
                        "version": version,
                    }
                )
            )
        return

    # ----- confirm -----
    if not yes:
        if not _isatty():
            msg = "Refusing to upgrade without confirmation in a non-interactive shell. Pass -y."
            if json_out:
                click.echo(_json.dumps({"ok": False, "error": msg}))
                sys.exit(1)
            click.secho(msg, fg="red")
            raise click.Abort()
        if not click.confirm("\nProceed with upgrade?", default=True):
            click.echo("Cancelled — nothing was changed.")
            sys.exit(1)

    # ----- stop -----
    killed: list[int] = []
    removed: list[Path] = []
    if state.alive and not is_windows:
        try:
            killed, removed = _stop_server(state, grace=grace)
        except click.ClickException as exc:
            if json_out:
                click.echo(_json.dumps({"ok": False, "error": str(exc)}))
                sys.exit(1)
            raise

    # ----- reinstall -----
    try:
        result = subprocess.run(install_cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        msg = "`uv` not found on PATH. Install uv (https://docs.astral.sh/uv/) and retry."
        if json_out:
            click.echo(_json.dumps({"ok": False, "error": msg, "killed": killed}))
            sys.exit(1)
        click.secho(msg, fg="red")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        msg = "uv tool install timed out after 600s."
        if json_out:
            click.echo(_json.dumps({"ok": False, "error": msg, "killed": killed}))
            sys.exit(1)
        click.secho(msg, fg="red")
        sys.exit(1)

    if result.returncode != 0:
        if json_out:
            click.echo(
                _json.dumps(
                    {
                        "ok": False,
                        "error": f"uv tool install failed (rc={result.returncode})",
                        "stderr": result.stderr,
                        "killed": killed,
                        "removed": [str(p) for p in removed],
                    }
                )
            )
            sys.exit(1)
        click.secho(f"\nuv tool install failed (rc={result.returncode}):", fg="red")
        click.echo(result.stderr.rstrip())
        sys.exit(1)

    # ----- success -----
    if json_out:
        click.echo(
            _json.dumps(
                {
                    "ok": True,
                    "killed": killed,
                    "removed": [str(p) for p in removed],
                    "reinstalled": pkg_target,
                    "version": version,
                }
            )
        )
        return

    if killed:
        click.secho(f"\nStopped pid {killed[0]}.", fg="green")
    if removed:
        for path in removed:
            click.echo(f"Removed {_format_path(path)}.")
    click.secho(f"Reinstalled {pkg_target}.", fg="green")
