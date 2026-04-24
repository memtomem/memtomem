"""Tests for episodic memory (sessions)."""

import pytest

from memtomem.server.context import AppContext
from memtomem.server.tools.session import mem_session_end, mem_session_start


class _StubCtx:
    """Minimal stand-in for MCP ``Context`` so session tools can be invoked
    directly. Mirrors the helper in ``test_server_degraded_mode``.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


class TestSessions:
    @pytest.mark.asyncio
    async def test_create_and_list(self, storage):
        await storage.create_session("s1", "agent-a", "default")
        sessions = await storage.list_sessions(agent_id="agent-a")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"
        assert sessions[0]["ended_at"] is None

    @pytest.mark.asyncio
    async def test_end_session(self, storage):
        await storage.create_session("s2", "agent-b", "default")
        await storage.end_session("s2", "Done", {"event_counts": {"query": 1}})
        sessions = await storage.list_sessions(agent_id="agent-b")
        assert sessions[0]["ended_at"] is not None
        assert sessions[0]["summary"] == "Done"

    @pytest.mark.asyncio
    async def test_session_events(self, storage):
        await storage.create_session("s3", "agent-c", "default")
        await storage.add_session_event("s3", "query", "search for X")
        await storage.add_session_event("s3", "add", "added Y", ["chunk-1"])
        events = await storage.get_session_events("s3")
        assert len(events) == 2
        assert events[0]["event_type"] == "query"
        assert events[1]["chunk_ids"] == ["chunk-1"]

    @pytest.mark.asyncio
    async def test_duplicate_session_ignored(self, storage):
        await storage.create_session("dup", "agent", "ns1")
        await storage.create_session("dup", "other", "ns2")  # INSERT OR IGNORE
        sessions = await storage.list_sessions()
        dup = [s for s in sessions if s["id"] == "dup"]
        assert len(dup) == 1
        assert dup[0]["agent_id"] == "agent"

    @pytest.mark.asyncio
    async def test_list_with_since(self, storage):
        await storage.create_session("old", "agent", "default")
        sessions = await storage.list_sessions(since="2099-01-01")
        assert len(sessions) == 0


class TestSessionAgentInheritance:
    """``mem_session_start`` records ``agent_id`` on the AppContext so
    ``mem_agent_search`` can resolve the active agent without the caller
    repeating the identity on every tool call. Pins the state transitions
    documented in the multi-agent plan:

    * fresh start → ``current_session_id`` + ``current_agent_id`` set
    * second start while active → previous session **auto-ended**, new
      session takes over, ``current_agent_id`` replaced
    * ``mem_session_end`` → both fields reset to ``None``
    * ``mem_session_end`` with no active session → no-op
    """

    @pytest.mark.asyncio
    async def test_start_sets_current_agent_id(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id="planner", title="Sprint", ctx=ctx)  # type: ignore[arg-type]

        assert app.current_session_id is not None
        assert app.current_agent_id == "planner"
        assert "Session started" in out
        assert "- Agent: planner" in out

    @pytest.mark.asyncio
    async def test_second_start_auto_ends_previous(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        first_session = app.current_session_id
        assert first_session is not None

        out = await mem_session_start(agent_id="coder", ctx=ctx)  # type: ignore[arg-type]

        # New session replaces the old one
        assert app.current_session_id is not None
        assert app.current_session_id != first_session
        assert app.current_agent_id == "coder"
        # Inline notice surfaces the auto-end so callers are not surprised
        assert "auto-ended previous session" in out
        # And the storage row for the old session is closed
        rows = await app.storage.list_sessions()
        old_row = next(r for r in rows if r["id"] == first_session)
        assert old_row["ended_at"] is not None
        assert "auto-ended" in (old_row.get("summary") or "")

    @pytest.mark.asyncio
    async def test_end_resets_both_fields(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        assert app.current_agent_id == "planner"

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        assert app.current_session_id is None
        assert app.current_agent_id is None
        assert "Session ended" in out

    @pytest.mark.asyncio
    async def test_end_with_no_active_session_is_noop(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        # Ensure no active session
        assert app.current_session_id is None
        assert app.current_agent_id is None

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        assert out == "No active session."
        # State unchanged
        assert app.current_session_id is None
        assert app.current_agent_id is None

    @pytest.mark.asyncio
    async def test_session_lock_is_distinct_from_config_lock(self, components):
        """Session state mutations must use ``_session_lock`` so a config
        write cannot block them, and vice versa. A simple identity check
        keeps the two locks from accidentally being aliased.
        """
        app = AppContext.from_components(components)
        assert app._session_lock is not app._config_lock
