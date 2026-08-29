"""ADR-0036: the project boundary on chunks reached by id.

The companion to ``test_id_access_bypass_pins.py``, which pins the axes id
access deliberately ignores. This file pins the one it enforces: outside a
project only ``scope='user'`` chunks resolve by id; inside project ``<X>``,
user chunks and ``<X>``'s chunks resolve. Ids address rows, they do not
authorize reading one.

Two properties every surface here shares, and each test states which it is
checking:

1. **Refusal.** An out-of-boundary id gets the same answer as one that does
   not exist — byte-identical, no status-code or message tell. A caller must
   not be able to use any of these tools as an existence oracle.
2. **Non-interference.** The screen does not narrow what an in-boundary caller
   could already reach.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata
from memtomem.search.visibility import chunk_in_scope_boundary, resolve_visible_chunk

pytestmark = pytest.mark.asyncio

MINE = Path("/mine")
ELSEWHERE = Path("/elsewhere")


def _chunk(
    content: str,
    *,
    scope: str = "user",
    project_root: Path | None = None,
    namespace: str = "default",
) -> Chunk:
    root = project_root or Path("/tmp")
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=root / ".memtomem" / "memories" / "n.md",
            scope=scope,
            project_root=project_root,
            namespace=namespace,
        ),
        id=uuid4(),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 768,
    )


def _foreign() -> Chunk:
    return _chunk("another project's note", scope="project_shared", project_root=ELSEWHERE)


def _app_returning(chunk: Chunk | None, *, context: Path | None) -> MagicMock:
    app = MagicMock()
    app.config.search.system_namespace_prefixes = ["archive:", "agent-runtime:"]
    app.config.indexing.project_memory_dirs = (
        [] if context is None else [context / ".memtomem" / "memories"]
    )
    app.storage.get_chunk = AsyncMock(return_value=chunk)
    return app


def _pin_context(monkeypatch, root: Path | None) -> None:
    """Pin what the caller's cwd would otherwise decide.

    ``_resolve_project_context_root`` is re-exported from
    ``server.tools.search`` precisely so it can be pinned in one place; every
    id-addressed surface reads it through there.
    """
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda _app: root
    )


class TestTheRuleItself:
    """``chunk_in_scope_boundary`` — the Python twin of ``scope_context_sql``."""

    async def test_user_scope_resolves_everywhere(self):
        meta = _chunk("u").metadata
        assert chunk_in_scope_boundary(meta, None)
        assert chunk_in_scope_boundary(meta, MINE)

    async def test_project_tier_resolves_only_in_its_own_project(self):
        meta = _chunk("p", scope="project_shared", project_root=MINE).metadata
        assert chunk_in_scope_boundary(meta, MINE)
        assert not chunk_in_scope_boundary(meta, ELSEWHERE)
        assert not chunk_in_scope_boundary(meta, None)

    async def test_resolver_answers_none_for_missing_and_for_foreign(self):
        """Refusal: the two cases are one value, so callers cannot split them."""
        storage = SimpleNamespace(get_chunk=AsyncMock(return_value=None))
        missing = await resolve_visible_chunk(storage, uuid4(), project_context_root=None)

        foreign_chunk = _foreign()
        storage = SimpleNamespace(get_chunk=AsyncMock(return_value=foreign_chunk))
        foreign = await resolve_visible_chunk(storage, foreign_chunk.id, project_context_root=None)

        assert missing is None
        assert foreign is None


class TestMemRead:
    async def test_foreign_id_reads_exactly_like_a_missing_one(self, monkeypatch):
        from memtomem.server.tools import browse

        foreign = _foreign()
        _pin_context(monkeypatch, MINE)

        monkeypatch.setattr(
            browse,
            "_get_app_initialized",
            AsyncMock(return_value=_app_returning(foreign, context=MINE)),
        )
        refused = await browse.mem_read(chunk_id=str(foreign.id), ctx=None)

        absent_id = uuid4()
        monkeypatch.setattr(
            browse,
            "_get_app_initialized",
            AsyncMock(return_value=_app_returning(None, context=MINE)),
        )
        missing = await browse.mem_read(chunk_id=str(absent_id), ctx=None)

        assert refused == f"Chunk {foreign.id} not found."
        assert missing == f"Chunk {absent_id} not found."
        assert "another project's note" not in refused

    async def test_own_project_chunk_still_reads(self, monkeypatch):
        """Non-interference: the screen costs an in-project caller nothing."""
        from memtomem.server.tools import browse

        mine = _chunk("my project rule", scope="project_shared", project_root=MINE)
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(
            browse,
            "_get_app_initialized",
            AsyncMock(return_value=_app_returning(mine, context=MINE)),
        )

        result = await browse.mem_read(chunk_id=str(mine.id), ctx=None)

        assert "my project rule" in result


class TestChunkResource:
    async def test_the_resource_is_screened_like_the_tool(self, monkeypatch):
        """The resource is a second door onto the same read (#2238 inventory)."""
        import json

        from memtomem.server import resources

        foreign = _foreign()
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(
            resources,
            "_get_app_initialized",
            AsyncMock(return_value=_app_returning(foreign, context=MINE)),
        )

        payload = json.loads(await resources.chunk_resource(chunk_id=str(foreign.id), ctx=None))

        assert payload == {"error": f"Chunk {foreign.id} not found"}


class TestRelations:
    async def test_link_refuses_a_foreign_end(self, monkeypatch):
        from memtomem.server.tools import cross_ref

        mine = _chunk("mine")
        foreign = _foreign()
        app = _app_returning(None, context=MINE)
        app.storage.get_chunk = AsyncMock(side_effect=[mine, foreign])
        app.storage.add_relation = AsyncMock()
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(cross_ref, "_get_app_initialized", AsyncMock(return_value=app))

        result = await cross_ref.mem_link(
            source_id=str(mine.id), target_id=str(foreign.id), ctx=None
        )

        assert result == f"Chunk {foreign.id} not found."
        app.storage.add_relation.assert_not_awaited()

    async def test_unlink_refuses_a_foreign_end(self, monkeypatch):
        """Severing a relation is a write; the same boundary applies."""
        from memtomem.server.tools import cross_ref

        mine = _chunk("mine")
        foreign = _foreign()
        app = _app_returning(None, context=MINE)
        app.storage.get_chunk = AsyncMock(side_effect=[mine, foreign])
        app.storage.delete_relation = AsyncMock(return_value=True)
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(cross_ref, "_get_app_initialized", AsyncMock(return_value=app))

        result = await cross_ref.mem_unlink(
            source_id=str(mine.id), target_id=str(foreign.id), ctx=None
        )

        assert result == f"Chunk {foreign.id} not found."
        app.storage.delete_relation.assert_not_awaited()

    async def test_related_drops_hidden_neighbours_from_output_and_count(self, monkeypatch):
        """A hidden relation leaves the listing *and* the link count.

        Rendering it as the dangling-id line would be worse than saying
        nothing: relations are ON DELETE CASCADE, so that line means
        "deleted", and it prints the full uuid. A count that included it
        would leak the same existence more quietly.
        """
        from memtomem.server.tools import cross_ref

        anchor = _chunk("anchor")
        visible = _chunk("visible neighbour")
        foreign = _foreign()
        deleted_id = uuid4()

        app = _app_returning(None, context=MINE)
        app.storage.get_chunk = AsyncMock(
            side_effect=[anchor, visible, foreign, None]  # anchor, then each relation
        )
        app.storage.get_related = AsyncMock(
            return_value=[
                (visible.id, "related"),
                (foreign.id, "related"),
                (deleted_id, "related"),
            ]
        )
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(cross_ref, "_get_app_initialized", AsyncMock(return_value=app))

        result = await cross_ref.mem_related(chunk_id=str(anchor.id), ctx=None)

        assert "visible neighbour" in result
        assert str(foreign.id) not in result
        assert "another project's note" not in result
        # The genuinely deleted relation still renders — that is information
        # about the caller's own graph, not about a hidden row.
        assert str(deleted_id) in result
        assert "(2 links)" in result


class TestReflect:
    async def test_link_counts_exclude_hidden_edges(self, monkeypatch):
        """A visible hub's degree must not count neighbours the caller can't see.

        ``get_most_connected`` counts every edge in the store, so rendering
        its number would report how many foreign-project neighbours a chunk
        has — the same existence leak as printing their ids, only quieter.
        """
        from memtomem.server.tools import reflection

        hub = _chunk("connected hub")
        visible = _chunk("visible neighbour")
        foreign = _foreign()

        app = _app_returning(None, context=MINE)
        app.storage.get_most_connected = AsyncMock(
            return_value=[{"chunk_id": str(hub.id), "link_count": 2}]
        )
        app.storage.get_related = AsyncMock(
            return_value=[(visible.id, "related"), (foreign.id, "related")]
        )
        app.storage.get_chunk = AsyncMock(side_effect=[hub, visible, foreign])
        for empty in (
            "get_frequently_accessed",
            "get_agent_sessions",
            "get_tag_counts",
            "get_knowledge_gaps",
        ):
            setattr(app.storage, empty, AsyncMock(return_value=[]))
        app.search_pipeline.flush_observation = AsyncMock()
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(reflection, "_get_app_initialized", AsyncMock(return_value=app))

        result = await reflection.mem_reflect(ctx=None)

        assert "connected hub" in result
        assert "1 links — connected hub" in result
        assert "2 links" not in result


class TestAgentShare:
    async def test_share_refuses_to_republish_a_foreign_chunk(self, monkeypatch):
        """The one id path that copies content rather than showing it."""
        from memtomem.server.tools import multi_agent

        foreign = _foreign()
        app = _app_returning(foreign, context=MINE)
        _pin_context(monkeypatch, MINE)
        monkeypatch.setattr(multi_agent, "_get_app_initialized", AsyncMock(return_value=app))

        result = await multi_agent.mem_agent_share(chunk_id=str(foreign.id), ctx=None)

        assert result == f"Chunk {foreign.id} not found."


class TestMutationLock:
    """``locked_source_chunk`` — the boundary is judged under the lock."""

    async def test_out_of_boundary_reports_not_found(self):
        from memtomem.tools.memory_mutation import locked_source_chunk

        foreign = _foreign()
        storage = SimpleNamespace(get_chunk=AsyncMock(return_value=foreign))

        async with locked_source_chunk(storage, foreign.id, project_context_root=MINE) as (
            chunk,
            reason,
        ):
            pass

        assert chunk is None
        assert reason == "not_found"

    async def test_a_chunk_re_scoped_while_we_waited_is_refused(self, tmp_path):
        """The second fetch decides, because it is the one the write uses.

        ``memory-migrate`` re-scopes under the source/index locks, so it can
        land between the probe and the under-lock re-fetch. Screening only the
        probe would check a value that is then thrown away.
        """
        from memtomem.tools.memory_mutation import locked_source_chunk

        source = tmp_path / "n.md"
        source.write_text("x\n", encoding="utf-8")
        in_boundary = Chunk(
            content="mine for now",
            metadata=ChunkMetadata(source_file=source, scope="user"),
            embedding=[0.1] * 768,
        )
        migrated = Chunk(
            content="mine for now",
            metadata=ChunkMetadata(
                source_file=source, scope="project_shared", project_root=ELSEWHERE
            ),
            id=in_boundary.id,
            embedding=[0.1] * 768,
        )
        storage = SimpleNamespace(get_chunk=AsyncMock(side_effect=[in_boundary, migrated]))

        async with locked_source_chunk(storage, in_boundary.id, project_context_root=MINE) as (
            chunk,
            reason,
        ):
            pass

        assert chunk is None
        assert reason == "not_found"


class TestTagService:
    async def test_replace_chunk_tags_refuses_out_of_boundary(self, storage):
        import dataclasses

        from memtomem.services import tag_management as svc

        # The real storage fixture pins 1024-d embeddings.
        foreign = dataclasses.replace(_foreign(), embedding=[0.1] * 1024)
        await storage.upsert_chunks([foreign])

        result = await svc.replace_chunk_tags(
            storage, foreign.id, ["new"], project_context_root=MINE
        )

        assert result is None
        # And the row is untouched — a refusal is not a silent no-op write.
        assert (await storage.get_chunk(foreign.id)).metadata.tags == foreign.metadata.tags
