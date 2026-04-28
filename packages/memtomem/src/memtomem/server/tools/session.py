"""Tools: mem_session_start, mem_session_end, mem_session_list."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from memtomem.constants import AGENT_NAMESPACE_PREFIX, validate_agent_id, validate_namespace
from memtomem.models import NamespaceFilter
from memtomem.server import mcp
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.summarization import SessionTooLargeError, summarize_session

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
       Run through :func:`validate_namespace` so a hostile-shaped string
       like ``"agent-runtime:foo:bar"`` cannot smuggle past the
       ``agent_id`` gate via the override; see issue #496.
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
    validate_agent_id(agent_id)
    if namespace is not None:
        validate_namespace(namespace)
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

    When ``summary`` is provided, the text is also promoted to a
    first-class chunk under ``archive:session:<session_id>`` (Phase A
    of the episodic-session-summary RFC). The chunk is hidden from
    default ``mem_search`` via the ``archive:`` system prefix and
    surfaces only under explicit ``namespace_filter``. Persisting the
    chunk is best-effort: a failure is logged but does not roll back
    the session-end DB write.

    When ``summary`` is omitted, Phase B's auto path runs: if the
    server has an LLM provider configured, ``session_summary.auto`` is
    True, and the session collected at least
    ``session_summary.min_chunks`` chunks in its namespace since
    ``started_at``, the server asks the LLM for a short narrative
    summary and persists it through the same archive-chunk path.
    Sessions whose serialized chunk body exceeds
    ``session_summary.max_input_chars`` skip the auto path with a
    log warning (callers can pass an explicit ``summary=`` instead).

    Args:
        summary: Optional summary of what was accomplished in this
            session. When provided, also written as a chunk under
            ``<memory_dir>/sessions/<YYYY-MM>/<session_id>.md``.
    """
    app = await _get_app_initialized(ctx)

    if not app.current_session_id:
        return "No active session."

    session_id = app.current_session_id
    # Capture before end_session and the lock-guarded reset below; the
    # archive helper runs after end_session, so any later state
    # mutation could clobber the tag we want to record.
    agent_id = app.current_agent_id

    # Gather session stats
    events = await app.storage.get_session_events(session_id)
    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

    # Read the session row before end_session writes ended_at — we need
    # ``started_at`` and ``namespace`` for the Phase B auto-summary
    # chunk lookup. Tolerate missing rows defensively (the row was
    # created in mem_session_start, so absence indicates external
    # tampering or a backend bug; either way, fall back to skipping
    # auto-summary rather than crashing the close path).
    session_row = await app.storage.get_session(session_id)

    await app.storage.end_session(session_id, summary, {"event_counts": event_counts})

    effective_summary = summary
    auto_summary_skip_reason: str | None = None
    if not summary:
        effective_summary, auto_summary_skip_reason = await _maybe_auto_summarize(
            app,
            session_id=session_id,
            session_row=session_row,
        )

    summary_chunk_line: str | None = None
    if effective_summary:
        try:
            summary_chunk_line = await _persist_session_summary_chunk(
                app,
                session_id=session_id,
                agent_id=agent_id,
                summary=effective_summary,
                event_counts=event_counts,
            )
        except Exception:
            logger.warning(
                "session_summary_chunk_persist_failed session_id=%s",
                session_id,
                exc_info=True,
            )

    # Cleanup session-bound working memory
    cleaned = await app.storage.scratch_cleanup(session_id)

    async with app._session_lock:
        app.current_session_id = None
        app.current_agent_id = None

    lines = [
        f"Session ended: {session_id}",
        f"- Events: {len(events)} ({', '.join(f'{k}:{v}' for k, v in event_counts.items())})",
    ]
    if effective_summary:
        prefix = "Summary" if summary else "Auto summary"
        lines.append(f"- {prefix}: {effective_summary[:100]}...")
    elif auto_summary_skip_reason:
        lines.append(f"- Auto summary: skipped ({auto_summary_skip_reason})")
    if summary_chunk_line:
        lines.append(summary_chunk_line)
    if cleaned:
        lines.append(f"- Working memory cleaned: {cleaned} entries")
    return "\n".join(lines)


async def _maybe_auto_summarize(
    app: AppContext,
    *,
    session_id: str,
    session_row: dict | None,
) -> tuple[str | None, str | None]:
    """Run the Phase B auto-summary path when prerequisites are met.

    Returns ``(summary_text, skip_reason)``. When the auto path
    produced text, ``skip_reason`` is ``None``. When the path was
    skipped, ``summary_text`` is ``None`` and ``skip_reason`` carries
    a short label suitable for the tool response (``"disabled"``,
    ``"no llm"``, ``"no session row"``, ``"no started_at"``,
    ``"below min_chunks"``, ``"too large"``, ``"empty output"``, or
    ``"llm error"``).

    Failures inside the LLM call are caught and surfaced as
    ``"llm error"`` so a misconfigured provider does not block
    ``mem_session_end`` from completing.
    """
    cfg = app.config.session_summary
    if not cfg.auto:
        return None, "disabled"

    llm = app.llm_provider
    if llm is None:
        return None, "no llm"

    if session_row is None:
        return None, "no session row"

    started_at_str = session_row.get("started_at")
    namespace = session_row.get("namespace") or "default"
    if not started_at_str:
        return None, "no started_at"

    try:
        started_at = datetime.fromisoformat(started_at_str)
    except ValueError:
        logger.warning(
            "auto_summary_invalid_started_at session_id=%s value=%r",
            session_id,
            started_at_str,
        )
        return None, "no started_at"
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    ns_filter = NamespaceFilter(namespaces=(namespace,))
    chunks = await app.storage.recall_chunks(
        since=started_at,
        namespace_filter=ns_filter,
        limit=max(cfg.min_chunks * 4, 200),
    )
    if len(chunks) < cfg.min_chunks:
        return None, "below min_chunks"

    try:
        summary = await summarize_session(
            session_id,
            chunks,
            llm=llm,
            max_tokens=cfg.max_summary_tokens,
            max_input_chars=cfg.max_input_chars,
        )
    except SessionTooLargeError as exc:
        logger.info("auto_summary_skipped session_id=%s reason=%s", session_id, exc)
        return None, "too large"
    except Exception:
        logger.warning(
            "auto_summary_llm_failed session_id=%s",
            session_id,
            exc_info=True,
        )
        return None, "llm error"

    if not summary:
        return None, "empty output"
    return summary, None


async def _persist_session_summary_chunk(
    app: AppContext,
    *,
    session_id: str,
    agent_id: str | None,
    summary: str,
    event_counts: dict[str, int],
) -> str | None:
    """Promote a session summary to a first-class chunk.

    Writes the markdown file at
    ``<memory_dir>/sessions/<YYYY-MM>/<session_id>.md`` and indexes it
    under ``archive:session:<session_id>``. The ``archive:`` prefix is
    a default system namespace, so the chunk is hidden from
    ``mem_search`` unless the caller passes an explicit
    ``namespace_filter``. Returns a single-line status for the tool
    response, or ``None`` when no memory directory is configured or
    when the indexer rejected the file (zero chunks).
    """
    memory_dirs = app.config.indexing.memory_dirs
    if not memory_dirs:
        return None

    # Validate the derived namespace before doing any I/O so an invalid
    # session_id (defensive — uuid4 is safe in practice) can't leave a
    # half-written file behind.
    namespace = f"archive:session:{session_id}"
    validate_namespace(namespace)

    # Primary memory dir: when multiple are configured, summaries land
    # under the first one. Keeps the location predictable across runs;
    # users with multi-dir setups can re-home via memory_dirs ordering.
    base = Path(memory_dirs[0]).expanduser().resolve()
    now = datetime.now(timezone.utc)
    target = base / "sessions" / now.strftime("%Y-%m") / f"{session_id}.md"

    event_total = sum(event_counts.values())
    content = _format_session_summary(
        session_id=session_id,
        agent_id=agent_id,
        ended_at=now,
        event_total=event_total,
        summary=summary,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, content, "utf-8")

    stats = await app.index_engine.index_file(target, namespace=namespace)
    app.search_pipeline.invalidate_cache()

    if not stats.indexed_chunks:
        # File written but indexer produced no chunks (empty body, dedup
        # collision, or a chunker reject). Surface the path so an
        # operator can investigate; suppress the misleading "0 chunks"
        # status line in the tool response.
        logger.warning(
            "session_summary_chunk_indexed_zero session_id=%s path=%s",
            session_id,
            target,
        )
        return None

    return f"- Summary chunk: {namespace} ({stats.indexed_chunks} chunks)"


def _format_session_summary(
    *,
    session_id: str,
    agent_id: str | None,
    ended_at: datetime,
    event_total: int,
    summary: str,
) -> str:
    """Render the session-summary markdown body.

    Layout: YAML frontmatter (session_id / agent_id / ended_at /
    event_count) → ``## Session summary: <id>`` heading → blockquote
    tags (``session-summary`` plus ``agent=<id>`` when an agent owned
    the session) → summary body. Matches the chunker's expected entry
    shape (heading + blockquote group + body) so both frontmatter and
    per-section tags promote cleanly to ``ChunkMetadata.tags``.

    ``agent_id`` is preserved as ``None`` rather than coerced to
    ``"default"``: collapsing to a literal would mask sessions that
    truly had no agent owner behind the legitimate ``default`` agent
    convention.
    """
    iso_ts = ended_at.isoformat(timespec="seconds")
    tag_list = ["session-summary"]
    if agent_id:
        tag_list.append(f"agent={agent_id}")
    tags_json = json.dumps(tag_list)
    fm_agent = agent_id if agent_id else "null"
    body = summary.strip()
    return (
        f"---\n"
        f"session_id: {session_id}\n"
        f"agent_id: {fm_agent}\n"
        f"ended_at: {iso_ts}\n"
        f"event_count: {event_total}\n"
        f"---\n"
        f"\n"
        f"## Session summary: {session_id}\n"
        f"\n"
        f"> tags: {tags_json}\n"
        f"\n"
        f"{body}\n"
    )


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
