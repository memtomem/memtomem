"""Tool-level threading tests for the per-call ``record`` switch (#2166).

Pipeline-level behavior — what ``record=False`` suppresses and how the
caches stay isolated — is covered in ``test_pipeline_replay_mode.py``. This
file pins the plumbing above it: ``mem_search`` / ``mem_agent_search`` /
``mem_do(action="agent_search")`` must deliver the caller's decision to
``SearchPipeline.search`` unchanged and default to recording, and one
end-to-end case proves the switch still means "writes nothing" once it has
travelled the whole MCP surface into real storage.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.search.pipeline import RetrievalStats
from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import mem_agent_search
from memtomem.server.tools.search import mem_search

from helpers import StubCtx, make_chunk


def _fake_app() -> MagicMock:
    app = MagicMock()
    app.search_pipeline.search = AsyncMock(return_value=([], RetrievalStats()))
    app.current_namespace = None
    app.webhook_manager = None
    return app


def _patch_search_tool(m, app) -> None:
    m.setattr("memtomem.server.tools.search._get_app_initialized", AsyncMock(return_value=app))
    m.setattr(
        "memtomem.server.tools.search._announce_dim_mismatch_once", AsyncMock(return_value=None)
    )


async def _drain_bg(pipeline) -> None:
    """Access counters are incremented off a background task (#1802)."""
    if pipeline._bg_tasks:
        await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)


@pytest.mark.asyncio
class TestMemSearchRecordParam:
    async def test_record_false_reaches_pipeline(self):
        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            _patch_search_tool(m, app)
            await mem_search(query="hello", record=False, ctx=SimpleNamespace())

        assert app.search_pipeline.search.await_args.kwargs["record"] is False

    async def test_record_omitted_defaults_to_recording(self):
        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            _patch_search_tool(m, app)
            await mem_search(query="hello", ctx=SimpleNamespace())

        assert app.search_pipeline.search.await_args.kwargs["record"] is True


@pytest.mark.asyncio
class TestMemAgentSearchRecordParam:
    """The default matches ``mem_search``: a fan-out tool still records.

    #2086 restored this tool's search observations; a ``False`` default here
    would turn them back off for every caller that never asked.
    """

    async def test_record_false_reaches_pipeline(self):
        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.multi_agent._get_app_initialized",
                AsyncMock(return_value=app),
            )
            await mem_agent_search(query="hello", record=False, ctx=SimpleNamespace())

        assert app.search_pipeline.search.await_args.kwargs["record"] is False

    async def test_record_omitted_defaults_to_recording(self):
        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.multi_agent._get_app_initialized",
                AsyncMock(return_value=app),
            )
            await mem_agent_search(query="hello", ctx=SimpleNamespace())

        assert app.search_pipeline.search.await_args.kwargs["record"] is True

    async def test_mem_do_routes_record_param(self):
        """In core mode this tool is reachable only through the dispatcher."""
        from memtomem.server.tools.meta import mem_do

        app = _fake_app()
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.multi_agent._get_app_initialized",
                AsyncMock(return_value=app),
            )
            result = await mem_do("agent_search", params={"query": "hello", "record": False})

        assert app.search_pipeline.search.await_args.kwargs["record"] is False
        assert "Error" not in str(result)[:40]

    async def test_the_dispatcher_route_refuses_and_runs_no_search(self):
        """#2296 on the fourth surface, and the *normal* one in core mode.

        ``mem_do`` invokes the registered function directly, so the refusal
        travels to its outer ``tool_handler`` rather than the one on the tool.
        That renders it correctly today by virtue of the exception's base
        class; nothing pinned it, and "correct by accident" is how the other
        three surfaces would have drifted apart too.
        """
        from memtomem.server.tools.meta import mem_do

        app = _fake_app()
        app.current_agent_id = None
        app.current_namespace = None
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "memtomem.server.tools.multi_agent._get_app_initialized",
                AsyncMock(return_value=app),
            )
            result = await mem_do(
                "agent_search", params={"query": "hello", "include_shared": False}
            )

        # The refusal reaches the caller as a rendered error, not as an
        # "internal error" wrapper and not as an empty result set.
        assert "include_shared=False needs a resolved agent" in str(result)
        assert "internal error" not in str(result)
        # And retrieval never ran, which is what keeps a shared row out of the
        # answer regardless of what default visibility does.
        app.search_pipeline.search.assert_not_awaited()


class TestEndToEndNoWrites:
    """The DoD: ``record=false`` at the MCP surface mutates no stored state."""

    @pytest.mark.asyncio
    async def test_mem_search_record_false_writes_nothing(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        ctx = StubCtx(AppContext.from_components(comp))

        chunks = [make_chunk("fan-out marker body", source=f"f{i}.md") for i in range(3)]
        await storage.upsert_chunks(chunks)
        ids = [c.id for c in chunks]
        before = await storage.get_access_counts(ids)

        out = await mem_search(query="fan-out", record=False, ctx=ctx)  # type: ignore[arg-type]
        await _drain_bg(pipeline)

        assert "fan-out marker body" in out, "sanity: the read itself still works"
        assert await storage.get_access_counts(ids) == before
        assert await storage.get_search_runs() == []
        assert pipeline._search_cache == {}

    @pytest.mark.asyncio
    async def test_the_default_still_records(self, bm25_only_components):
        """Guards the direction of the previous test: it is the flag, not the fixture."""
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        ctx = StubCtx(AppContext.from_components(comp))

        chunks = [make_chunk("recorded marker body", source=f"r{i}.md") for i in range(3)]
        await storage.upsert_chunks(chunks)
        ids = [c.id for c in chunks]

        await mem_search(query="recorded", ctx=ctx)  # type: ignore[arg-type]
        await _drain_bg(pipeline)

        counts = await storage.get_access_counts(ids)
        assert all(counts.get(str(cid), 0) >= 1 for cid in ids)
        assert len(await storage.get_search_runs()) == 1


class TestMemAddDuplicateCheckWritesNothing:
    """#2683: ``mem_add``'s duplicate check is an internal read, not a search.

    It searches for the note it just wrote; recorded, that bumped the access
    counts of every hit (the new note included) and stored the note body as
    a query.
    """

    @pytest.mark.asyncio
    async def test_mem_add_leaves_access_counts_and_runs_alone(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        ctx = StubCtx(AppContext.from_components(comp))

        # The check swallows its own failures, so prove it actually ran: a
        # check that raised, or was skipped, would write nothing either.
        completed: list[dict] = []
        real_search = pipeline.search

        async def spy(*args, **kwargs):
            result = await real_search(*args, **kwargs)
            completed.append(kwargs)
            return result

        monkeypatch.setattr(pipeline, "search", spy)

        await storage.upsert_chunks(
            [make_chunk(f"duplicate check marker body {i}", source=f"d{i}.md") for i in range(3)]
        )

        note = "duplicate check marker body"
        out = await mem_add(content=note, ctx=ctx)  # type: ignore[arg-type]
        await _drain_bg(pipeline)

        assert "Error" not in out, out
        assert [kw.get("record") for kw in completed] == [False]
        # Sanity: the check's own query hits the seeds and the new note, so a
        # recorded check would have had four chunks to count.
        hits, _ = await real_search(note, top_k=5, record=False)
        assert len(hits) >= 4, hits
        accessed = (
            storage._get_read_db()
            .execute("SELECT COUNT(*) FROM chunks WHERE access_count > 0")
            .fetchone()[0]
        )
        assert accessed == 0
        assert await storage.get_search_runs() == []


class TestProcedureListWritesNothing:
    """#2691: listing procedures runs a fixed query, which nobody searched for.

    Recorded, each listing bumped the access counts of every procedure and
    stored ``"procedure workflow steps"`` as a query — the second listing too,
    though the result cache spared its counters.
    """

    @pytest.mark.asyncio
    async def test_listing_twice_leaves_access_counts_and_runs_alone(self, bm25_only_components):
        from memtomem.server.tools.procedure import mem_procedure_list

        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        ctx = StubCtx(AppContext.from_components(comp))

        await storage.upsert_chunks(
            [
                make_chunk(
                    f"deploy procedure workflow steps {i}", source=f"p{i}.md", tags=("procedure",)
                )
                for i in range(3)
            ]
        )

        for _ in range(2):
            out = await mem_procedure_list(ctx=ctx)  # type: ignore[arg-type]
            await _drain_bg(pipeline)
            assert out.startswith("Procedures: 3 found"), out

        accessed = (
            storage._get_read_db()
            .execute("SELECT COUNT(*) FROM chunks WHERE access_count > 0")
            .fetchone()[0]
        )
        assert accessed == 0
        assert await storage.get_search_runs() == []


class TestRecordFalseKeepsTheDenseLeg:
    """#2671: above the KNN cap, ``record=false`` used to be keyword-only."""

    @pytest.mark.asyncio
    async def test_mem_search_record_false_reports_a_dense_leg(self, components, monkeypatch):
        from memtomem.storage import sqlite_backend

        class _Embedder:
            dimension = 1024
            model_name = "fake"

            async def embed_query(self, query: str) -> list[float]:
                return [0.1] * 1024

        storage, pipeline = components.storage, components.search_pipeline
        pipeline._embedder = _Embedder()
        monkeypatch.setattr(sqlite_backend, "VEC_MAX_KNN_K", 3)
        await storage.upsert_chunks(
            [
                make_chunk(f"cap marker {i}", source=f"k{i}.md", embedding=[0.1] * 1024)
                for i in range(5)
            ]
        )
        ctx = StubCtx(AppContext.from_components(components))

        out = await mem_search(
            query="cap marker",
            record=False,
            output_format="verbose",
            ctx=ctx,  # type: ignore[arg-type]
        )
        await _drain_bg(pipeline)

        pipeline_line = out.rsplit("pipeline: ", 1)[1].splitlines()[0]
        stages = [stage.split(":", 1)[0] for stage in pipeline_line.split(" → ")]
        assert "Dense" in stages, pipeline_line
        assert "Dense-err" not in stages, pipeline_line
