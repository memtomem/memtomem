"""MCP / Web parity for tag-management tools.

Both surfaces must funnel through ``services.tag_management`` so that:

- ``search_pipeline.invalidate_cache()`` fires after a successful apply
  (catches a regression where MCP would bypass the service and call
  ``storage.rename_tag`` / ``delete_tag`` / ``merge_tags`` directly,
  leaving the result TTL cache stale).
- ``updated_at`` gets bumped (storage-level invariant; bypass would
  still bump because the storage helpers carry the bump now, so this
  is *not* the discriminator — the cache invalidation is).
- ``dry_run`` returns counts + sample without writing.

The discriminator across MCP and storage-direct paths is the cache
invalidation: storage helpers don't know about ``SearchPipeline``, so
only the service layer can invoke it.
"""

from __future__ import annotations

import pytest

from helpers import StubCtx, make_chunk
from memtomem.server.context import AppContext
from memtomem.server.tools.tag_management import (
    mem_tag_delete,
    mem_tag_merge,
    mem_tag_rename,
)


@pytest.fixture
async def mcp_app(components, monkeypatch):
    """``AppContext`` wrapping the real ``components`` fixture, with the
    search pipeline's ``invalidate_cache`` swapped for a counter so the
    test can check whether the MCP path actually triggered it."""
    app = AppContext.from_components(components)
    counter = {"calls": 0}

    def fake_invalidate() -> None:
        counter["calls"] += 1

    monkeypatch.setattr(components.search_pipeline, "invalidate_cache", fake_invalidate)
    return app, counter


@pytest.mark.asyncio
async def test_mcp_rename_routes_through_service_invalidates_cache(mcp_app, components):
    """Apply path: rename via MCP must trigger ``invalidate_cache`` —
    proves the tool is going through the service rather than bypassing
    it to call ``storage.rename_tag`` directly."""
    app, counter = mcp_app
    chunk = make_chunk(content="alpha", tags=("old",))
    await components.storage.upsert_chunks([chunk])
    ctx = StubCtx(app)

    out = await mem_tag_rename("old", "new", ctx=ctx)
    assert "1 chunks" in out

    assert counter["calls"] == 1, "MCP rename did not trigger search-cache invalidation"
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("new") == 1
    assert "old" not in counts


@pytest.mark.asyncio
async def test_mcp_rename_dry_run_does_not_invalidate_cache(mcp_app, components):
    app, counter = mcp_app
    chunk = make_chunk(content="alpha", tags=("old",))
    await components.storage.upsert_chunks([chunk])
    ctx = StubCtx(app)

    out = await mem_tag_rename("old", "new", dry_run=True, ctx=ctx)
    assert "DRY RUN" in out
    assert "1 chunks" in out
    assert counter["calls"] == 0
    # Storage untouched
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("old") == 1
    assert "new" not in counts


@pytest.mark.asyncio
async def test_mcp_delete_routes_through_service(mcp_app, components):
    app, counter = mcp_app
    chunk = make_chunk(content="alpha", tags=("doomed", "keep"))
    await components.storage.upsert_chunks([chunk])
    ctx = StubCtx(app)

    out = await mem_tag_delete("doomed", ctx=ctx)
    assert "1 chunks" in out
    assert counter["calls"] == 1
    counts = dict(await components.storage.get_tag_counts())
    assert "doomed" not in counts
    assert counts.get("keep") == 1


@pytest.mark.asyncio
async def test_mcp_delete_dry_run_no_invalidate(mcp_app, components):
    app, counter = mcp_app
    chunk = make_chunk(content="alpha", tags=("doomed", "keep"))
    await components.storage.upsert_chunks([chunk])
    ctx = StubCtx(app)

    out = await mem_tag_delete("doomed", dry_run=True, ctx=ctx)
    assert "DRY RUN" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("doomed") == 1


@pytest.mark.asyncio
async def test_mcp_merge_routes_through_service(mcp_app, components):
    app, counter = mcp_app
    c1 = make_chunk(content="a", tags=("py",))
    c2 = make_chunk(content="b", tags=("python3",))
    await components.storage.upsert_chunks([c1, c2])
    ctx = StubCtx(app)

    out = await mem_tag_merge(["py", "python3"], "python", ctx=ctx)
    assert "2 chunks" in out
    assert counter["calls"] == 1
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("python") == 2
    assert "py" not in counts
    assert "python3" not in counts


@pytest.mark.asyncio
async def test_mcp_merge_dry_run_no_invalidate(mcp_app, components):
    app, counter = mcp_app
    c1 = make_chunk(content="a", tags=("py",))
    await components.storage.upsert_chunks([c1])
    ctx = StubCtx(app)

    out = await mem_tag_merge(["py"], "python", dry_run=True, ctx=ctx)
    assert "DRY RUN" in out
    assert "1 chunks" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("py") == 1


@pytest.mark.asyncio
async def test_mcp_rename_rejects_empty(mcp_app):
    app, _ = mcp_app
    ctx = StubCtx(app)
    out = await mem_tag_rename("", "new", ctx=ctx)
    assert "Error" in out
    out = await mem_tag_rename("old", "", ctx=ctx)
    assert "Error" in out


@pytest.mark.asyncio
async def test_mcp_rename_rejects_same_name_after_strip(mcp_app, components):
    """Service-layer reject (post-strip) covers MCP too: the wrapper used
    to pre-check ``old_tag == new_tag`` raw, which let ``"foo"`` vs
    ``" foo "`` slip through. With the gate in ``services.tag_management``
    the MCP path now reports the same error from a single source.
    """
    app, counter = mcp_app
    c1 = make_chunk(content="a", tags=("kept",))
    await components.storage.upsert_chunks([c1])
    ctx = StubCtx(app)

    out = await mem_tag_rename("kept", "kept", ctx=ctx)
    assert "Error" in out
    out = await mem_tag_rename("  kept  ", "kept", ctx=ctx)
    assert "Error" in out
    # No write happened.
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("kept") == 1
    assert counter["calls"] == 0


@pytest.mark.asyncio
async def test_mcp_merge_rejects_empty_target(mcp_app):
    app, _ = mcp_app
    ctx = StubCtx(app)
    out = await mem_tag_merge(["py"], "", ctx=ctx)
    assert "Error" in out


@pytest.mark.asyncio
async def test_mcp_merge_rejects_empty_sources(mcp_app):
    app, _ = mcp_app
    ctx = StubCtx(app)
    out = await mem_tag_merge([], "python", ctx=ctx)
    assert "Error" in out
    out = await mem_tag_merge(["", "  "], "python", ctx=ctx)
    assert "Error" in out
