"""Tools: mem_session_start, mem_session_end, mem_session_list."""

from __future__ import annotations

from uuid import uuid4

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_start(
    agent_id: str = "default",
    title: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Start a new episodic memory session.

    Creates a session record and sets it as the current session.
    All subsequent tool calls will be tracked as session events.

    Args:
        agent_id: Identifier for the agent starting the session
        title: Optional human-readable session title (e.g. "Sprint Planning")
        namespace: Session namespace (default: current session namespace)
    """
    app = _get_app(ctx)
    session_id = str(uuid4())
    effective_ns = namespace or app.current_namespace or "default"

    metadata = {"title": title} if title else {}
    await app.storage.create_session(session_id, agent_id, effective_ns, metadata=metadata)
    app.current_session_id = session_id

    lines = [f"Session started: {session_id}"]
    if title:
        lines.append(f"- Title: {title}")
    lines.append(f"- Agent: {agent_id}")
    lines.append(f"- Namespace: {effective_ns}")
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_end(
    summary: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """End the current episodic memory session.

    Closes the session, saves an optional summary, and records
    event statistics. Working memory bound to this session is cleaned up.

    Args:
        summary: Optional summary of what was accomplished in this session
    """
    app = _get_app(ctx)

    if not app.current_session_id:
        return "No active session."

    session_id = app.current_session_id

    # Gather session stats
    events = await app.storage.get_session_events(session_id)
    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

    await app.storage.end_session(session_id, summary, {"event_counts": event_counts})

    # Cleanup session-bound working memory
    cleaned = await app.storage.scratch_cleanup(session_id)

    app.current_session_id = None

    lines = [
        f"Session ended: {session_id}",
        f"- Events: {len(events)} ({', '.join(f'{k}:{v}' for k, v in event_counts.items())})",
    ]
    if summary:
        lines.append(f"- Summary: {summary[:100]}...")
    if cleaned:
        lines.append(f"- Working memory cleaned: {cleaned} entries")
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_list(
    agent_id: str | None = None,
    since: str | None = None,
    limit: int = 10,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """List recent episodic memory sessions.

    Args:
        agent_id: Filter by agent (omit for all agents)
        since: Only sessions started after this date (YYYY-MM-DD or ISO)
        limit: Maximum sessions to return (default 10)
    """
    app = _get_app(ctx)
    sessions = await app.storage.list_sessions(agent_id=agent_id, since=since, limit=limit)

    if not sessions:
        return "No sessions found."

    lines = [f"Sessions: {len(sessions)}\n"]
    for s in sessions:
        status = "active" if s["ended_at"] is None else "ended"
        meta = s.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        title = meta.get("title", "")
        label = f' "{title}"' if title else ""
        summary = f" — {s['summary'][:60]}..." if s.get("summary") else ""
        lines.append(
            f"  [{status}] {s['id'][:8]}...{label} ({s['agent_id']}) {s['started_at']}{summary}"
        )

    return "\n".join(lines)
