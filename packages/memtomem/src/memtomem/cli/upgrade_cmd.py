"""CLI: ``mm upgrade`` — stop running memtomem processes, then reinstall.

``uv tool install --reinstall memtomem`` only replaces the on-disk bytes;
any ``memtomem-server`` process already imported by an MCP client keeps
running the old code until it exits. That split-brain is exactly what
caused the v0.1.25 → v0.1.26 stale ``.server.pid`` repro that motivated
issue #443. The same applies to a backgrounded ``mm web`` — it holds
``web.pid`` and keeps serving the previous version against the shared DB
(#1569). ``mm upgrade`` wraps the reinstall with process-level hygiene:

    enumerate live servers + probe web UI → SIGTERM (escalate to SIGKILL
    after grace) → reinstall → reconcile one post-install snapshot using
    process start stamps → retire old/unstamped holders while preserving the
    new generation. A final ``BEGIN IMMEDIATE`` DB probe warns about writers
    the pid files cannot explain (#1606).

There is no ``--skip-pkill``: the kill-then-reinstall ordering is the
whole reason this command exists. On Windows the kill stage is skipped
automatically (POSIX advisory flock + signals are unavailable) and the
user is told to stop the processes manually if they observe a
split-brain.
"""

from __future__ import annotations

import json as _json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import click

from memtomem._instance_registry import RegistrySnapshot, snapshot_all_instances
from memtomem._process_probe import probe_pid
from memtomem._runtime_paths import legacy_server_pid_path
from memtomem.cli._db_lock import check_db_lock
from memtomem.cli._liveness import (
    ServerState,
    check_web_liveness,
    enumerate_server_liveness_inventory,
    probe_legacy_pid_file,
    probe_pid_file,
    record_narrowed_inventory_warning,
)
from memtomem.cli._prompts import confirm as _confirm

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

# A holder can acquire its compatibility/runtime lock just before writing the
# pid payload. Three short exponential retries (350 ms total) cover that
# bounded startup window under load without charging the normal path or
# turning a real unsignalable holder into an unbounded wait.
_INVENTORY_RETRY_DELAYS = (0.05, 0.1, 0.2)


def _isatty() -> bool:
    """CliRunner seam (mirrors ``uninstall_cmd._isatty``)."""
    return sys.stdin.isatty()


def _format_path(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return s.replace(home, "~", 1) if s.startswith(home) else s


def _utc_now() -> datetime:
    """Clock seam for process-generation tests."""
    return datetime.now(UTC)


def _started_at(state: ServerState) -> datetime | None:
    """Parse a pid payload's UTC generation stamp, if it has one."""
    if state.started is None:
        return None
    try:
        parsed = datetime.fromisoformat(state.started)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_new_generation(state: ServerState, installed_at: datetime) -> bool:
    """Return whether *state* declares it started after installation.

    The stamp is captured at process entry/pid publication, not Python's
    module-import instant. The pre-install inventory bounds that tiny gap.
    Both clocks are same-host wall clocks; strict ``>`` keeps equality on the
    conservative retirement side (an NTP forward step remains a negligible
    residual risk).
    """
    started = _started_at(state)
    return started is not None and started > installed_at


def _same_process_generation(current: ServerState, previous: ServerState) -> bool:
    """Conservatively compare a post-stop holder with its retirement target.

    Start stamps disambiguate the case where the kernel recycles a PID during
    cleanup.  A matching PID without two stamps is not identity: accepting it
    would let an unrelated replacement process inherit the retirement target.
    """
    if current.pid != previous.pid:
        return False
    current_started = _started_at(current)
    previous_started = _started_at(previous)
    return (
        current_started is not None
        and previous_started is not None
        and current_started == previous_started
    )


def _probe_problem(state: ServerState, label: str) -> str:
    """Describe one unverifiable state without claiming a held lock."""
    if state.pid_file is None:
        action = "enumerating pid files"
    else:
        action = f"probing {_format_path(state.pid_file)}"
    return f"{label}: {action} failed ({state.probe_error})"


def _upgrade_server_stops(states: list[ServerState]) -> list[ServerState]:
    """Return authoritative server states that are safe to signal.

    A classified shared legacy lock is only a modern compatibility alias.
    An exclusive legacy lock is a separate old server and is stoppable. Tests
    and compatibility callers can still supply an unclassified legacy state;
    retain the pre-#2002 rule there and suppress it only when a runtime state
    is also present.
    """
    legacy_pid = legacy_server_pid_path()
    runtime_states = [state for state in states if state.pid_file != legacy_pid]
    stops = list(runtime_states)
    for state in states:
        if state.pid_file != legacy_pid:
            continue
        if state.legacy_lock_mode == "shared":
            continue
        if state.legacy_lock_mode == "exclusive" or not runtime_states:
            stops.append(state)
    return stops


def _inventory_problems(server_states: list[ServerState], web_state: ServerState) -> list[str]:
    """Return every error or live state that cannot be safely retired."""
    legacy_pid = legacy_server_pid_path()
    problems = [
        _probe_problem(state, "memtomem-server")
        for state in server_states
        if state.probe_error is not None
    ]
    if web_state.probe_error is not None:
        problems.append(_probe_problem(web_state, "web UI"))

    direct_servers = _upgrade_server_stops(server_states)
    for state in direct_servers:
        if state.probe_error is None and state.pid is None:
            path = _format_path(state.pid_file) if state.pid_file is not None else "?"
            if state.legacy_lock_mode == "exclusive":
                problems.append(
                    "legacy memtomem-server: the exclusive compatibility lock at "
                    f"{path} has no signalable PID"
                )
            else:
                problems.append(f"memtomem-server: live lock {path} has no signalable PID")

    shared_aliases = [state for state in server_states if state.legacy_lock_mode == "shared"]
    runtime_servers = [
        state
        for state in direct_servers
        if state.pid_file != legacy_pid and state.probe_error is None and state.pid is not None
    ]
    if shared_aliases and not runtime_servers:
        path = _format_path(shared_aliases[0].pid_file or legacy_server_pid_path())
        problems.append(
            "legacy memtomem-server: the shared compatibility lock at "
            f"{path} has no authoritative runtime PID to signal"
        )

    if web_state.alive and web_state.probe_error is None and web_state.pid is None:
        path = _format_path(web_state.pid_file) if web_state.pid_file is not None else "?"
        problems.append(f"web UI: live lock {path} has no signalable PID")
    return problems


def _registry_inventory_problems(
    snapshot: RegistrySnapshot,
    server_states: list[ServerState],
) -> list[str]:
    """Return fail-closed findings from the all-process server registry.

    Pid files name only their primary holder.  Startup presence markers and
    store sentinels also reveal handshake-only and secondary server processes;
    those processes cannot be safely terminated from a pid-file inventory and
    therefore must block an in-place reinstall.  ``procid`` distinguishes two
    processes with the same pid across pid namespaces, while joining a
    process's presence marker and store sentinel without double-counting it.
    """
    problems: list[str] = []
    if snapshot.canonical_error is not None:
        problems.append(
            "server registry: canonical runtime directory could not be read "
            f"({snapshot.canonical_error})"
        )
    if snapshot.refusal is not None:
        path, exc = snapshot.refusal
        problems.append(f"server registry: refused runtime root {_format_path(path)} ({exc})")
    if not snapshot.complete:
        problems.append("server registry: the all-process snapshot is incomplete")

    attributed_pids = {
        state.pid
        for state in _upgrade_server_stops(server_states)
        if state.alive and state.probe_error is None and state.pid is not None
    }
    procids_by_pid: dict[int, set[str]] = {}
    for info in (*snapshot.instances, *snapshot.presence):
        procids_by_pid.setdefault(info.pid, set()).add(info.procid)
    for pid, procids in sorted(procids_by_pid.items()):
        if pid not in attributed_pids:
            problems.append(
                f"server registry: live server pid {pid} has no authoritative pid lock "
                "(secondary or startup-only process); stop it manually"
            )
        elif len(procids) > 1:
            problems.append(
                f"server registry: pid {pid} identifies {len(procids)} live process "
                "identities across namespaces; automatic termination is unsafe"
            )
    return problems


def _complete_inventory_problems(
    server_states: list[ServerState],
    web_state: ServerState,
    registry: RegistrySnapshot,
    *,
    is_windows: bool,
) -> list[str]:
    """Combine pid-lock and registry evidence into one mutation gate."""
    problems = _inventory_problems(server_states, web_state)
    problems.extend(_registry_inventory_problems(registry, server_states))
    if is_windows:
        for state in _upgrade_server_stops(server_states):
            if state.alive and state.probe_error is None:
                problems.append(
                    "memtomem-server: a live process is present on Windows; stop every "
                    "server manually before upgrading"
                )
                break
        if web_state.alive and web_state.probe_error is None:
            problems.append(
                "web UI: a live process is present on Windows; stop `mm web` manually "
                "before upgrading"
            )
    return list(dict.fromkeys(problems))


def _has_startup_gap(server_states: list[ServerState], web_state: ServerState) -> bool:
    """Return whether a normal pid-payload startup window may be visible."""
    legacy_pid = legacy_server_pid_path()
    direct_servers = _upgrade_server_stops(server_states)
    if any(
        state.alive and state.probe_error is None and state.pid is None for state in direct_servers
    ):
        return True
    if web_state.alive and web_state.probe_error is None and web_state.pid is None:
        return True

    has_shared_alias = any(state.legacy_lock_mode == "shared" for state in server_states)
    has_signalable_runtime = any(
        state.pid_file != legacy_pid and state.probe_error is None and state.pid is not None
        for state in direct_servers
    )
    return has_shared_alias and not has_signalable_runtime


def _stabilize_process_inventory(
    server_states: list[ServerState] | None = None,
    web_state: ServerState | None = None,
    server_warning: str | None = None,
) -> tuple[list[ServerState], ServerState, str | None]:
    """Retry only transient lock-before-payload startup snapshots.

    Enumeration and real probe errors remain immediate failures. A held lock
    with no pid, or a shared legacy alias whose runtime pid file has not yet
    appeared, gets two bounded backoff retries before it becomes a hard
    inventory problem.
    """
    collected_warnings = [
        warning
        for warning in [server_warning, *(state.probe_warning for state in server_states or [])]
        if warning
    ]
    if server_states is None:
        current_servers, current_warning = enumerate_server_liveness_inventory()
        if current_warning:
            collected_warnings.append(current_warning)
    else:
        current_servers = server_states
    current_web = check_web_liveness() if web_state is None else web_state
    for delay in _INVENTORY_RETRY_DELAYS:
        if not _has_startup_gap(current_servers, current_web):
            break
        time.sleep(delay)
        current_servers, current_warning = enumerate_server_liveness_inventory()
        if current_warning:
            collected_warnings.append(current_warning)
        current_web = check_web_liveness()
    warning = "; ".join(dict.fromkeys(collected_warnings)) or None
    return current_servers, current_web, warning


def _json_with_warnings(payload: dict[str, object], warnings: list[str]) -> dict[str, object]:
    return {**payload, **({"warnings": warnings} if warnings else {})}


def _inventory_failure_message(problems: list[str], *, package_changed: bool = False) -> str:
    """Format all inventory failures with phase-accurate remediation."""
    detail = "\n".join(f"- {problem}" for problem in problems)
    if package_changed:
        suffix = (
            "The package was reinstalled, but process generation cleanup is incomplete. "
            "Stop every memtomem server and `mm web` manually, then restart them."
        )
    else:
        suffix = (
            "Refusing to reinstall. Restore access to every reported path and stop the "
            "reported process manually, then retry."
        )
    return f"Cannot verify the complete memtomem process inventory:\n{detail}\n{suffix}"


def _refuse_upgrade(
    message: str,
    *,
    json_out: bool,
    killed: list[int] | None = None,
    removed: list[Path] | None = None,
    extra: dict[str, object] | None = None,
) -> NoReturn:
    """Emit a structured or human refusal and terminate with status 1."""
    if json_out:
        payload: dict[str, object] = {"ok": False, "error": message}
        if killed is not None:
            payload["killed"] = killed
        if removed is not None:
            payload["removed"] = [str(path) for path in removed]
        if extra is not None:
            payload.update(extra)
        click.echo(_json.dumps(payload))
    else:
        click.secho(message, fg="red")
        if killed:
            click.echo("Stopped before failure: " + ", ".join(str(pid) for pid in killed))
        if removed:
            for path in removed:
                click.echo(f"Removed before failure: {_format_path(path)}")
    sys.exit(1)


def _reprobe_process_state(state: ServerState) -> ServerState:
    """Re-probe the same lock path while preserving legacy classification."""
    if state.pid_file is None:
        return ServerState(alive=False, pid=None, pid_file=None)
    if os.name != "nt" and state.pid_file == legacy_server_pid_path():
        return probe_legacy_pid_file(state.pid_file)
    return probe_pid_file(state.pid_file)


def _stop_server(
    state: ServerState,
    grace: float,
    *,
    warnings_to_stderr: bool = False,
) -> tuple[list[int], list[Path], ServerState, str | None]:
    """SIGTERM the live pid-file holder, escalate to SIGKILL after ``grace`` seconds.

    Works on any :class:`ServerState` — the MCP server and ``mm web``
    both hold their pid file with the same flock contract. Returns
    ``(killed_pids, removed_pid_files, post_stop_state, error)``. Returning
    errors keeps partial kill/unlink accounting available to both human and
    JSON callers. Caller is responsible for skipping this on Windows / when
    ``state.alive`` is False.
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
            return (
                killed,
                removed,
                state,
                f"cannot signal pid {pid}: {exc}. Stop the process manually and retry.",
            )

        # Poll for exit. server's ``_install_sigterm_handler`` (#439)
        # unlinks its own pid file on a clean SIGTERM, so the file may
        # vanish before grace expires — that's fine.
        # The two collapses of the tri-state differ, which is why this reads
        # the probe directly rather than through a boolean helper (#2234).
        # Waiting: stop early only on positive evidence the process is gone —
        # "unknown" keeps polling until the grace period expires, which costs
        # time and nothing else.
        deadline = time.monotonic() + grace
        while pid is not None and time.monotonic() < deadline:
            if probe_pid(pid) == "dead":
                break
            time.sleep(0.1)
        # Escalating: SIGKILL only on positive evidence it is still alive.
        # "unknown" must not become a signal sent to a pid we cannot vouch for.
        if pid is not None and probe_pid(pid) == "alive":
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
        recheck = _reprobe_process_state(state)
        if recheck.probe_error is not None:
            return (
                killed,
                removed,
                recheck,
                f"cannot verify {_format_path(state.pid_file)} after stopping pid "
                f"{state.pid}: {recheck.probe_error}",
            )
        if recheck.alive:
            click.secho(
                f"  Skipping pid-file unlink — {state.pid_file} is now held by a "
                "freshly started writer (likely an auto-restart from your MCP "
                "client). Leaving its live lock intact.",
                fg="yellow",
                err=warnings_to_stderr,
            )
        else:
            try:
                state.pid_file.unlink(missing_ok=True)
                removed.append(state.pid_file)
            except OSError as exc:
                return (
                    killed,
                    removed,
                    recheck,
                    f"failed to remove stale pid file {state.pid_file}: {exc}",
                )
    else:
        recheck = ServerState(alive=False, pid=None, pid_file=None)

    return killed, removed, recheck, None


def _cleanup_web_sidecar(web_state: ServerState, removed: list[Path]) -> None:
    """Remove stale ``web.json`` without touching a replacement writer's file."""
    if web_state.pid_file is None or web_state.pid_file not in removed:
        return

    from memtomem.cli.web import _WEB_INFO_NAME

    info_file = web_state.pid_file.with_name(_WEB_INFO_NAME)
    if not info_file.exists():
        return
    recheck = probe_pid_file(web_state.pid_file)
    if recheck.alive:
        return
    try:
        info_file.unlink()
        removed.append(info_file)
    except OSError:
        pass


def _stop_process_snapshot(
    server_states: list[ServerState],
    web_state: ServerState,
    *,
    grace: float,
    warnings_to_stderr: bool = False,
    installed_at: datetime | None = None,
    continue_on_error: bool = False,
) -> tuple[list[int], list[Path], list[str], list[ServerState], ServerState]:
    """Stop attributable old processes and account for every outcome.

    When *installed_at* is provided, holders whose pid metadata says they
    started after that timestamp are already on the new generation and are
    left running. Post-install cleanup can set *continue_on_error* so one bad
    holder does not prevent best-effort retirement of the rest. The default
    pre-install path stops immediately after the first failed retirement.
    """
    killed: list[int] = []
    removed: list[Path] = []
    problems: list[str] = []
    remaining_servers: list[ServerState] = []
    for state in _upgrade_server_stops(server_states):
        if state.probe_error is not None or state.pid is None:
            continue
        if installed_at is not None and _is_new_generation(state, installed_at):
            remaining_servers.append(state)
            continue
        stopped, cleaned, recheck, error = _stop_server(
            state,
            grace=grace,
            warnings_to_stderr=warnings_to_stderr,
        )
        killed.extend(stopped)
        removed.extend(cleaned)
        if error is not None:
            problems.append(error)
        if recheck.alive:
            remaining_servers.append(recheck)
        if error is not None and not continue_on_error:
            break

    remaining_web = ServerState(alive=False, pid=None, pid_file=None)
    if (
        (continue_on_error or not problems)
        and web_state.alive
        and web_state.probe_error is None
        and web_state.pid is not None
    ):
        if installed_at is not None and _is_new_generation(web_state, installed_at):
            remaining_web = web_state
        else:
            stopped, cleaned, recheck, error = _stop_server(
                web_state,
                grace=grace,
                warnings_to_stderr=warnings_to_stderr,
            )
            killed.extend(stopped)
            removed.extend(cleaned)
            if error is not None:
                problems.append(error)
            _cleanup_web_sidecar(web_state, removed)
            if recheck.alive:
                remaining_web = recheck
    return killed, removed, problems, remaining_servers, remaining_web


def _resolve_db_path() -> Path | None:
    """Best-effort DB path for the post-stop write-lock probe (#1606).

    ``migrate=False`` for the same reason as ``mm reset``: this is a
    read-only lookup and the auto-discover migration would rewrite
    config.json (and create its lock sidecar) as a side effect.

    Returns ``None`` on any failure — the probe is belt-and-braces, and
    an upgrade must never be blocked by an unrelated config problem
    (matching ``check_db_lock``'s own fail-open contract).
    """
    try:
        from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

        cfg = Mem2MemConfig()
        load_config_d(cfg)
        load_config_overrides(cfg, migrate=False)
        return Path(cfg.storage.sqlite_path).expanduser()
    except Exception:
        return None


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
    """Stop running memtomem servers and the Web UI, then reinstall via ``uv tool``.

    The canonical ``uv tool install --reinstall memtomem`` only swaps the
    on-disk bytes; any server already imported by an MCP client — and any
    backgrounded ``mm web`` (#1569) — keeps running the previous version.
    ``mm upgrade`` adds the missing process-level hygiene step around it.
    """
    is_windows = sys.platform == "win32"
    server_states, server_warning = enumerate_server_liveness_inventory()
    web_state = check_web_liveness()
    if not is_windows:
        server_states, web_state, server_warning = _stabilize_process_inventory(
            server_states, web_state, server_warning
        )
    registry = snapshot_all_instances()
    extras, extras_auto = _resolve_extras(extras_flag)
    install_cmd = _build_install_cmd(version, extras)
    pkg_target = install_cmd[-1]
    server_stops = _upgrade_server_stops(server_states)
    live_states = [*server_stops, *([web_state] if web_state.alive else [])]
    initial_problems = _complete_inventory_problems(
        server_states,
        web_state,
        registry,
        is_windows=is_windows,
    )
    warnings: list[str] = []
    record_narrowed_inventory_warning(warnings, server_warning, emit=False)
    # Windows skips the kill stage entirely, so a truthful plan/dry-run
    # must not claim we would kill or remove anything there.
    planned_stops = [] if is_windows else live_states

    # ----- plan -----
    if not json_out:
        click.echo("memtomem upgrade plan:")
        if is_windows:
            click.secho(
                "  Detected Windows; process termination is unavailable. "
                "Any live server or `mm web` process makes this upgrade fail closed.",
                fg="yellow",
            )
        elif live_states:
            labeled_states = [(server, "server") for server in server_stops]
            if web_state.alive:
                labeled_states.append((web_state, "web UI"))
            for live, label in labeled_states:
                pid_repr = live.pid if live.pid is not None else "?"
                pid_file_repr = _format_path(live.pid_file) if live.pid_file else "?"
                click.echo(f"  Stop running {label} (pid {pid_repr}, lock {pid_file_repr})")
                click.echo(f"  Wait up to {grace:g}s for graceful exit, then SIGKILL")
                click.echo(f"  Remove stale {pid_file_repr}")
        else:
            click.echo("  No running server or web UI detected — reinstall only")
        if extras:
            source = "auto-detected from uv tool receipt" if extras_auto else "from --extras"
            click.echo(f"  Extras: [{','.join(extras)}] ({source})")
        elif extras_auto:
            click.echo("  Extras: none detected (bare install)")
        click.echo(f"  Reinstall: {' '.join(install_cmd)}")
        if not is_windows:
            click.echo("  Recycle any server or Web UI process present after reinstall")
        if initial_problems:
            click.echo("  Inventory diagnostics:")
            for problem in initial_problems:
                click.echo(f"    - {problem}")
        for warning in warnings:
            click.secho(f"  Warning: {warning}", fg="yellow")

    if dry_run:
        if json_out:
            payload: dict[str, object] = {
                "ok": not initial_problems,
                "dry_run": True,
                "inventory_complete": not initial_problems,
                "would_kill": [s.pid for s in planned_stops if s.pid is not None],
                "would_remove": [str(s.pid_file) for s in planned_stops if s.pid_file is not None],
                "would_install": install_cmd,
                "extras": extras,
                "version": version,
            }
            if warnings:
                payload["warnings"] = warnings
            if initial_problems:
                payload["error"] = _inventory_failure_message(initial_problems)
            click.echo(_json.dumps(payload))
        if initial_problems:
            if not json_out:
                click.secho(_inventory_failure_message(initial_problems), fg="red")
            sys.exit(1)
        return

    if initial_problems:
        _refuse_upgrade(
            _inventory_failure_message(initial_problems),
            json_out=json_out,
            extra={"warnings": warnings} if warnings else None,
        )

    # ----- confirm -----
    if not yes:
        if not _isatty():
            msg = "Refusing to upgrade without confirmation in a non-interactive shell. Pass -y."
            if json_out:
                click.echo(_json.dumps(_json_with_warnings({"ok": False, "error": msg}, warnings)))
                sys.exit(1)
            click.secho(msg, fg="red")
            raise click.Abort()
        # err=json_out keeps stdout a single JSON document in interactive
        # `mm upgrade --json` runs (via _prompts.confirm — #1640).
        if not _confirm("\nProceed with upgrade?", default=True, err=json_out):
            # Voluntary cancel → exit 0; keep JSON schema consistent.
            if json_out:
                click.echo(
                    _json.dumps(_json_with_warnings({"ok": True, "cancelled": True}, warnings))
                )
            else:
                click.echo("Cancelled — nothing was changed.")
            return

    # ----- stop -----
    killed: list[int] = []
    removed: list[Path] = []
    if planned_stops:
        stopped, cleaned, stop_problems, _remaining_servers, _remaining_web = (
            _stop_process_snapshot(
                server_states,
                web_state,
                grace=grace,
                warnings_to_stderr=json_out,
            )
        )
        killed.extend(stopped)
        removed.extend(cleaned)
        if stop_problems:
            _refuse_upgrade(
                _inventory_failure_message(stop_problems),
                json_out=json_out,
                killed=killed,
                removed=removed,
                extra={"warnings": warnings} if warnings else None,
            )

    # ----- complete pre-install boundary (#2002) -----
    # A clean snapshot cannot be required here: MCP clients commonly respawn
    # immediately and would make upgrade impossible. Require a complete,
    # signalable inventory, then retire that generation after uv succeeds.
    # Windows cannot retire a holder automatically, but it still participates
    # in the boundary: an observed process is a refusal rather than permission
    # to reinstall bytes underneath it.
    if not is_windows:
        boundary_servers, boundary_web, boundary_warning = _stabilize_process_inventory()
        record_narrowed_inventory_warning(
            warnings,
            boundary_warning,
            emit=not json_out,
            indent="  ",
        )
    else:
        boundary_servers, boundary_warning = enumerate_server_liveness_inventory()
        boundary_web = check_web_liveness()
        record_narrowed_inventory_warning(
            warnings,
            boundary_warning,
            emit=not json_out,
            indent="  ",
        )
    boundary_registry = snapshot_all_instances()
    boundary_problems = _complete_inventory_problems(
        boundary_servers,
        boundary_web,
        boundary_registry,
        is_windows=is_windows,
    )
    if boundary_problems:
        _refuse_upgrade(
            _inventory_failure_message(boundary_problems),
            json_out=json_out,
            killed=killed,
            removed=removed,
            extra={"warnings": warnings} if warnings else None,
        )

    # ----- reinstall -----
    try:
        result = subprocess.run(install_cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        msg = "`uv` not found on PATH. Install uv (https://docs.astral.sh/uv/) and retry."
        if json_out:
            click.echo(
                _json.dumps(
                    _json_with_warnings(
                        {
                            "ok": False,
                            "error": msg,
                            "killed": killed,
                            "removed": [str(path) for path in removed],
                        },
                        warnings,
                    )
                )
            )
            sys.exit(1)
        click.secho(msg, fg="red")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        msg = "uv tool install timed out after 600s."
        if json_out:
            click.echo(
                _json.dumps(
                    _json_with_warnings(
                        {
                            "ok": False,
                            "error": msg,
                            "killed": killed,
                            "removed": [str(path) for path in removed],
                        },
                        warnings,
                    )
                )
            )
            sys.exit(1)
        click.secho(msg, fg="red")
        sys.exit(1)

    if result.returncode != 0:
        if json_out:
            click.echo(
                _json.dumps(
                    _json_with_warnings(
                        {
                            "ok": False,
                            "error": f"uv tool install failed (rc={result.returncode})",
                            "stderr": result.stderr,
                            "killed": killed,
                            "removed": [str(p) for p in removed],
                        },
                        warnings,
                    )
                )
            )
            sys.exit(1)
        click.secho(f"\nuv tool install failed (rc={result.returncode}):", fg="red")
        click.echo(result.stderr.rstrip())
        sys.exit(1)

    installed_at = _utc_now()
    final_servers = server_states
    final_web = web_state
    if not is_windows:
        # ----- post-install generation reconciliation -----
        # This is the third and final unconditional complete inventory
        # (initial / pre-install boundary / post-install). Processes carrying
        # a start timestamp at or after ``installed_at`` imported the new
        # bytes and stay up; older or unstamped holders are retired once.
        # Targeted per-path re-probes from _stop_server verify the result. A
        # fourth complete inventory is used only if one of those re-probes
        # catches the lock-before-pid startup window.
        retirement_servers, retirement_web, retirement_warning = _stabilize_process_inventory()
        retirement_registry = snapshot_all_instances()
        record_narrowed_inventory_warning(
            warnings,
            retirement_warning,
            emit=not json_out,
            indent="  ",
        )
        cleanup_problems = _complete_inventory_problems(
            retirement_servers,
            retirement_web,
            retirement_registry,
            is_windows=False,
        )
        retirement_server_targets = [
            state
            for state in _upgrade_server_stops(retirement_servers)
            if state.pid is not None
            and state.probe_error is None
            and not _is_new_generation(state, installed_at)
        ]
        retirement_web_target = (
            retirement_web
            if retirement_web.alive
            and retirement_web.pid is not None
            and retirement_web.probe_error is None
            and not _is_new_generation(retirement_web, installed_at)
            else None
        )
        stopped, cleaned, stop_problems, final_servers, final_web = _stop_process_snapshot(
            retirement_servers,
            retirement_web,
            grace=grace,
            warnings_to_stderr=json_out,
            installed_at=installed_at,
            continue_on_error=True,
        )
        killed.extend(stopped)
        removed.extend(cleaned)
        cleanup_problems.extend(stop_problems)

        if _has_startup_gap(final_servers, final_web):
            final_servers, final_web, final_warning = _stabilize_process_inventory(
                final_servers, final_web
            )
            record_narrowed_inventory_warning(
                warnings,
                final_warning,
                emit=not json_out,
                indent="  ",
            )
        final_registry = snapshot_all_instances()
        cleanup_problems.extend(
            _complete_inventory_problems(
                final_servers,
                final_web,
                final_registry,
                is_windows=False,
            )
        )

        for state in _upgrade_server_stops(final_servers):
            if state.probe_error is not None or state.pid is None:
                continue
            path = _format_path(state.pid_file) if state.pid_file is not None else "?"
            if state.legacy_lock_mode == "exclusive":
                cleanup_problems.append(
                    "legacy memtomem-server: an exclusive compatibility holder remains "
                    f"at {path} (pid {state.pid}); its generation cannot be verified"
                )
            elif any(_same_process_generation(state, old) for old in retirement_server_targets):
                cleanup_problems.append(
                    f"memtomem-server: retirement pid {state.pid} still holds {path}"
                )
            elif not _is_new_generation(state, installed_at):
                cleanup_problems.append(
                    f"memtomem-server: a pre-install or unverifiable generation pid "
                    f"{state.pid} still holds {path}"
                )
        if (
            final_web.alive
            and final_web.probe_error is None
            and final_web.pid is not None
            and not _is_new_generation(final_web, installed_at)
        ):
            path = _format_path(final_web.pid_file) if final_web.pid_file is not None else "?"
            if retirement_web_target is not None and _same_process_generation(
                final_web, retirement_web_target
            ):
                cleanup_problems.append(
                    f"web UI: retirement pid {final_web.pid} still holds {path}"
                )
            else:
                cleanup_problems.append(
                    f"web UI: a pre-install or unverifiable generation pid "
                    f"{final_web.pid} still holds {path}"
                )

        cleanup_problems = list(dict.fromkeys(cleanup_problems))
        if cleanup_problems:
            extra: dict[str, object] = {
                "reinstalled": pkg_target,
                "cleanup_complete": False,
            }
            if warnings:
                extra["warnings"] = warnings
            _refuse_upgrade(
                _inventory_failure_message(cleanup_problems, package_changed=True),
                json_out=json_out,
                killed=killed,
                removed=removed,
                extra=extra,
            )
    else:
        # A Windows process can appear while uv is installing. Accept only a
        # directly attributable pid-file holder stamped after installation;
        # everything else is old or unverifiable and therefore a split-brain
        # risk that requires a manual stop.
        final_servers, final_warning = enumerate_server_liveness_inventory()
        final_web = check_web_liveness()
        record_narrowed_inventory_warning(
            warnings,
            final_warning,
            emit=not json_out,
            indent="  ",
        )
        final_registry = snapshot_all_instances()
        cleanup_problems = _inventory_problems(final_servers, final_web)
        cleanup_problems.extend(_registry_inventory_problems(final_registry, final_servers))
        for state in _upgrade_server_stops(final_servers):
            if (
                state.alive
                and state.probe_error is None
                and not _is_new_generation(state, installed_at)
            ):
                cleanup_problems.append(
                    f"memtomem-server: pid {state.pid or '?'} started before the install "
                    "or has no verifiable generation stamp"
                )
        if (
            final_web.alive
            and final_web.probe_error is None
            and not _is_new_generation(final_web, installed_at)
        ):
            cleanup_problems.append(
                f"web UI: pid {final_web.pid or '?'} started before the install or has "
                "no verifiable generation stamp"
            )
        cleanup_problems = list(dict.fromkeys(cleanup_problems))
        if cleanup_problems:
            extra = {"reinstalled": pkg_target, "cleanup_complete": False}
            if warnings:
                extra["warnings"] = warnings
            _refuse_upgrade(
                _inventory_failure_message(cleanup_problems, package_changed=True),
                json_out=json_out,
                killed=killed,
                removed=removed,
                extra=extra,
            )

    # ----- unexplained DB writer warning (#1606) -----
    # Do not blame a verified replacement process for retaining old code.
    # When no accepted pid-file holder explains a lock, preserve the existing
    # warn-and-proceed heuristic for invisible writers.
    known_final_process = bool(_upgrade_server_stops(final_servers)) or final_web.alive
    db_path = _resolve_db_path()
    db_lock_warning = False
    if (
        db_path is not None
        and (is_windows or not known_final_process)
        and check_db_lock(db_path).locked
    ):
        db_lock_warning = True
        if not is_windows:
            # A new-generation process can start after the post-install
            # reconciliation snapshot but before this DB probe. Confirm only
            # the rare locked case once so we do not mislabel that verified
            # process as an old invisible writer. Probe failures stay warning-
            # side: only an actually observed holder suppresses the warning.
            late_servers, late_warning = enumerate_server_liveness_inventory()
            record_narrowed_inventory_warning(
                warnings,
                late_warning,
                emit=not json_out,
                indent="  ",
            )
            late_web = check_web_liveness()
            # Match the normal ``known_final_process`` authority filter: a
            # shared legacy alias alone is not enough. Suppress only for a
            # directly attributable holder whose start stamp proves it began
            # after this install; unstamped/path-foreign old binaries keep the
            # conservative warning.
            observed_late_process = any(
                state.alive
                and state.probe_error is None
                and _is_new_generation(state, installed_at)
                for state in _upgrade_server_stops(late_servers)
            ) or (
                late_web.alive
                and late_web.probe_error is None
                and _is_new_generation(late_web, installed_at)
            )
            if observed_late_process:
                db_lock_warning = False

    # ----- success -----
    if json_out:
        payload = {
            "ok": True,
            "killed": killed,
            "removed": [str(p) for p in removed],
            "reinstalled": pkg_target,
            "extras": extras,
            "version": version,
            "db_lock_warning": db_lock_warning,
        }
        if warnings:
            payload["warnings"] = warnings
        click.echo(_json.dumps(payload))
        return

    if killed:
        pids_repr = ", ".join(str(pid) for pid in killed)
        noun = "pids" if len(killed) > 1 else "pid"
        click.secho(f"\nStopped {noun} {pids_repr}.", fg="green")
    if removed:
        for removed_path in removed:
            click.echo(f"Removed {_format_path(removed_path)}.")
    click.secho(f"Reinstalled {pkg_target}.", fg="green")
    if db_lock_warning and db_path is not None:
        db_repr = _format_path(db_path)
        click.secho(
            f"Warning: another process still holds a write lock on {db_repr} — "
            "it keeps running the previous version until it exits. Find it with "
            f"`lsof {db_repr}` (or `ps aux | grep memtomem`) and restart it to "
            "pick up the upgrade.",
            fg="yellow",
        )
