"""An entry whose content opens with its own ``## `` heading, on every write surface (#2762).

The writer used to put the entry header above that heading, where it leads no
section of the entry: the entry's chunks were neither tagged nor dated from it,
and the header text was indexed as part of the previous entry's chunk. ``mm add``
and web ``/api/add`` hid the tag half until the next re-index with a post-index
DB patch, now removed. The writer side is pinned in
``test_memory_writer_tag_format.py`` and ``mm add`` in
``test_cli_add_tag_isolation.py``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

_ALPHA = "Alpha entry: the deploy pipeline uses blue-green with a five minute bake"
_BETA = "Beta entry: the on-call rotation hands over every Monday at 09:00 UTC"
_OTHER = "Other section: the staging cluster drains at 18:00 UTC"
_TEMPLATE = f"## Mine\n\n{_BETA}"


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def _holder(comp, source: Path, needle: str):
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    holders = [c for c in chunks if needle in c.content]
    assert len(holders) == 1, [c.content for c in chunks]
    return holders[0]


async def _assert_entries(comp, source: Path) -> None:
    """Alpha keeps only its own text and tag; the template entry is tagged and
    dated by its header; a re-index of the file stores the same."""
    lines = source.read_text(encoding="utf-8").split("\n")
    header = lines[lines.index("## Mine") + 2]
    assert header.startswith("> created: ")
    written = datetime.fromisoformat(header.removeprefix("> created: "))
    for _ in range(2):
        alpha = await _holder(comp, source, _ALPHA)
        assert alpha.content == _ALPHA
        assert set(alpha.metadata.tags) == {"alpha"}
        beta = await _holder(comp, source, _BETA)
        assert beta.metadata.heading_hierarchy == ("## Mine",)
        assert set(beta.metadata.tags) == {"beta"}
        assert beta.created_at == written
        assert not (await comp.index_engine.index_file(source, force=True)).errors


@pytest.mark.asyncio
async def test_mcp_mem_add(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    await memory_crud.mem_add(content=_ALPHA, title="Alpha", tags=["alpha"], file="e.md", ctx=ctx)
    await memory_crud.mem_add(content=_TEMPLATE, tags=["beta"], file="e.md", ctx=ctx)

    await _assert_entries(comp, mem_dir / "e.md")


@pytest.mark.asyncio
async def test_mcp_mem_batch_add(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    result = await memory_crud.mem_batch_add(
        entries=[
            {"key": "Alpha", "value": _ALPHA, "tags": ["alpha"]},
            {"value": _TEMPLATE, "tags": ["beta"]},
        ],
        file="e.md",
        ctx=ctx,
    )
    assert not result.startswith("Error"), result

    await _assert_entries(comp, mem_dir / "e.md")


@pytest.mark.asyncio
async def test_web_add(bm25_only_components):
    comp, mem_dir = bm25_only_components
    async with _web_client(comp) as client:
        for body in (
            {"content": _ALPHA, "title": "Alpha", "tags": ["alpha"], "file": "e.md"},
            {"content": _TEMPLATE, "tags": ["beta"], "file": "e.md"},
        ):
            resp = await client.post("/api/add", json=body)
            assert resp.status_code == 200, resp.text

    await _assert_entries(comp, mem_dir / "e.md")


@pytest.mark.asyncio
async def test_web_add_does_not_tag_a_second_top_level_section(bm25_only_components):
    """A further ``## `` heading in one add's content closes the entry, so its
    section is untagged right after the add, as after a re-index and as on MCP."""
    comp, mem_dir = bm25_only_components
    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/add",
            json={
                "content": f"{_TEMPLATE}\n\n## Other\n\n{_OTHER}",
                "tags": ["beta"],
                "file": "e.md",
            },
        )
    assert resp.status_code == 200, resp.text

    source = mem_dir / "e.md"
    assert set((await _holder(comp, source, _BETA)).metadata.tags) == {"beta"}
    assert set((await _holder(comp, source, _OTHER)).metadata.tags) == set()


@pytest.mark.asyncio
async def test_a_body_only_edit_keeps_the_header(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    await memory_crud.mem_add(content=_TEMPLATE, tags=["beta"], file="e.md", ctx=ctx)
    source = mem_dir / "e.md"
    beta = await _holder(comp, source, _BETA)

    result = await memory_crud.mem_edit(chunk_id=str(beta.id), new_content="Edited body.", ctx=ctx)
    assert not result.startswith("Error"), result

    lines = source.read_text(encoding="utf-8").split("\n")
    heading = lines.index("## Mine")
    assert lines[heading + 2].startswith("> created: ")
    assert lines[heading + 3] == '> tags: ["beta"]'
    edited = await _holder(comp, source, "Edited body.")
    assert set(edited.metadata.tags) == {"beta"}
