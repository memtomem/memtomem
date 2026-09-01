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
from memtomem.server.tools.meta import mem_do
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

    out = await mem_tag_rename("old", "new", dry_run=False, ctx=ctx)
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

    out = await mem_tag_delete("doomed", dry_run=False, ctx=ctx)
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

    out = await mem_tag_merge(["py", "python3"], "python", dry_run=False, ctx=ctx)
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


# --- default-is-dry-run pins (#1992) ---------------------------------------
# One pin per tool: a call that omits dry_run must preview, not write. Each
# tool gets its own test so a sibling can't pass on another's behalf.


@pytest.mark.asyncio
async def test_mcp_rename_defaults_to_dry_run(mcp_app, components):
    app, counter = mcp_app
    await components.storage.upsert_chunks([make_chunk(content="a", tags=("old",))])
    ctx = StubCtx(app)

    out = await mem_tag_rename("old", "new", ctx=ctx)
    assert "DRY RUN" in out
    assert "dry_run=false" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("old") == 1
    assert "new" not in counts


@pytest.mark.asyncio
async def test_mcp_delete_defaults_to_dry_run(mcp_app, components):
    app, counter = mcp_app
    await components.storage.upsert_chunks([make_chunk(content="a", tags=("doomed",))])
    ctx = StubCtx(app)

    out = await mem_tag_delete("doomed", ctx=ctx)
    assert "DRY RUN" in out
    assert "dry_run=false" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("doomed") == 1


@pytest.mark.asyncio
async def test_mcp_merge_defaults_to_dry_run(mcp_app, components):
    app, counter = mcp_app
    await components.storage.upsert_chunks([make_chunk(content="a", tags=("py",))])
    ctx = StubCtx(app)

    out = await mem_tag_merge(["py"], "python", ctx=ctx)
    assert "DRY RUN" in out
    assert "dry_run=false" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("py") == 1
    assert "python" not in counts


# --- strict dry_run pins (#1992 follow-up) ----------------------------------
# mem_do forwards params unvalidated, so the tool body must refuse anything
# that is not a literal True/False: a falsy-but-not-False value (0, "", None)
# would otherwise slip past the preview default straight into a bulk write.
# These go through the REAL mem_do dispatch (not a direct function call) so
# the registry wiring for each action is pinned too. One test per tool — a
# sibling can't pass on another's behalf.

_MALFORMED_DRY_RUN = (0, "false", "", None)


@pytest.mark.asyncio
async def test_mem_do_rename_refuses_malformed_dry_run(mcp_app, components):
    app, counter = mcp_app
    await components.storage.upsert_chunks([make_chunk(content="a", tags=("old",))])
    ctx = StubCtx(app)

    for bad in _MALFORMED_DRY_RUN:
        out = await mem_do(
            action="tag_rename",
            params={"old_tag": "old", "new_tag": "new", "dry_run": bad},
            ctx=ctx,
        )
        assert "error" in out.lower(), f"dry_run={bad!r} was not refused"
        assert "literal boolean" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("old") == 1
    assert "new" not in counts


@pytest.mark.asyncio
async def test_mem_do_delete_refuses_malformed_dry_run(mcp_app, components):
    app, counter = mcp_app
    await components.storage.upsert_chunks([make_chunk(content="a", tags=("doomed",))])
    ctx = StubCtx(app)

    for bad in _MALFORMED_DRY_RUN:
        out = await mem_do(action="tag_delete", params={"tag": "doomed", "dry_run": bad}, ctx=ctx)
        assert "error" in out.lower(), f"dry_run={bad!r} was not refused"
        assert "literal boolean" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("doomed") == 1


@pytest.mark.asyncio
async def test_mem_do_merge_refuses_malformed_dry_run(mcp_app, components):
    app, counter = mcp_app
    await components.storage.upsert_chunks([make_chunk(content="a", tags=("py",))])
    ctx = StubCtx(app)

    for bad in _MALFORMED_DRY_RUN:
        out = await mem_do(
            action="tag_merge",
            params={"sources": ["py"], "target": "python", "dry_run": bad},
            ctx=ctx,
        )
        assert "error" in out.lower(), f"dry_run={bad!r} was not refused"
        assert "literal boolean" in out
    assert counter["calls"] == 0
    counts = dict(await components.storage.get_tag_counts())
    assert counts.get("py") == 1
    assert "python" not in counts


# The OTHER dispatch door: a direct tool call goes through FastMCP's pydantic
# arg model, which is LAX by default and would coerce "false" / 0 → False
# before the body's strict_bool runs — reaching apply without a literal JSON
# false. StrictBool annotations close that. Built via func_metadata so this
# runs in every tool mode (see test_server_tools_context_pull.py).

_TAG_TOOLS = {
    "rename": (mem_tag_rename, {"old_tag": "a", "new_tag": "b"}),
    "delete": (mem_tag_delete, {"tag": "a"}),
    "merge": (mem_tag_merge, {"sources": ["a"], "target": "b"}),
}


def _arg_model(func) -> type:
    from mcp.server.mcpserver.utilities.func_metadata import func_metadata

    return func_metadata(func).arg_model


@pytest.mark.parametrize("tool", sorted(_TAG_TOOLS))
@pytest.mark.parametrize("bad", ["true", "false", 1, 0, "yes"])
def test_fastmcp_boundary_rejects_non_literal_dry_run(tool: str, bad: object) -> None:
    from pydantic import ValidationError

    func, required = _TAG_TOOLS[tool]
    with pytest.raises(ValidationError):
        _arg_model(func).model_validate({**required, "dry_run": bad})


@pytest.mark.parametrize("tool", sorted(_TAG_TOOLS))
def test_fastmcp_boundary_accepts_real_booleans(tool: str) -> None:
    func, required = _TAG_TOOLS[tool]
    model = _arg_model(func)
    for val in (True, False):
        m = model.model_validate({**required, "dry_run": val})
        assert m.dry_run is val
    # Wire schema stays a plain boolean with the preview default — clients
    # see no StrictBool leak.
    schema = model.model_json_schema()["properties"]["dry_run"]
    assert schema["type"] == "boolean"
    assert schema["default"] is True


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
