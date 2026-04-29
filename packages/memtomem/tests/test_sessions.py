"""Tests for episodic memory (sessions)."""

from pathlib import Path

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

    @pytest.mark.asyncio
    async def test_find_stale_active_sessions(self, storage):
        """Backs ``mm session start --auto-end-stale``: only active rows
        with ``started_at < cutoff`` come back. Ended rows and recent
        active rows must be skipped so SessionStart hooks don't end
        in-flight work or double-end already-closed sessions."""
        # Active and old (started_at backdated below, after the row exists).
        await storage.create_session("stale-old", "agent", "default")
        # Active and recent — must be skipped.
        await storage.create_session("stale-recent", "agent", "default")
        # Ended — must be skipped regardless of started_at.
        await storage.create_session("stale-ended", "agent", "default")
        await storage.end_session("stale-ended", "manual", {})
        # Backdate one active row so it's older than the cutoff.
        db = storage._get_db()
        db.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00", "stale-old"),
        )
        db.commit()

        rows = await storage.find_stale_active_sessions("2025-01-01T00:00:00")
        ids = [r["id"] for r in rows]
        assert ids == ["stale-old"]


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


class TestSessionNamespaceDerivation:
    """``mem_session_start`` derives the session record's namespace from
    ``agent_id`` when the caller doesn't pass an explicit ``namespace=``.
    Mirrors the LangGraph adapter's ``MemtomemStore.start_agent_session``
    so MCP and Python entry points agree.

    Priority: explicit namespace > agent-runtime:<id> when agent_id is
    non-default > app.current_namespace > "default".
    """

    @pytest.mark.asyncio
    async def test_agent_id_auto_derives_namespace(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]

        assert "- Namespace: agent-runtime:planner" in out
        rows = await app.storage.list_sessions()
        active = next(r for r in rows if r["id"] == app.current_session_id)
        assert active["namespace"] == "agent-runtime:planner"

    @pytest.mark.asyncio
    async def test_explicit_namespace_wins_over_agent_id(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id="planner", namespace="custom-ns", ctx=ctx)  # type: ignore[arg-type]

        assert "- Namespace: custom-ns" in out
        rows = await app.storage.list_sessions()
        active = next(r for r in rows if r["id"] == app.current_session_id)
        assert active["namespace"] == "custom-ns"

    @pytest.mark.asyncio
    async def test_default_agent_id_does_not_auto_derive(self, components):
        """Backward compat: callers that don't pass ``agent_id`` (it
        defaults to ``"default"``) keep the legacy namespace behavior so
        pre-multi-agent workflows are unchanged.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(ctx=ctx)  # type: ignore[arg-type]

        assert "- Namespace: default" in out
        rows = await app.storage.list_sessions()
        active = next(r for r in rows if r["id"] == app.current_session_id)
        assert active["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_agent_id_beats_current_namespace(self, components):
        """When both ``agent_id`` and ``app.current_namespace`` could
        supply a value, ``agent_id`` (priority 2) wins over
        ``current_namespace`` (priority 3).
        """
        app = AppContext.from_components(components)
        app.current_namespace = "legacy-ns"
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]

        assert "- Namespace: agent-runtime:planner" in out
        rows = await app.storage.list_sessions()
        active = next(r for r in rows if r["id"] == app.current_session_id)
        assert active["namespace"] == "agent-runtime:planner"


class TestSessionSummaryPhaseA:
    """Phase A of the episodic-session-summary RFC: an explicit
    ``summary=`` argument to ``mem_session_end`` is promoted to a
    first-class chunk under ``archive:session:<session_id>``. The
    chunk is hidden from default ``mem_search`` via the ``archive:``
    system prefix; LLM auto-summarization is Phase B and not exercised
    here.
    """

    @pytest.mark.asyncio
    async def test_summary_persists_archive_chunk(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        assert session_id is not None

        out = await mem_session_end(  # type: ignore[arg-type]
            summary="explored the auth flow", ctx=ctx
        )

        assert f"archive:session:{session_id}" in out

        base = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        files = list((base / "sessions").rglob(f"{session_id}.md"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        assert "explored the auth flow" in body
        assert "session-summary" in body
        assert session_id in body

    @pytest.mark.asyncio
    async def test_no_summary_skips_chunk(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        assert session_id is not None

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
        assert "archive:session:" not in out

        base = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        sessions_dir = base / "sessions"
        if sessions_dir.exists():
            files = list(sessions_dir.rglob(f"{session_id}.md"))
            assert not files

    @pytest.mark.asyncio
    async def test_summary_chunk_hidden_from_default_search(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]

        await mem_session_end(  # type: ignore[arg-type]
            summary="distinctive phrase for archive search filter test",
            ctx=ctx,
        )

        results, _ = await app.search_pipeline.search("distinctive phrase", top_k=10)
        assert all(not (r.chunk.namespace or "").startswith("archive:session:") for r in results), (
            "archive:session:* chunks must not appear in default mem_search"
        )


class _FakeLLM:
    """Minimal ``LLMProvider`` stand-in for Phase B tests.

    Records every ``generate`` call and returns a fixed response so the
    archive chunk content is predictable. ``close`` is a no-op because
    the real provider close runs over a network client that is not
    instantiated here.
    """

    def __init__(self, response: str = "AUTO-SUMMARY-OUTPUT") -> None:
        self.response = response
        self.calls: list[tuple[str, str, int]] = []

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        self.calls.append((prompt, system, max_tokens))
        return self.response

    async def close(self) -> None:
        return None


class TestSessionSummaryPhaseB:
    """Phase B of the episodic-session-summary RFC: when ``summary=`` is
    omitted on ``mem_session_end``, the server runs an LLM auto-summary
    over chunks added during the session and persists the result through
    Phase A's archive-chunk path. The auto path must also gate cleanly
    when prerequisites are missing (no LLM, below ``min_chunks``,
    oversize, ``auto=False``).
    """

    @staticmethod
    def _seed_chunks(memory_dir: Path, count: int, prefix: str = "auto") -> None:
        """Drop ``count`` markdown files into the configured memory dir.

        Uses one file per chunk so the chunker can't merge them into a
        single chunk and so each lands in storage with a fresh
        ``created_at`` after the session start.
        """
        for i in range(count):
            (memory_dir / f"{prefix}-{i:02d}.md").write_text(
                f"# Note {i}\n\nDistinct content body number {i}, words: alpha beta gamma {i}.\n",
                encoding="utf-8",
            )

    async def _index_dir(self, app: AppContext, memory_dir: Path, namespace: str) -> None:
        for path in sorted(memory_dir.glob("auto-*.md")):
            await app.index_engine.index_file(path, namespace=namespace)

    @pytest.mark.asyncio
    async def test_auto_summary_runs_when_threshold_met(self, components, monkeypatch):
        components.llm = _FakeLLM(response="The session set up alpha beta gamma notes.")
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        assert session_id is not None

        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        self._seed_chunks(memory_dir, count=6)
        await self._index_dir(app, memory_dir, namespace="agent-runtime:planner")

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        assert f"archive:session:{session_id}" in out
        assert "Auto summary:" in out
        assert components.llm.calls, "LLM provider should have been invoked"

        files = list((memory_dir / "sessions").rglob(f"{session_id}.md"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        assert "alpha beta gamma" in body

    @pytest.mark.asyncio
    async def test_auto_summary_skipped_below_min_chunks(self, components):
        components.llm = _FakeLLM()
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        self._seed_chunks(memory_dir, count=2)
        await self._index_dir(app, memory_dir, namespace="agent-runtime:planner")

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        assert "archive:session:" not in out
        assert "below min_chunks" in out
        assert not components.llm.calls

    @pytest.mark.asyncio
    async def test_auto_summary_skipped_when_no_llm(self, components):
        components.llm = None
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        TestSessionSummaryPhaseB._seed_chunks(memory_dir, count=6)
        await TestSessionSummaryPhaseB()._index_dir(
            app, memory_dir, namespace="agent-runtime:planner"
        )

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
        assert "archive:session:" not in out
        assert "no llm" in out

    @pytest.mark.asyncio
    async def test_auto_summary_disabled_via_config(self, components):
        components.llm = _FakeLLM()
        components.config.session_summary.auto = False
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        TestSessionSummaryPhaseB._seed_chunks(memory_dir, count=6)
        await TestSessionSummaryPhaseB()._index_dir(
            app, memory_dir, namespace="agent-runtime:planner"
        )

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
        assert "archive:session:" not in out
        assert "disabled" in out
        assert not components.llm.calls

    @pytest.mark.asyncio
    async def test_auto_summary_skipped_when_oversize(self, components):
        components.llm = _FakeLLM()
        components.config.session_summary.max_input_chars = 10  # force overflow
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        TestSessionSummaryPhaseB._seed_chunks(memory_dir, count=6)
        await TestSessionSummaryPhaseB()._index_dir(
            app, memory_dir, namespace="agent-runtime:planner"
        )

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
        assert "archive:session:" not in out
        assert "too large" in out
        assert not components.llm.calls, "oversize check must short-circuit before generate()"

    @pytest.mark.asyncio
    async def test_explicit_summary_skips_auto_path(self, components):
        components.llm = _FakeLLM(response="WOULD-NOT-SHOW")
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        out = await mem_session_end(  # type: ignore[arg-type]
            summary="manual override", ctx=ctx
        )
        assert "manual override" in out
        assert "WOULD-NOT-SHOW" not in out
        assert not components.llm.calls

    @pytest.mark.asyncio
    async def test_summary_links_written_for_auto_path(self, components):
        """Phase B-2: auto path writes ``link_type='summarizes'`` rows
        from the summary chunk back to each source chunk it summarized.
        """
        components.llm = _FakeLLM(response="auto-generated summary text")
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        TestSessionSummaryPhaseB._seed_chunks(memory_dir, count=6)
        await TestSessionSummaryPhaseB()._index_dir(
            app, memory_dir, namespace="agent-runtime:planner"
        )

        source_chunks = await app.storage.recall_chunks(limit=100)
        source_ids = {
            c.id for c in source_chunks if c.metadata.namespace == "agent-runtime:planner"
        }
        assert len(source_ids) >= 5

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        # Each source chunk should have a (target_id, "summarizes") row
        # whose source_id points at a single common summary chunk.
        link_sources: set = set()
        linked_targets: set = set()
        for tid in source_ids:
            link = await app.storage.get_chunk_link(tid, link_type="summarizes")
            if link is not None:
                link_sources.add(link.source_id)
                linked_targets.add(tid)

        assert linked_targets == source_ids, "every source chunk should have a summarizes link"
        assert len(link_sources) == 1, "all links should share one summary chunk_id"

    @pytest.mark.asyncio
    async def test_summary_links_respect_cap(self, components):
        """Phase B-2: ``max_summary_links`` caps fanout — newest first,
        tail dropped. With 6 source chunks and cap=3, exactly 3 links
        land (and they correspond to the 3 newest chunks, since
        ``recall_chunks`` returns newest-first).
        """
        components.llm = _FakeLLM(response="cap test summary")
        components.config.session_summary.max_summary_links = 3
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        TestSessionSummaryPhaseB._seed_chunks(memory_dir, count=6)
        await TestSessionSummaryPhaseB()._index_dir(
            app, memory_dir, namespace="agent-runtime:planner"
        )

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        # Verify cap count AND that linked chunks are a subset of the
        # session's source chunks (catches a bug that would link
        # arbitrary chunks not in the recall_chunks output). We can't
        # assert "exactly newest 3" because tests seed all 6 chunks in
        # the same second, and ORDER BY created_at DESC has no stable
        # secondary sort across two queries when timestamps tie — in
        # production a session spans real time so the tail-drop
        # ordering is well-defined.
        planner_ids = {
            c.id
            for c in await app.storage.recall_chunks(limit=100)
            if c.metadata.namespace == "agent-runtime:planner"
        }
        linked_ids = {
            cid
            for cid in planner_ids
            if await app.storage.get_chunk_link(cid, link_type="summarizes") is not None
        }
        assert len(linked_ids) == 3, f"expected cap=3 links, got {len(linked_ids)}"
        assert linked_ids.issubset(planner_ids), (
            "linked targets must come from the session's source chunks"
        )

    @pytest.mark.asyncio
    async def test_summary_links_skipped_for_manual_summary(self, components):
        """Manual ``summary=`` does not collect source chunks, so the
        Phase B-2 link-writer must not run (no fake links pointing
        from the archive chunk to arbitrary unrelated chunks).
        """
        components.llm = _FakeLLM()
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        memory_dir = Path(app.config.indexing.memory_dirs[0]).expanduser().resolve()
        TestSessionSummaryPhaseB._seed_chunks(memory_dir, count=6)
        await TestSessionSummaryPhaseB()._index_dir(
            app, memory_dir, namespace="agent-runtime:planner"
        )

        await mem_session_end(summary="manual", ctx=ctx)  # type: ignore[arg-type]

        source_chunks = await app.storage.recall_chunks(limit=100)
        for c in source_chunks:
            if c.metadata.namespace != "agent-runtime:planner":
                continue
            link = await app.storage.get_chunk_link(c.id, link_type="summarizes")
            assert link is None, "manual summary path must not write summarizes links"
