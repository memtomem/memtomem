"""Tests for analytics storage mixin methods."""

import logging

import pytest
from pathlib import Path
from memtomem.models import Chunk, ChunkMetadata

from helpers import StubCtx, make_chunk


def _make_chunk(components, content="test", tags=(), namespace="default"):
    dim = components.config.embedding.dimension
    return make_chunk(content=content, tags=tags, namespace=namespace, embedding=[0.0] * dim)


def _project_chunk(
    components, project: Path, content: str, tag: str, namespace: str = "default"
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=project / f"{content}.md",
            tags=(tag,),
            scope="project_shared",
            project_root=project,
            namespace=namespace,
        ),
        embedding=[0.0] * components.config.embedding.dimension,
    )


class TestHealthReport:
    @pytest.mark.asyncio
    async def test_empty_db(self, storage):
        report = await storage.get_health_report()
        assert report["total_chunks"] == 0
        assert report["access_coverage"]["pct"] == 0
        assert report["tag_coverage"]["pct"] == 0
        # No project-scoped answer exists for these rows (#2281).
        assert report["sessions"]["available"] is False
        assert report["sessions"]["total"] is None

    @pytest.mark.asyncio
    async def test_with_data(self, storage, components):
        chunk = _make_chunk(components, tags=("test",))
        await storage.upsert_chunks([chunk])
        # Increment access
        await storage.increment_access([chunk.id])

        report = await storage.get_health_report()
        assert report["total_chunks"] == 1
        assert report["access_coverage"]["accessed"] == 1
        assert report["access_coverage"]["pct"] == 100.0


class TestFrequentlyAccessed:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_frequently_accessed()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_accessed_chunks(self, storage, components):
        chunk = _make_chunk(components)
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])

        result = await storage.get_frequently_accessed(limit=5)
        assert len(result) == 1
        assert result[0]["total_access"] == 2

    @pytest.mark.asyncio
    async def test_namespace_filter(self, storage, components):
        c1 = _make_chunk(components, content="a", namespace="ns1")
        c2 = _make_chunk(components, content="b", namespace="ns2")
        await storage.upsert_chunks([c1, c2])
        await storage.increment_access([c1.id])
        await storage.increment_access([c2.id])

        result = await storage.get_frequently_accessed(namespace="ns1")
        assert len(result) == 1


class TestAgentSessions:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_agent_sessions()
        assert result == []

    @pytest.mark.asyncio
    async def test_with_sessions(self, storage):
        await storage.create_session("s1", "agent-a", "default")
        await storage.create_session("s2", "agent-a", "default")
        await storage.create_session("s3", "agent-b", "default")

        result = await storage.get_agent_sessions()
        assert len(result) == 2
        agent_a = next(r for r in result if r["agent_id"] == "agent-a")
        assert agent_a["session_count"] == 2


class TestKnowledgeGaps:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_knowledge_gaps()
        assert result == []

    @pytest.mark.asyncio
    async def test_queries_with_no_results(self, storage):
        await storage.save_query_history("missing topic", [], [], [])
        await storage.save_query_history("missing topic", [], [], [])
        await storage.save_query_history("found topic", [], ["id1"], [0.9])

        gaps = await storage.get_knowledge_gaps()
        assert len(gaps) == 1
        assert gaps[0]["query"] == "missing topic"
        assert gaps[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_db_error_logs_debug(self, storage, caplog):
        # Drop the query_history table to force the expected missing-table
        # case — the failure is logged (debug) and degrades to [] rather
        # than being silently swallowed.
        storage._get_db().execute("DROP TABLE IF EXISTS query_history")

        with caplog.at_level(logging.DEBUG, logger="memtomem.storage.mixins.analytics"):
            result = await storage.get_knowledge_gaps()

        assert result == []
        assert any(
            rec.levelno == logging.DEBUG and "query_history table missing" in rec.message
            for rec in caplog.records
        ), "Expected DEBUG log when query_history is missing (not fully silent)"

    @pytest.mark.asyncio
    async def test_real_query_bug_reraises(self, storage, monkeypatch):
        # A non-missing-table OperationalError (e.g. bad column / syntax)
        # must NOT be swallowed to [] — it's a regression, so re-raise.
        import sqlite3

        class _BadDB:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("no such column: bogus")

        monkeypatch.setattr(storage, "_get_db", lambda: _BadDB())
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            await storage.get_knowledge_gaps()


class TestActivitySummary:
    @pytest.mark.asyncio
    async def test_missing_access_log_logs_debug_and_degrades(self, storage, caplog):
        # Drop access_log to force the expected missing-table case. The
        # summary must still return (from created/updated) and the failure
        # must be logged at DEBUG rather than silently swallowed (#1613).
        storage._get_db().execute("DROP TABLE IF EXISTS access_log")

        with caplog.at_level(logging.DEBUG, logger="memtomem.storage.mixins.analytics"):
            result = await storage.get_activity_summary()

        assert isinstance(result, list)
        assert any(
            rec.levelno == logging.DEBUG and "access_log table missing" in rec.message
            for rec in caplog.records
        ), "Expected DEBUG log when access_log is missing (not fully silent)"


class TestMostConnected:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_most_connected()
        assert result == []

    @pytest.mark.asyncio
    async def test_with_relations(self, storage, components):
        c1 = _make_chunk(components, content="hub")
        c2 = _make_chunk(components, content="spoke1")
        c3 = _make_chunk(components, content="spoke2")
        await storage.upsert_chunks([c1, c2, c3])
        await storage.add_relation(c1.id, c2.id)
        await storage.add_relation(c1.id, c3.id)

        result = await storage.get_most_connected(limit=2)
        assert len(result) >= 1
        assert result[0]["link_count"] >= 2


class TestMostConnectedBoundary:
    """#2244 — the aggregate ranks inside the caller's boundary, not after it.

    Ranking by whole-store degree and screening the page afterwards lets hubs
    the caller cannot see consume the top-N slots, so a caller in a small
    project alongside a large one gets an empty section while having perfectly
    good hubs of its own. These pin the screen as happening *before* the cut.
    """

    PROJECT_A = Path("/workspace/project-a")
    PROJECT_B = Path("/workspace/project-b")

    async def _store(self, storage, components, chunks, edges):
        await storage.upsert_chunks(chunks)
        for source, target in edges:
            await storage.add_relation(source.id, target.id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [1, 2, 3, 5])
    async def test_own_hub_survives_foreign_hubs_that_dominate_raw_degree(
        self, storage, components, limit
    ):
        """The caller's hub is listed however far below the raw cut it sits.

        Six foreign hubs of degree six each outrank a single visible hub of
        degree one in whole-store order. An over-fetch factor makes this pass
        for *some* factor; screening before the LIMIT makes it pass for every
        one, which is what the issue asks for.
        """
        foreign = []
        for h in range(6):
            hub = _project_chunk(components, self.PROJECT_B, f"beta-hub-{h}", "beta")
            spokes = [
                _project_chunk(components, self.PROJECT_B, f"beta-spoke-{h}-{i}", "beta")
                for i in range(6)
            ]
            foreign.append((hub, spokes))
        mine = _project_chunk(components, self.PROJECT_A, "alpha-hub", "alpha")
        # Two spokes, so the hub is unambiguously the caller's top row rather
        # than tied with its own neighbour on the chunk_id tie-break.
        my_spokes = [
            _project_chunk(components, self.PROJECT_A, f"alpha-spoke-{i}", "alpha")
            for i in range(2)
        ]

        chunks = [mine, *my_spokes]
        edges = [(mine, spoke) for spoke in my_spokes]
        for hub, spokes in foreign:
            chunks.extend([hub, *spokes])
            edges.extend((hub, spoke) for spoke in spokes)
        await self._store(storage, components, chunks, edges)

        rows = await storage.get_most_connected(limit=limit, project_context_root=self.PROJECT_A)

        assert [row["chunk_id"] for row in rows][:1] == [str(mine.id)]
        assert rows[0]["link_count"] == 2
        foreign_ids = {str(hub.id) for hub, _ in foreign}
        assert not foreign_ids & {row["chunk_id"] for row in rows}

    @pytest.mark.asyncio
    async def test_link_count_is_the_visible_degree(self, storage, components):
        """Edges leaving the boundary are not counted, in either direction.

        Reporting the stored degree would say how many foreign neighbours a
        chunk has — the same existence leak as printing their ids, quieter.
        """
        hub = _project_chunk(components, self.PROJECT_A, "alpha-hub", "alpha")
        mine = _project_chunk(components, self.PROJECT_A, "alpha-spoke", "alpha")
        foreign = [
            _project_chunk(components, self.PROJECT_B, f"beta-{i}", "beta") for i in range(9)
        ]
        # Half the foreign edges point *at* the hub, so a one-sided screen on
        # ``source_id`` alone would still leak them into the count.
        edges = [(hub, mine)]
        edges += [(hub, f) for f in foreign[:5]]
        edges += [(f, hub) for f in foreign[5:]]
        await self._store(storage, components, [hub, mine, *foreign], edges)

        rows = await storage.get_most_connected(project_context_root=self.PROJECT_A)

        assert {row["chunk_id"]: row["link_count"] for row in rows} == {
            str(hub.id): 1,
            str(mine.id): 1,
        }

    @pytest.mark.asyncio
    async def test_hub_whose_every_edge_leaves_the_boundary_is_absent(self, storage, components):
        """Visible degree zero is absence, not a row reading ``0``.

        A zero-degree row would still say "this chunk is a hub, and everything
        it connects to is out of your reach".
        """
        hub = _project_chunk(components, self.PROJECT_A, "alpha-hub", "alpha")
        foreign = [
            _project_chunk(components, self.PROJECT_B, f"beta-{i}", "beta") for i in range(3)
        ]
        await self._store(storage, components, [hub, *foreign], [(hub, f) for f in foreign])

        rows = await storage.get_most_connected(project_context_root=self.PROJECT_A)

        assert rows == []

    @pytest.mark.asyncio
    async def test_ranking_uses_visible_degree_not_the_stored_one(self, storage, components):
        """A thin hub must not outrank a thick one on its hidden edges.

        Ten edges of which one is visible outranks nine visible ones in the
        store's own ordering, so cutting on that ordering both lists the wrong
        hub and lets the hidden edges choose it.
        """
        thin = _project_chunk(components, self.PROJECT_A, "thin-hub", "alpha")
        thick = _project_chunk(components, self.PROJECT_A, "thick-hub", "alpha")
        thin_spoke = _project_chunk(components, self.PROJECT_A, "thin-spoke", "alpha")
        thick_spokes = [
            _project_chunk(components, self.PROJECT_A, f"thick-spoke-{i}", "alpha")
            for i in range(3)
        ]
        foreign = [
            _project_chunk(components, self.PROJECT_B, f"beta-{i}", "beta") for i in range(9)
        ]
        edges = [(thin, thin_spoke)]
        edges += [(thin, f) for f in foreign]
        edges += [(thick, spoke) for spoke in thick_spokes]
        await self._store(
            storage,
            components,
            [thin, thick, thin_spoke, *thick_spokes, *foreign],
            edges,
        )

        rows = await storage.get_most_connected(limit=1, project_context_root=self.PROJECT_A)

        assert [row["chunk_id"] for row in rows] == [str(thick.id)]

    @pytest.mark.asyncio
    async def test_namespace_filter_composes_with_the_boundary(self, storage, components):
        """``namespace=`` narrows within the boundary; it does not replace it."""
        hub = _project_chunk(components, self.PROJECT_A, "alpha-hub", "alpha", namespace="work")
        same_ns = _project_chunk(
            components, self.PROJECT_A, "alpha-work-spoke", "alpha", namespace="work"
        )
        other_ns = _project_chunk(
            components, self.PROJECT_A, "alpha-home-spoke", "alpha", namespace="home"
        )
        foreign = _project_chunk(
            components, self.PROJECT_B, "beta-work-spoke", "beta", namespace="work"
        )
        await self._store(
            storage,
            components,
            [hub, same_ns, other_ns, foreign],
            [(hub, same_ns), (hub, other_ns), (hub, foreign)],
        )

        rows = await storage.get_most_connected(
            namespace="work", project_context_root=self.PROJECT_A
        )

        assert {row["chunk_id"]: row["link_count"] for row in rows} == {
            str(hub.id): 1,
            str(same_ns.id): 1,
        }


class TestProjectAnalyticsIsolation:
    @pytest.mark.asyncio
    async def test_aggregates_tags_gaps_and_relations_are_project_scoped(self, storage, components):
        project_a = Path("/workspace/project-a")
        project_b = Path("/workspace/project-b")
        a1 = _project_chunk(components, project_a, "alpha-hub", "alpha")
        a2 = _project_chunk(components, project_a, "alpha-spoke", "alpha")
        b1 = _project_chunk(components, project_b, "beta-hub", "beta")
        b2 = _project_chunk(components, project_b, "beta-spoke", "beta")
        await storage.upsert_chunks([a1, a2, b1, b2])
        await storage.increment_access([a1.id, b1.id])
        await storage.add_relation(a1.id, a2.id)
        await storage.add_relation(b1.id, b2.id)
        await storage.save_query_history("alpha gap", [], [], [], project_context_root=project_a)
        await storage.save_query_history("beta gap", [], [], [], project_context_root=project_b)

        report = await storage.get_health_report(project_context_root=project_a)
        assert report["total_chunks"] == 2
        assert report["cross_references"] == 1
        assert all("beta" not in row["content"] for row in report["top_accessed"])
        assert await storage.get_tag_counts(project_context_root=project_a) == [("alpha", 2)]
        assert await storage.get_knowledge_gaps(project_context_root=project_a) == [
            {"query": "alpha gap", "count": 1}
        ]
        connected = await storage.get_most_connected(project_context_root=project_a)
        assert {row["chunk_id"] for row in connected} == {str(a1.id), str(a2.id)}


class TestChunkFactors:
    @pytest.mark.asyncio
    async def test_returns_factors(self, storage, components):
        chunk = _make_chunk(components, tags=("a", "b"))
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])

        factors = await storage.get_chunk_factors()
        assert len(factors) == 1
        assert factors[0]["access_count"] == 1
        assert factors[0]["updated_at"] is not None


class TestConsolidationGroups:
    @pytest.mark.asyncio
    async def test_empty(self, storage):
        result = await storage.get_consolidation_groups()
        assert result == []

    @pytest.mark.asyncio
    async def test_groups_by_source(self, storage, components):
        # Create 4 chunks from same source
        for i in range(4):
            c = Chunk(
                content=f"content {i}",
                metadata=ChunkMetadata(source_file=Path("/tmp/big.md")),
                embedding=[0.0] * components.config.embedding.dimension,
            )
            await storage.upsert_chunks([c])

        result = await storage.get_consolidation_groups(min_size=3)
        assert len(result) == 1
        assert result[0]["chunk_count"] == 4


class TestScratchPromote:
    @pytest.mark.asyncio
    async def test_promote(self, storage):
        await storage.scratch_set("key1", "val1")
        promoted = await storage.scratch_promote("key1")
        assert promoted is True

        entry = await storage.scratch_get("key1")
        assert entry["promoted"] is True

    @pytest.mark.asyncio
    async def test_promote_nonexistent(self, storage):
        promoted = await storage.scratch_promote("nope")
        assert promoted is False


class TestReflectEndToEnd:
    """The whole path, no stubs: real store, real aggregate, real renderer."""

    @pytest.mark.asyncio
    async def test_report_lists_the_callers_hub_not_the_dominant_foreign_one(
        self, storage, components, monkeypatch
    ):
        """#2244 end to end — a small project beside a large one still gets a section.

        Every layer between the SQL and the rendered markdown is exercised
        here, so a boundary that is correct in the aggregate but re-widened
        by the tool (an over-fetch, a Python recount, a re-sort) still fails.
        """
        from memtomem.server.context import AppContext
        from memtomem.server.tools.reflection import mem_reflect

        project_a = Path("/workspace/project-a")
        project_b = Path("/workspace/project-b")

        mine = _project_chunk(components, project_a, "alpha-hub", "alpha")
        my_spokes = [
            _project_chunk(components, project_a, f"alpha-spoke-{i}", "alpha") for i in range(2)
        ]
        beta_hub = _project_chunk(components, project_b, "beta-hub", "beta")
        beta_spokes = [
            _project_chunk(components, project_b, f"beta-spoke-{i}", "beta") for i in range(8)
        ]
        await storage.upsert_chunks([mine, *my_spokes, beta_hub, *beta_spokes])
        for spoke in my_spokes:
            await storage.add_relation(mine.id, spoke.id)
        for spoke in beta_spokes:
            await storage.add_relation(beta_hub.id, spoke.id)
        # A cross-project edge, so the visible count cannot come from simply
        # ignoring the foreign store.
        await storage.add_relation(mine.id, beta_hub.id)

        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root",
            lambda _app: project_a,
        )
        ctx = StubCtx(AppContext.from_components(components))

        report = await mem_reflect(ctx=ctx)  # type: ignore[arg-type]

        assert "### Most Connected Memories" in report
        assert "2 links — alpha-hub" in report
        assert "beta-hub" not in report
        assert str(beta_hub.id) not in report
