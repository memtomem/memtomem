"""Tools: mem_session_start, mem_session_end, mem_session_list."""

from __future__ import annotations

import logging
from uuid import uuid4

from memtomem.constants import AGENT_NAMESPACE_PREFIX
from memtomem.server import mcp
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


async def _end_active_session_inline(app: AppContext, reason: str) -> str | None:
    """End the currently-active session without resetting ``current_*`` state.

    Returns a one-line warning describing what was rolled forward, or
    ``None`` when no session was active. Caller is responsible for
    resetting ``current_session_id`` / ``current_agent_id`` afterwards
    (typically by overwriting them as part of a fresh session start).
    """

    session_id = app.current_session_id
    if not session_id:
        return None

    events = await app.storage.get_session_events(session_id)
    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

    await app.storage.end_session(
        session_id,
        f"[auto-ended: {reason}]",
        {"event_counts": event_counts, "auto_ended": True},
    )
    await app.storage.scratch_cleanup(session_id)
    logger.warning(
        "mem_session_start auto-ended previous session %s (%s events) — %s",
        session_id,
        len(events),
        reason,
    )
    return f"(auto-ended previous session {session_id[:8]}... — {reason})"


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_start(
    agent_id: str = "default",
    title: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Start a new episodic memory session.

    Creates a session record and sets it as the current session. All
    subsequent tool calls will be tracked as session events. The
    ``agent_id`` is recorded on ``AppContext.current_agent_id`` so that
    multi-agent tools (``mem_agent_search`` and friends) can resolve the
    active agent without the caller passing it on every call.

    State transitions:

    * No active session → records the new session, sets
      ``current_session_id`` and ``current_agent_id``.
    * Active session present → the previous session is **auto-ended**
      (with a warning logged and an inline notice in the return string)
      and the new session takes its place. The previous ``agent_id`` is
      replaced by the new one — agents do not stack.

    Namespace derivation priority (matches the LangGraph adapter
    ``MemtomemStore.start_agent_session`` so MCP and Python entry points
    behave the same):

    1. Explicit ``namespace=`` argument (escape hatch — wins everything).
    2. ``agent-runtime:<agent_id>`` when ``agent_id`` is non-default.
       This is the common case for multi-agent workflows.
    3. ``app.current_namespace`` (pre-multi-agent fallback).
    4. ``"default"``.

    Only the **session record's** namespace is derived; ``mem_add`` /
    ``mem_search`` without explicit ``namespace=`` still consult
    ``app.current_namespace`` as before. Namespace and agent_id remain
    separate axes on ``AppContext``.

    Args:
        agent_id: Identifier for the agent starting the session
        title: Optional human-readable session title (e.g. "Sprint Planning")
        namespace: Session namespace. When omitted and ``agent_id`` is
            non-default, defaults to ``agent-runtime:<agent_id>``.
    """
    app = await _get_app_initialized(ctx)
    session_id = str(uuid4())
    if namespace:
        effective_ns = namespace
    elif agent_id and agent_id != "default":
        effective_ns = f"{AGENT_NAMESPACE_PREFIX}{agent_id}"
    elif app.current_namespace:
        effective_ns = app.current_namespace
    else:
        effective_ns = "default"

    auto_end_notice: str | None = None
    async with app._session_lock:
        if app.current_session_id:
            auto_end_notice = await _end_active_session_inline(
                app, reason="superseded by new mem_session_start"
            )

        metadata = {"title": title} if title else {}
        await app.storage.create_session(session_id, agent_id, effective_ns, metadata=metadata)
        app.current_session_id = session_id
        app.current_agent_id = agent_id

    lines = [f"Session started: {session_id}"]
    if auto_end_notice:
        lines.append(auto_end_notice)
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
    ctx: CtxType = None,
) -> str:
    """End the current episodic memory session.

    Closes the session, saves an optional summary, and records event
    statistics. Working memory bound to this session is cleaned up.
    Resets both ``current_session_id`` and ``current_agent_id`` so the
    next ``mem_agent_search`` falls back to ``current_namespace``.

    Args:
        summary: Optional summary of what was accomplished in this session
    """
    app = await _get_app_initialized(ctx)

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

    async with app._session_lock:
        app.current_session_id = None
        app.current_agent_id = None

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
    ctx: CtxType = None,
) -> str:
    """List recent episodic memory sessions.

    Args:
        agent_id: Filter by agent (omit for all agents)
        since: Only sessions started after this date (YYYY-MM-DD or ISO)
        limit: Maximum sessions to return (default 10)
    """
    if not 1 <= limit <= 200:
        return f"Error: limit must be between 1 and 200, got {limit}."

    app = await _get_app_initialized(ctx)
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
