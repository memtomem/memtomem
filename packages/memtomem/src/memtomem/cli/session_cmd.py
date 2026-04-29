"""CLI: mm session — manage agent sessions and activity logging."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    InvalidNameError,
    validate_agent_id,
    validate_namespace,
)

logger = logging.getLogger(__name__)


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}

# Per-call cap on ``--auto-end-stale``'s cleanup work. Keeps a SessionStart
# hook's blocking window bounded when an incident leaves hundreds of orphans.
# Truncated runs leave the remainder for the next hook fire — see
# ``find_stale_active_sessions`` for the drain math.
_STALE_CLEANUP_BATCH = 100


def _parse_duration(spec: str) -> timedelta:
    """Parse a short duration like ``24h``, ``7d``, ``30m``, ``45s``.

    Raises ``click.BadParameter`` on unparseable input so the CLI surfaces
    a useful message instead of a stack trace. Backs ``--auto-end-stale``.
    """
    match = _DURATION_RE.match(spec)
    if not match:
        raise click.BadParameter(f"invalid duration {spec!r}; expected like '30m', '24h', '7d'.")
    n = int(match.group(1))
    unit = _DURATION_UNITS[match.group(2).lower()]
    return timedelta(**{unit: n})


def _derive_session_namespace(agent_id: str, namespace: str | None) -> str:
    """Resolve the namespace stored on a CLI-created session record.

    Mirrors the priority chain documented on ``mem_session_start``
    (``server/tools/session.py:76-95``), with the ``app.current_namespace``
    step omitted: each ``mm`` invocation is a fresh process and has no
    cross-call session state to consult.
    """
    if namespace:
        return namespace
    if agent_id and agent_id != "default":
        return f"{AGENT_NAMESPACE_PREFIX}{agent_id}"
    return "default"


# Session state file — stores active session UUID.
def _state_dir() -> Path:
    """Return the memtomem state directory, resolving HOME at call time."""
    return Path.home() / ".memtomem"


def _state_file() -> Path:
    """Return the path to the current-session state file (lazy — resolves HOME at call time)."""
    return _state_dir() / ".current_session"


def _read_current_session() -> str | None:
    """Read the active session ID from the state file, or None."""
    try:
        text = _state_file().read_text(encoding="utf-8").strip()
        return text if text else None
    except FileNotFoundError:
        return None


def _write_current_session(session_id: str) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    _state_file().write_text(session_id + "\n", encoding="utf-8")


def _clear_current_session() -> None:
    try:
        _state_file().unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# mm session
# ---------------------------------------------------------------------------


@click.group()
def session() -> None:
    """Manage agent sessions — start, end, list, events."""


@session.command()
@click.option("--agent-id", "-a", default="default", help="Agent identifier")
@click.option("--title", "-t", default=None, help="Session title")
@click.option("--namespace", "-n", default=None, help="Namespace for session")
@click.option(
    "--idempotent",
    is_flag=True,
    help=(
        "Return the active session for the same --agent-id if one exists, "
        "otherwise start a new one. Designed for SessionStart hooks."
    ),
)
@click.option(
    "--auto-end-stale",
    "auto_end_stale",
    default=None,
    metavar="DURATION",
    help=(
        "Before resolving idempotency, end any active session whose started_at "
        "is older than DURATION (e.g. 24h, 7d). Off by default — opt in for hooks."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help='Emit one JSON line: {"session_id":..., "resumed":bool, "auto_ended":[...]}',
)
def start(
    agent_id: str,
    title: str | None,
    namespace: str | None,
    idempotent: bool,
    auto_end_stale: str | None,
    as_json: bool,
) -> None:
    """Start a new session and save its ID to ~/.memtomem/.current_session.

    With ``--idempotent``, return the active session for the same ``--agent-id``
    if one exists; with ``--auto-end-stale=<duration>``, first close any active
    sessions older than the duration. See
    ``memtomem-docs/memtomem/planning/hooks-session-cli-rfc.md`` for the
    SessionStart hook design these flags back.

    ``--idempotent`` is safe for serial hook callers but not for concurrent
    ones. Two parallel ``mm session start --idempotent`` invocations can both
    observe a missing/stale state file and both create a session — the state
    file is written atomically per call but the read-then-create is not
    locked. Claude Code's SessionStart fires once per session, so this is
    fine for the documented hook recipe; multi-process callers must
    serialize themselves.
    """
    try:
        validate_agent_id(agent_id)
        if namespace is not None:
            validate_namespace(namespace)
    except InvalidNameError as e:
        raise click.ClickException(str(e)) from e
    stale_cutoff = _parse_duration(auto_end_stale) if auto_end_stale else None
    try:
        asyncio.run(
            _start(
                agent_id,
                title,
                namespace,
                idempotent=idempotent,
                stale_cutoff=stale_cutoff,
                stale_label=auto_end_stale,
                as_json=as_json,
            )
        )
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _start(
    agent_id: str,
    title: str | None,
    namespace: str | None,
    *,
    idempotent: bool = False,
    stale_cutoff: timedelta | None = None,
    stale_label: str | None = None,
    as_json: bool = False,
) -> None:
    from memtomem.cli._bootstrap import cli_components

    # JSON exposes a flat ``auto_ended`` list of UUIDs; the per-reason counters
    # below feed the text-mode breakdown so an interactive operator still sees
    # whether the cleanup came from cutoff age (stale) or a cross-agent
    # forced-end. ``sessions.metadata.reason`` carries the same information
    # row-by-row for JSON consumers that need it (queryable via SQL).
    auto_ended: list[str] = []
    n_stale = 0
    n_cross_agent = 0
    resumed = False
    session_id: str | None = None
    ns = _derive_session_namespace(agent_id, namespace)

    async with cli_components() as comp:
        if stale_cutoff is not None:
            cutoff_iso = (datetime.now(timezone.utc) - stale_cutoff).isoformat(timespec="seconds")
            stale_rows = await comp.storage.find_stale_active_sessions(
                cutoff_iso, limit=_STALE_CLEANUP_BATCH
            )
            for row in stale_rows:
                await comp.storage.end_session(
                    row["id"],
                    f"auto-ended after {stale_label} inactivity",
                    {"auto_ended": True, "reason": "stale"},
                )
                auto_ended.append(row["id"])
                n_stale += 1
            if len(stale_rows) >= _STALE_CLEANUP_BATCH:
                logger.warning(
                    "auto-end-stale truncated to %d sessions; rerun the hook "
                    "or invoke `mm session start --auto-end-stale %s` again to "
                    "drain the rest",
                    _STALE_CLEANUP_BATCH,
                    stale_label,
                )

        if idempotent:
            current = _read_current_session()
            if current:
                current_row = await comp.storage.get_session(current)
                if current_row and current_row["ended_at"] is None:
                    if current_row["agent_id"] == agent_id:
                        session_id = current
                        resumed = True
                    else:
                        # Cross-agent collision: end the old session here and
                        # leave session_id=None so the create branch below
                        # mints a fresh ID and rewrites the state file.
                        await comp.storage.end_session(
                            current,
                            f"auto-ended on cross-agent resume to {agent_id}",
                            {"auto_ended": True, "reason": "cross_agent"},
                        )
                        auto_ended.append(current)
                        n_cross_agent += 1

        if session_id is None:
            session_id = str(uuid.uuid4())
            metadata = {"title": title} if title else {}
            await comp.storage.create_session(session_id, agent_id, ns, metadata)
            _write_current_session(session_id)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "session_id": session_id,
                    "resumed": resumed,
                    "auto_ended": auto_ended,
                }
            )
        )
        return

    if resumed:
        click.echo(f"Session resumed: {session_id}")
    else:
        click.echo(f"Session started: {session_id}")
        if title:
            click.echo(f"  Title: {title}")
    click.echo(f"  Agent: {agent_id}")
    click.echo(f"  Namespace: {ns}")
    if auto_ended:
        bits = []
        if n_stale:
            bits.append(f"{n_stale} stale")
        if n_cross_agent:
            bits.append(f"{n_cross_agent} cross-agent")
        click.echo(f"  Auto-ended {len(auto_ended)} session(s) ({', '.join(bits)})")


@session.command()
@click.option("--summary", "-s", default=None, help="Session summary")
@click.option("--auto", "auto_summary", is_flag=True, help="Generate summary from events")
def end(summary: str | None, auto_summary: bool) -> None:
    """End the current session."""
    session_id = _read_current_session()
    if not session_id:
        raise click.ClickException("No active session. Run `mm session start` first.")
    try:
        asyncio.run(_end(session_id, summary, auto_summary))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _end(session_id: str, summary: str | None, auto_summary: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        events = await comp.storage.get_session_events(session_id)

        if auto_summary and not summary:
            # Build summary from events
            type_counts: dict[str, int] = {}
            for ev in events:
                t = ev["event_type"]
                type_counts[t] = type_counts.get(t, 0) + 1
            parts = [f"{v}x {k}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1])]
            summary = (
                f"Session had {len(events)} events: {', '.join(parts)}"
                if parts
                else "Session ended (no events)"
            )

        metadata = {"event_count": len(events)}
        await comp.storage.end_session(session_id, summary, metadata)

    _clear_current_session()
    click.echo(f"Session ended: {session_id}")
    if summary:
        click.echo(f"  Summary: {summary}")


@session.command("list")
@click.option("--agent-id", "-a", default=None, help="Filter by agent ID")
@click.option("--since", default=None, help="Filter by start date (YYYY-MM-DD)")
@click.option("--limit", "-l", default=20, help="Max results")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def list_sessions(
    agent_id: str | None, since: str | None, limit: int, *, as_json: bool = False
) -> None:
    """List sessions."""
    try:
        asyncio.run(_list_sessions(agent_id, since, limit, as_json=as_json))
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _list_sessions(
    agent_id: str | None, since: str | None, limit: int, *, as_json: bool = False
) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        sessions = await comp.storage.list_sessions(agent_id=agent_id, since=since, limit=limit)

    if as_json:
        payload = [
            {
                "id": s["id"],
                "agent_id": s["agent_id"],
                "started_at": s["started_at"],
                "ended_at": s["ended_at"],
                "status": "ended" if s["ended_at"] else "active",
            }
            for s in sessions
        ]
        click.echo(json.dumps({"sessions": payload, "count": len(payload)}, indent=2))
        return

    if not sessions:
        click.echo("No sessions found.")
        return

    click.echo(f"{'ID':<38}{'Agent':<15}{'Started':<22}{'Status'}")
    click.echo("-" * 85)
    for s in sessions:
        status = "ended" if s["ended_at"] else "active"
        started = s["started_at"][:19] if s["started_at"] else ""
        click.echo(f"{s['id']:<38}{s['agent_id']:<15}{started:<22}{status}")
    click.echo(f"\n{len(sessions)} session(s)")


@session.command()
@click.argument("session_id", default="")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def events(session_id: str, *, as_json: bool = False) -> None:
    """Show events for a session. Uses current session if no ID given."""
    if not session_id:
        session_id = _read_current_session() or ""
    if not session_id:
        # JSON callers get a parseable error shape instead of a Click exit-1
        # so ``mm session events --json | jq`` doesn't break when no session
        # is active. Text callers keep the original ClickException path.
        if as_json:
            click.echo(json.dumps({"error": "no_session"}))
            return
        raise click.ClickException("No session ID provided and no active session.")
    try:
        asyncio.run(_events(session_id, as_json=as_json))
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _events(session_id: str, *, as_json: bool = False) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        evts = await comp.storage.get_session_events(session_id)

    if as_json:
        payload = [
            {
                "created_at": ev["created_at"],
                "event_type": ev["event_type"],
                "content": ev["content"],
            }
            for ev in evts
        ]
        click.echo(
            json.dumps(
                {"session_id": session_id, "events": payload, "count": len(payload)},
                indent=2,
            )
        )
        return

    if not evts:
        click.echo("No events for this session.")
        return

    click.echo(f"{'Time':<22}{'Type':<18}{'Content'}")
    click.echo("-" * 70)
    for ev in evts:
        ts = ev["created_at"][:19] if ev["created_at"] else ""
        content = ev["content"][:40].replace("\n", " ")
        click.echo(f"{ts:<22}{ev['event_type']:<18}{content}")
    click.echo(f"\n{len(evts)} event(s)")


# ---------------------------------------------------------------------------
# mm activity
# ---------------------------------------------------------------------------


@click.group()
def activity() -> None:
    """Log agent activity events to the current session."""


@activity.command("log")
@click.option(
    "--type",
    "event_type",
    type=click.Choice(["tool_call", "subagent_start", "subagent_stop", "decision", "error"]),
    default="tool_call",
    help="Event type",
)
@click.option("--content", "-c", required=True, help="Event description")
@click.option("--meta", default=None, help="JSON metadata")
@click.option("--json", "as_json", is_flag=True, help="Output a JSON ack for scripting.")
def log_event(event_type: str, content: str, meta: str | None, *, as_json: bool = False) -> None:
    """Log an activity event to the current session.

    Silent by default so hook callers never fail. ``--json`` emits an ack
    shape on stdout: ``{"ok": true, ...}`` on success, ``{"ok": false,
    "reason": ...}`` when there is no active session or the write failed.
    Exit code is always 0.
    """
    session_id = _read_current_session()
    if not session_id:
        # No active session — silently skip (hooks should not fail).
        # --json callers get a parseable skip ack so pipelines can tell
        # "no session" apart from "event written".
        if as_json:
            click.echo(json.dumps({"ok": False, "reason": "no_active_session"}))
        return
    try:
        metadata = json.loads(meta) if meta else None
    except json.JSONDecodeError:
        # Malformed --meta: under --json emit the error ack (exit 0) so
        # scripts can distinguish "bad input" from "write failed". Under
        # text path, let Click surface the traceback — a hook author
        # mistyping meta wants to see why.
        if as_json:
            click.echo(json.dumps({"ok": False, "reason": "invalid_meta"}))
            return
        raise
    try:
        asyncio.run(_log_event(session_id, event_type, content, metadata))
    except Exception:
        logger.warning("Activity hook failed", exc_info=True)
        if as_json:
            click.echo(json.dumps({"ok": False, "reason": "write_failed"}))
        return
    if as_json:
        click.echo(json.dumps({"ok": True, "session_id": session_id, "event_type": event_type}))


async def _log_event(session_id: str, event_type: str, content: str, metadata: dict | None) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        await comp.storage.add_session_event(session_id, event_type, content, metadata=metadata)


# ---------------------------------------------------------------------------
# mm session wrap
# ---------------------------------------------------------------------------


@session.command()
@click.option("--agent-id", "-a", default="headless", help="Agent identifier")
@click.option("--title", "-t", default=None, help="Session title")
@click.argument("command", nargs=-1, required=True)
def wrap(agent_id: str, title: str | None, command: tuple[str, ...]) -> None:
    """Wrap a command with session start/end.

    Usage: mm session wrap -- claude -p "run tests"
    """
    import subprocess
    import sys

    try:
        validate_agent_id(agent_id)
    except InvalidNameError as e:
        raise click.ClickException(str(e)) from e

    # Start session
    session_id = str(uuid.uuid4())
    cmd_str = " ".join(command)
    effective_title = title or f"Headless: {cmd_str[:60]}"

    try:
        asyncio.run(_wrap_start(session_id, agent_id, effective_title))
    except Exception as e:
        click.echo(f"Warning: session start failed: {e}", err=True)

    _write_current_session(session_id)

    # Run the wrapped command
    try:
        result = subprocess.run(command, check=False)
        exit_code = result.returncode
    except KeyboardInterrupt:
        exit_code = 130
    except Exception as e:
        click.echo(f"Command failed: {e}", err=True)
        exit_code = 1

    # End session
    summary = f"Command: {cmd_str[:100]}. Exit code: {exit_code}"
    try:
        asyncio.run(_wrap_end(session_id, summary, exit_code))
    except Exception as e:
        click.echo(f"Warning: session end failed: {e}", err=True)

    _clear_current_session()
    sys.exit(exit_code)


async def _wrap_start(session_id: str, agent_id: str, title: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    ns = _derive_session_namespace(agent_id, None)
    async with cli_components() as comp:
        await comp.storage.create_session(session_id, agent_id, ns, {"title": title})


async def _wrap_end(session_id: str, summary: str, exit_code: int) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        events = await comp.storage.get_session_events(session_id)
        metadata = {"event_count": len(events), "exit_code": exit_code}
        await comp.storage.end_session(session_id, summary, metadata)
