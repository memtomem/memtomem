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
import re
import time
import tomllib
from pathlib import Path

import click

from memtomem.cli._liveness import ServerState, check_server_liveness, probe_pid_file

# Bare PEP 440 release identifier — no operators, no whitespace. We pin
# with ``memtomem==<version>``, so accepting a specifier like ``>=0.1.30``
# would compose to ``memtomem==>=0.1.30`` and uv would reject it with a
# less obvious parser error. Pre/post/dev releases (``0.1.30rc1``,
# ``0.1.30.post1``, ``0.1.30.dev0``, ``0.1.30+local``) stay allowed.
_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*"  # release segment: 1, 1.2, 1.2.3, ...
    r"(?:(?:a|b|rc)[0-9]+)?"  # pre-release: a1, b2, rc3
    r"(?:\.post[0-9]+)?"  # post-release
    r"(?:\.dev[0-9]+)?"  # dev release
    r"(?:\+[a-z0-9]+(?:[._-][a-z0-9]+)*)?"  # local version segment
    r"$",
    re.IGNORECASE,
)


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
        if pid is not None and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Brief settle so the kernel actually reaps it before we
            # try to unlink the lock file.
            time.sleep(0.5)

    # Clean the stale pid file. Clean SIGTERM teardown usually removes it
    # itself, but the SIGKILL path leaves it behind. Re-probe immediately
    # before unlink so we don't accidentally delete a fresh lockfile that
    # an MCP client just respawned at the same path during the SIGKILL
    # settle window.
    if state.pid_file is not None:
        recheck = probe_pid_file(state.pid_file)
        if recheck.alive:
            click.secho(
                f"  Skipping pid-file unlink — {state.pid_file} is now held by a "
                "freshly started writer (likely an auto-restart from your MCP "
                "client). Leaving it alone.",
                fg="yellow",
            )
        else:
            try:
                state.pid_file.unlink(missing_ok=True)
                removed.append(state.pid_file)
            except OSError as exc:
                raise click.ClickException(
                    f"failed to remove stale pid file {state.pid_file}: {exc}"
                ) from exc

    return killed, removed


def _detect_installed_extras() -> list[str]:
    """Best-effort: read uv's tool receipt to preserve extras on reinstall.

    ``uv tool install 'memtomem[all]'`` records the install spec in
    ``<uv tool dir>/memtomem/uv-receipt.toml`` as
    ``[tool].requirements = [{ name = "memtomem", extras = ["all"] }]``.
    Without re-passing the same extras, ``uv tool install --reinstall
    memtomem`` would silently fall back to the bare BM25-only install,
    dropping ONNX dense embeddings, the Web UI, etc. (review feedback).

    Returns ``[]`` on any failure (uv unavailable, receipt missing or
    malformed) — callers fall back to no extras and can override with
    ``--extras``.
    """
    try:
        result = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        tools_dir = Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return []

    receipt = tools_dir / "memtomem" / "uv-receipt.toml"
    if not receipt.exists():
        return []
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    for req in data.get("tool", {}).get("requirements", []):
        if req.get("name") == "memtomem":
            extras = req.get("extras") or []
            return [str(e) for e in extras]
    return []


def _build_install_cmd(version: str | None, extras: list[str]) -> list[str]:
    pkg = "memtomem"
    if extras:
        pkg = f"memtomem[{','.join(extras)}]"
    if version:
        if not _VERSION_PATTERN.match(version):
            raise click.BadParameter(
                f"{version!r} is not a bare PEP 440 release (e.g. 0.1.30, 0.1.30rc1). "
                "Pass a literal version, not a specifier like '>=0.1.30'.",
                param_hint="--version",
            )
        pkg = f"{pkg}=={version}"
    # ``--refresh`` invalidates uv's cached PyPI index so a freshly
    # released version isn't masked by the cached resolver result
    # (memo: feedback_uv_index_cache_lag.md).
    return ["uv", "tool", "install", "--refresh", "--reinstall", pkg]


def _resolve_extras(extras_flag: str | None) -> tuple[list[str], bool]:
    """Resolve ``--extras`` value to a concrete list + ``auto_detected`` flag.

    ``None`` (flag omitted) → auto-detect from receipt.
    ``"none"`` / empty → explicit bare install.
    Anything else → split on ``,`` and strip.
    """
    if extras_flag is None:
        return _detect_installed_extras(), True
    cleaned = extras_flag.strip().lower()
    if cleaned in ("", "none"):
        return [], False
    return [e.strip() for e in extras_flag.split(",") if e.strip()], False


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
@click.option(
    "--extras",
    "extras_flag",
    default=None,
    metavar="LIST",
    help=(
        "Extras to install (e.g. 'all' or 'onnx,web'). "
        "Default: auto-detect from the current uv-tool install so a "
        "memtomem[all] user keeps [all]. Pass 'none' for a bare install."
    ),
)
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--json", "json_out", is_flag=True, help="Emit a structured JSON result.")
@click.option(
    "--dry-run", is_flag=True, help="Print the plan and exit without killing or installing."
)
def upgrade(
    version: str | None,
    grace: float,
    extras_flag: str | None,
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
    extras, extras_auto = _resolve_extras(extras_flag)
    install_cmd = _build_install_cmd(version, extras)
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
        if extras:
            source = "auto-detected from uv tool receipt" if extras_auto else "from --extras"
            click.echo(f"  Extras: [{','.join(extras)}] ({source})")
        elif extras_auto:
            click.echo("  Extras: none detected (bare install)")
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
                        "extras": extras,
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
            # Voluntary cancel → exit 0; keep JSON schema consistent.
            if json_out:
                click.echo(_json.dumps({"ok": True, "cancelled": True}))
            else:
                click.echo("Cancelled — nothing was changed.")
            return

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
                    "extras": extras,
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
