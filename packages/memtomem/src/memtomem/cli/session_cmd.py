"""CLI: mm session — manage agent sessions and activity logging."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

import click

logger = logging.getLogger(__name__)


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
        text = _state_file().read_text().strip()
        return text if text else None
    except FileNotFoundError:
        return None


def _write_current_session(session_id: str) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    _state_file().write_text(session_id + "\n")


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
def start(agent_id: str, title: str | None, namespace: str | None) -> None:
    """Start a new session and save its ID to ~/.memtomem/.current_session."""
    try:
        asyncio.run(_start(agent_id, title, namespace))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _start(agent_id: str, title: str | None, namespace: str | None) -> None:
    from memtomem.cli._bootstrap import cli_components

    session_id = str(uuid.uuid4())
    ns = namespace or "default"
    metadata = {"title": title} if title else {}

    async with cli_components() as comp:
        await comp.storage.create_session(session_id, agent_id, ns, metadata)

    _write_current_session(session_id)
    click.echo(f"Session started: {session_id}")
    if title:
        click.echo(f"  Title: {title}")
    click.echo(f"  Agent: {agent_id}")


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
def list_sessions(agent_id: str | None, since: str | None, limit: int) -> None:
    """List sessions."""
    try:
        asyncio.run(_list_sessions(agent_id, since, limit))
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _list_sessions(agent_id: str | None, since: str | None, limit: int) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        sessions = await comp.storage.list_sessions(agent_id=agent_id, since=since, limit=limit)

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
def events(session_id: str) -> None:
    """Show events for a session. Uses current session if no ID given."""
    if not session_id:
        session_id = _read_current_session() or ""
    if not session_id:
        raise click.ClickException("No session ID provided and no active session.")
    try:
        asyncio.run(_events(session_id))
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _events(session_id: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        evts = await comp.storage.get_session_events(session_id)

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
def log_event(event_type: str, content: str, meta: str | None) -> None:
    """Log an activity event to the current session."""
    session_id = _read_current_session()
    if not session_id:
        # No active session — silently skip (hooks should not fail)
        return
    metadata = json.loads(meta) if meta else None
    try:
        asyncio.run(_log_event(session_id, event_type, content, metadata))
    except Exception:
        logger.warning("Activity hook failed", exc_info=True)


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

    async with cli_components() as comp:
        await comp.storage.create_session(session_id, agent_id, "default", {"title": title})


async def _wrap_end(session_id: str, summary: str, exit_code: int) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        events = await comp.storage.get_session_events(session_id)
        metadata = {"event_count": len(events), "exit_code": exit_code}
        await comp.storage.end_session(session_id, summary, metadata)
