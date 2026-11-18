"""``mm daemon status`` / ``mm daemon stop``: shared daemons (#3381).

A shared daemon is found by its lifetime lock, ``server-daemon-<name>.pid`` in
the runtime directory, which it holds for its whole life. ``status`` reports
each one; ``stop`` stops a live one the way ``mm upgrade`` stops a server
(SIGTERM, then SIGKILL only on positive evidence the process is still alive)
and is confirmed by the lifetime lock being released, never by a reply.

A SIGTERM skips the daemon's asynchronous teardown (session drain, store close,
WAL checkpoint), as it does for a standalone server: SQLite recovers the WAL
on the next open. Stopping a daemon ends every session connected to it.
"""

from __future__ import annotations

import json as _json
import os
import sys

import click

from memtomem.cli._liveness import DaemonState, ServerState

_STATUS_RUNNING = 0
_STATUS_NONE = 3


def _validated_name(name: str | None) -> str | None:
    from memtomem._daemon_paths import NAME_PATTERN

    if name is not None and not NAME_PATTERN.fullmatch(name):
        raise click.BadParameter(
            "use 1 to 32 characters from a-z, 0-9 and '-', starting with a letter or digit",
            param_hint="NAME",
        )
    return name


def _select(name: str | None) -> tuple[list[DaemonState], tuple[str, ...]]:
    """``(daemons NAME selects, warnings)``; all daemons when NAME is omitted. Fails closed."""
    from memtomem.cli._liveness import enumerate_daemons

    inventory = enumerate_daemons()
    if inventory.error is not None:
        raise click.ClickException(
            f"Cannot list shared daemons: {inventory.error}. No daemon was signalled or reported."
        )
    selected = [d for d in inventory.daemons if name is None or d.name == name]
    return selected, inventory.warnings


def _echo_warnings(warnings: tuple[str, ...]) -> None:
    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)


def _status_word(state: ServerState) -> str:
    if not state.alive:
        return "stale"
    return "unverified" if state.probe_error is not None else "running"


def _row(daemon: DaemonState) -> dict[str, object]:
    state = daemon.state
    row: dict[str, object] = {
        "name": daemon.name,
        "status": _status_word(state),
        "pid": state.pid,
        "started": state.started,
        "pid_file": str(state.pid_file) if state.pid_file is not None else None,
        "socket": os.path.lexists(daemon.socket_path),
    }
    if state.probe_error is not None:
        row["probe_error"] = state.probe_error
    return row


@click.group("daemon")
def daemon() -> None:
    """Report and stop shared daemons (one engine serving several client sessions)."""


@daemon.command("status")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print one JSON document.")
def status(name: str | None, as_json: bool) -> None:
    """Show shared daemons, or the one named NAME.

    Exit status: 0 when a selected daemon is running (or cannot be verified),
    3 when none is, 1 when the daemons cannot be listed.
    """
    name = _validated_name(name)
    try:
        selected, warnings = _select(name)
    except click.ClickException as exc:
        if as_json:
            click.echo(_json.dumps({"error": exc.message}))
            sys.exit(1)
        raise
    running = any(d.state.alive for d in selected)
    if as_json:
        rows = [_row(d) for d in selected]
        payload: dict[str, object] = {"daemons": rows, "count": len(rows)}
        if warnings:
            payload["warnings"] = list(warnings)
        click.echo(_json.dumps(payload))
        sys.exit(_STATUS_RUNNING if running else _STATUS_NONE)
    _echo_warnings(warnings)
    if not selected:
        click.echo(f"no shared daemon named {name!r}" if name else "no shared daemon")
    else:
        for d in selected:
            if d.state.alive:
                word = "running"
                if d.state.probe_error is not None:
                    word = f"running (unverified: {d.state.probe_error})"
                pid = d.state.pid if d.state.pid is not None else "?"
                started = d.state.started or "?"
                click.echo(f"{d.name}  {word}  pid={pid}  started={started}")
            else:
                click.echo(f"{d.name}  stopped  (stale pid file at {d.state.pid_file})")
    sys.exit(_STATUS_RUNNING if running else _STATUS_NONE)


def _store_pid_states(pid: int) -> list[ServerState]:
    """The daemon's other lock files: every live server pid file that records *pid*."""
    from memtomem.cli._liveness import enumerate_server_liveness_inventory
    from memtomem.cli.upgrade_cmd import _upgrade_server_stops

    states, _warning = enumerate_server_liveness_inventory()
    # The same filter ``mm upgrade`` applies: a shared legacy alias is not a process.
    return [s for s in _upgrade_server_stops(states) if s.probe_error is None and s.pid == pid]


def _stop_one(daemon: DaemonState, grace: float) -> str | None:
    """Stop one live daemon; ``None`` on success, else what went wrong."""
    from memtomem.cli.upgrade_cmd import (
        _group_generation_problem,
        _retire_pid_file,
        _terminate_pid,
    )

    state = daemon.state
    if state.probe_error is not None:
        return (
            f"cannot verify daemon {daemon.name!r}: {state.probe_error}. No signal was sent; "
            "stop it through your operating system's process tools."
        )
    pid = state.pid
    if pid is None:
        return (
            f"daemon {daemon.name!r} holds its lifetime lock but has not recorded its pid yet "
            "(it may be starting). No signal was sent; retry in a moment."
        )
    lifetime_path = state.pid_file
    group = [state, *(s for s in _store_pid_states(pid) if s.pid_file != lifetime_path)]
    problem = _group_generation_problem(pid, group)
    if problem is not None:
        return problem
    _killed, error = _terminate_pid(pid, grace)
    if error is not None:
        return error
    errors = []
    for member in group:
        _removed, recheck, error = _retire_pid_file(member, pid, warnings_to_stderr=True)
        if error is not None:
            errors.append(error)
        elif member is state and recheck.alive:
            errors.append(
                f"daemon {daemon.name!r}: {lifetime_path} is held by pid {recheck.pid} "
                "after the stop"
            )
    return "; ".join(errors) or None


@daemon.command("stop")
@click.argument("name", required=False)
@click.option(
    "--grace",
    type=click.FloatRange(min=0.0),
    default=5.0,
    show_default=True,
    help="Seconds to wait after SIGTERM before SIGKILL.",
)
def stop(name: str | None, grace: float) -> None:
    """Stop the shared daemon NAME, or every running one.

    Every session connected to a stopped daemon ends (its server exits 1); the
    client may need a reconnect or a restart. Exit status: 0 when nothing selected
    is left running, 1 otherwise.
    """
    name = _validated_name(name)
    if os.name == "nt":
        click.echo("shared daemons run on POSIX only; nothing to stop")
        return
    selected, warnings = _select(name)
    _echo_warnings(warnings)
    live = [d for d in selected if d.state.alive]
    if not live:
        # A stale lifetime lock is left in place: it is harmless, the next
        # daemon of that name takes it over, and ``mm uninstall`` removes it.
        click.echo(f"shared daemon {name!r} is not running" if name else "no shared daemon running")
        return
    failures: list[str] = []
    for d in live:
        error = _stop_one(d, grace)
        if error is None:
            click.echo(f"stopped {d.name} pid={d.state.pid}")
        else:
            failures.append(error)
    if failures:
        raise click.ClickException("\n".join(failures))
