"""ADR-0036 pins: the visibility axes id-addressed access deliberately bypasses.

ADR-0036 splits the two rules search applies. The ADR-0011 project boundary
becomes a boundary on id-addressed access; **system-namespace hiding and
temporal validity do not**, because both are retrieval-relevance defaults that
an explicit argument already lifts and one config line switches off entirely
(``constants.py``: "a *convenience* isolation boundary, not a security
boundary"). Maintenance tools stay whole-store for the same reason they exist.

Nothing here is a happy-path test of a feature. Each case pins a bypass that is
deliberate, so that hardening it later is a decision someone makes on purpose
rather than a side effect of tightening the scope axis — which is exactly how
``mem_expand``'s anchor carve-out was lost track of between #2192 and #2236.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata

pytestmark = pytest.mark.asyncio


def _make_chunk(
    content: str,
    *,
    source: str = "/tmp/pins.md",
    start_line: int = 0,
    end_line: int = 10,
    namespace: str = "default",
    scope: str = "user",
    project_root: Path | None = None,
    valid_to_unix: int | None = None,
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            start_line=start_line,
            end_line=end_line,
            namespace=namespace,
            scope=scope,
            project_root=project_root,
            valid_to_unix=valid_to_unix,
        ),
        id=uuid4(),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 768,
    )


def _app_with(chunk: Chunk, *, listed: list[Chunk] | None = None) -> MagicMock:
    app = MagicMock()
    app.config.search.system_namespace_prefixes = ["archive:", "agent-runtime:"]
    app.storage.get_chunk = AsyncMock(return_value=chunk)
    app.storage.list_chunks_by_source = AsyncMock(return_value=listed or [chunk])
    return app


class TestMemReadBypassesTheRelevanceAxes:
    """``mem_read`` shows a chunk the default search would not rank."""

    async def test_reads_a_system_namespace_chunk(self, monkeypatch):
        """ADR-0036 §2: ``archive:*`` is hidden from search, not from a read.

        Reading an archived chunk by id is an existing workflow — the archive
        bucket is where auto-consolidation puts things, and inspecting one is
        the point. Hiding it here would break that to enforce a rule any caller
        lifts with ``namespace=``.
        """
        from memtomem.server.tools import browse

        chunk = _make_chunk("archived decision record", namespace="archive:2024")
        monkeypatch.setattr(
            browse, "_get_app_initialized", AsyncMock(return_value=_app_with(chunk))
        )

        result = await browse.mem_read(chunk_id=str(chunk.id), ctx=None)

        assert "archived decision record" in result
        assert "archive:2024" in result

    async def test_reads_an_agent_runtime_chunk(self, monkeypatch):
        """ADR-0036 §2: ``agent-runtime:*`` is a routing scope, not a boundary.

        Pinned separately from ``archive:`` because it is the prefix whose
        name most invites being read as isolation; ``server/tools/meta.py``
        says outright that anything which can call the server can read it.
        """
        from memtomem.server.tools import browse

        chunk = _make_chunk("planner scratch note", namespace="agent-runtime:planner")
        monkeypatch.setattr(
            browse, "_get_app_initialized", AsyncMock(return_value=_app_with(chunk))
        )

        result = await browse.mem_read(chunk_id=str(chunk.id), ctx=None)

        assert "planner scratch note" in result

    async def test_reads_a_chunk_outside_its_validity_window(self, monkeypatch):
        """ADR-0036 §2: an expired chunk is stale for ranking, not confidential.

        Temporal validity travels with the relevance half of the split. A
        caller asking for a superseded chunk by id generally wants exactly
        that — to see what the superseded version said.
        """
        from memtomem.server.tools import browse

        chunk = _make_chunk("superseded convention", valid_to_unix=1)
        monkeypatch.setattr(
            browse, "_get_app_initialized", AsyncMock(return_value=_app_with(chunk))
        )

        result = await browse.mem_read(chunk_id=str(chunk.id), ctx=None)

        assert "superseded convention" in result


class TestExpandAnchorBypassesTheRelevanceAxes:
    """The anchor's own content survives both relevance axes.

    #2239 removed the anchor's *widening* effect on its neighbours. It did
    not, and ADR-0036 does not, stop the addressed chunk itself from being
    returned on the namespace and validity axes — that is ``mem_read``'s
    contract and expansion inherits it.
    """

    async def test_expands_from_a_system_namespace_anchor(self, monkeypatch):
        from memtomem.server.tools import search as search_tools

        anchor = _make_chunk(
            "archived anchor", namespace="archive:2024", start_line=10, end_line=20
        )
        neighbour = _make_chunk("ordinary neighbour", start_line=21, end_line=30)
        app = _app_with(anchor, listed=[anchor, neighbour])
        monkeypatch.setattr(search_tools, "_get_app_initialized", AsyncMock(return_value=app))
        monkeypatch.setattr(search_tools, "_resolve_project_context_root", lambda _app: None)

        result = await search_tools.mem_expand(chunk_id=str(anchor.id), window=2, ctx=None)

        assert "archived anchor" in result

    async def test_expands_from_an_expired_anchor(self, monkeypatch):
        from memtomem.server.tools import search as search_tools

        anchor = _make_chunk("expired anchor", valid_to_unix=1, start_line=10, end_line=20)
        app = _app_with(anchor)
        monkeypatch.setattr(search_tools, "_get_app_initialized", AsyncMock(return_value=app))
        monkeypatch.setattr(search_tools, "_resolve_project_context_root", lambda _app: None)

        result = await search_tools.mem_expand(chunk_id=str(anchor.id), window=2, ctx=None)

        assert "expired anchor" in result


class TestMaintenanceTierStaysWholeStore:
    """ADR-0036 §3: operator-tier tools are not screened by scope.

    They service the one physical store, whose rows span every project, so
    scoping their corpora would make them unable to do the job they are named
    for. The honest consequence — stated in the ADR rather than hidden — is
    that this is leak hygiene on the ordinary surfaces, not confidentiality
    against a caller who can reach ``mem_do``.
    """

    async def test_dedup_merge_deletes_an_out_of_boundary_id(self, monkeypatch):
        from memtomem.server.tools import dedup_decay

        keep_id, foreign_id = uuid4(), uuid4()
        app = MagicMock()
        app.dedup_scanner.merge = AsyncMock(return_value=1)
        monkeypatch.setattr(dedup_decay, "_get_app_initialized", AsyncMock(return_value=app))
        monkeypatch.setattr(
            dedup_decay, "capture_session_for_untracked_write", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(dedup_decay, "flag_untracked_write", AsyncMock(return_value=None))

        result = await dedup_decay.mem_dedup_merge(
            keep_id=str(keep_id),
            delete_ids=[str(foreign_id)],
            dry_run=False,
            ctx=None,
        )

        assert "Merge complete: 1 chunks deleted" in result
        # No project context was consulted, and the foreign id reached the
        # scanner unscreened.
        called_keep, called_delete = app.dedup_scanner.merge.await_args.args
        assert called_keep == keep_id
        assert called_delete == [foreign_id]


class TestScopeAxisIsTheOneThatBinds:
    """The contrast that makes the split legible.

    Web ``GET /chunks/{id}`` has hydrated through the always-on ADR-0011
    fragment since it was written — "Knowing an id is not authorization"
    (``web/routes/chunks.py``). ADR-0036 adopts that answer for the scope axis
    everywhere; this pins the precedent it generalises, so a future change
    that relaxes the web route has to argue with the ADR rather than with one
    route's comment.
    """

    async def test_web_get_chunk_hydrates_through_the_scope_boundary(self, monkeypatch):
        from fastapi import HTTPException

        from memtomem.web.routes import chunks as chunks_route

        storage = MagicMock()
        storage.recall_chunks = AsyncMock(return_value=[])
        # ``get_chunk`` must not be the route's resolver — pinning it as a
        # tripwire rather than trusting the call-shape assertion alone.
        storage.get_chunk = AsyncMock(side_effect=AssertionError("must resolve via recall_chunks"))
        config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[]))
        chunk_id = uuid4()

        with pytest.raises(HTTPException) as excinfo:
            await chunks_route.get_chunk(chunk_id=chunk_id, storage=storage, config=config)

        assert excinfo.value.status_code == 404
        kwargs = storage.recall_chunks.await_args.kwargs
        assert kwargs["chunk_ids"] == (chunk_id,)
        assert "project_context_root" in kwargs
