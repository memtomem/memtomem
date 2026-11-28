"""#3633: a chunk edit may not put the text after it inside a code fence.

The Markdown chunker reads a fence that never closes as running to the end of
the file, and a fence as running to the next closer of its kind. An edit whose
result opens a fence over the entries after it drops them from the index (no
chunk, heading or tags of their own) although they were indexed correctly
before. Both edit surfaces (MCP ``mem_edit`` and the web ``PATCH``) go through
``memory_writer.replace_chunk_body``, which refuses such an edit before writing.
The file is judged as the chunker reads it, before and after the edit, so an
edit that removes a closer or reaches a fence elsewhere is judged too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import STALE_SOURCE_PROVENANCE_DETAIL, source_span_hash
from memtomem.tools import memory_mutation
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

pytestmark = pytest.mark.asyncio

SURFACES = ("web", "mcp")

THREE = "## First\n\nBody of First.\n\n## Second\n\nBody of Second.\n\n## Third\n\nBody of Third.\n"
OPEN_BODY = "```python\nprint(1)"


def _refusal(run: str = "```") -> str:
    from memtomem.tools.memory_writer import open_fence_edit_refusal

    return open_fence_edit_refusal(run)


async def _index(comp, directory, text, name="a.md"):
    source = directory / name
    source.write_text(text, encoding="utf-8")
    stats = await comp.index_engine.index_file(source, ingress_guarded=True)
    assert not stats.errors
    return source


async def _chunks(comp, source):
    chunks = await comp.storage.list_chunks_by_source(source)
    return sorted(chunks, key=lambda c: c.metadata.start_line)


async def _headings(comp, source):
    return [c.metadata.heading_hierarchy[-1] for c in await _chunks(comp, source)]


async def _chunk(comp, source, heading):
    for chunk in await _chunks(comp, source):
        if chunk.metadata.heading_hierarchy[-1] == heading:
            return chunk
    raise AssertionError(f"no chunk under {heading!r}")


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def _edit(comp, surface, chunk_id, new_content, base_hash=None):
    """``(succeeded, refusal text or None, raw)`` for one edit through ``surface``."""
    if surface == "mcp":
        ctx = StubCtx(AppContext.from_components(comp))
        kwargs = {"base_hash": base_hash} if base_hash is not None else {}
        raw = await memory_crud.mem_edit(str(chunk_id), new_content, ctx=ctx, **kwargs)
        if raw.startswith("Memory updated"):
            return True, None, raw
        return False, raw.removeprefix("Error: "), raw
    body = {"new_content": new_content}
    if base_hash is not None:
        body["base_hash"] = base_hash
    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        raw = await c.patch(f"/api/chunks/{chunk_id}", json=body)
    if raw.status_code == 200:
        return True, None, raw
    detail = raw.json().get("detail") if raw.status_code == 409 else None
    return False, detail if isinstance(detail, str) else None, raw


async def _source_base(comp, chunk_id):
    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        resp = await c.get(f"/api/chunks/{chunk_id}/source")
    assert resp.status_code == 200, resp.text
    return resp.json()["text"], resp.json()["base_hash"]


@pytest.fixture
def spies(bm25_only_components, monkeypatch):
    """Witnesses that a refusal restored and re-indexed nothing."""
    comp, _ = bm25_only_components
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    restores = []
    for module in (memory_crud, memory_mutation):
        restore = Mock(wraps=module.restore_pre_image_quietly)
        monkeypatch.setattr(module, "restore_pre_image_quietly", restore)
        restores.append(restore)
    return index, restores


def _assert_untouched(spies):
    index, restores = spies
    index.assert_not_awaited()
    for restore in restores:
        restore.assert_not_called()


# ── refused ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("surface", SURFACES)
async def test_the_issue_reproduction_is_refused_and_nothing_is_written(
    bm25_only_components, spies, surface
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, THREE)
    assert await _headings(comp, source) == ["## First", "## Second", "## Third"]
    first = await _chunk(comp, source, "## First")
    before = source.read_bytes()
    spies[0].reset_mock()

    ok, refusal, raw = await _edit(comp, surface, first.id, OPEN_BODY)

    assert not ok, raw
    assert refusal == _refusal()
    assert source.read_bytes() == before
    _assert_untouched(spies)
    assert await _headings(comp, source) == ["## First", "## Second", "## Third"]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "text",
    [
        # Both versions end inside a fence: the one ``## Third`` already opened.
        pytest.param(
            "## First\n\nBody of First.\n\n## Second\n\nBody of Second.\n\n"
            "## Third\n\n```text\nnever closed\n",
            id="a-later-fence-already-open",
        ),
        # The edit's fence closes at ``## Third``'s bare closer: EOF is outside.
        pytest.param(
            "## First\n\nBody of First.\n\n## Second\n\nBody of Second.\n\n"
            "## Third\n\n```text\nexample\n```\n",
            id="a-later-closer-closes-the-edits-fence",
        ),
    ],
)
async def test_an_edit_that_fences_later_headings_is_refused_whatever_eof_says(
    bm25_only_components, spies, surface, text
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, text)
    assert "## Second" in await _headings(comp, source)
    first = await _chunk(comp, source, "## First")
    before = source.read_bytes()
    spies[0].reset_mock()

    ok, refusal, raw = await _edit(comp, surface, first.id, OPEN_BODY)

    assert not ok, raw
    assert refusal == _refusal()
    assert source.read_bytes() == before
    _assert_untouched(spies)


@pytest.mark.parametrize("surface", SURFACES)
async def test_the_last_entry_may_not_leave_the_file_inside_a_fence(
    bm25_only_components, spies, surface
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, THREE)
    third = await _chunk(comp, source, "## Third")
    before = source.read_bytes()
    spies[0].reset_mock()

    ok, refusal, raw = await _edit(comp, surface, third.id, OPEN_BODY)

    assert not ok, raw
    assert refusal == _refusal()
    assert source.read_bytes() == before
    _assert_untouched(spies)


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_source_edit_that_drops_a_closer_is_refused(bm25_only_components, surface):
    comp, mem_dir = bm25_only_components
    text = THREE.replace("Body of First.", "```python\nprint(1)\n```")
    source = await _index(comp, mem_dir, text)
    first = await _chunk(comp, source, "## First")
    shown, base_hash = await _source_base(comp, first.id)
    assert shown.rstrip("\n").endswith("```")
    before = source.read_bytes()

    dropped = shown.rstrip("\n").removesuffix("```").rstrip("\n")
    ok, refusal, raw = await _edit(comp, surface, first.id, dropped, base_hash=base_hash)

    assert not ok, raw
    assert refusal == _refusal()
    assert source.read_bytes() == before


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_fence_opened_only_by_a_resolved_wikilink_is_refused(bm25_only_components, surface):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, THREE)
    first = await _chunk(comp, source, "## First")
    before = source.read_bytes()

    ok, refusal, raw = await _edit(comp, surface, first.id, "[[note|~~~python]]\nprint(1)")

    assert not ok, raw
    assert refusal == _refusal("~~~")
    assert source.read_bytes() == before


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_tail_whose_line_breaks_changed_after_indexing_is_refused(
    bm25_only_components, surface
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, THREE)
    first = await _chunk(comp, source, "## First")
    # Changed outside the span, so its hash still holds: a separator the
    # rewrite would count as a line break and the chunker would not.
    source.write_text(THREE.replace("Body of Third.", "Body\u2028of Third."), encoding="utf-8")
    before = source.read_bytes()

    ok, refusal, raw = await _edit(comp, surface, first.id, "New body of First.")

    assert not ok, raw
    assert refusal == STALE_SOURCE_PROVENANCE_DETAIL
    assert source.read_bytes() == before


# ── allowed ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_closed_fence_in_the_body_is_written(bm25_only_components, surface):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, THREE)
    first = await _chunk(comp, source, "## First")

    ok, _, raw = await _edit(comp, surface, first.id, OPEN_BODY + "\n```")

    assert ok, raw
    assert "print(1)\n```" in source.read_text(encoding="utf-8")
    assert await _headings(comp, source) == ["## First", "## Second", "## Third"]


OPEN_AT_EOF = (
    "## First\n\nBody of First.\n\n## Second\n\nBody of Second.\n\n"
    "## Third\n\n```text\nnever closed\n"
)


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_already_inside_a_fence_can_still_be_edited_and_repaired(
    bm25_only_components, surface
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, OPEN_AT_EOF)
    first = await _chunk(comp, source, "## First")

    ok, _, raw = await _edit(comp, surface, first.id, "New body of First.")
    assert ok, raw

    third = await _chunk(comp, source, "## Third")
    ok, _, raw = await _edit(comp, surface, third.id, "```text\nnow closed\n```")
    assert ok, raw
    assert source.read_text(encoding="utf-8").endswith("now closed\n```\n")


async def test_a_non_markdown_source_is_not_judged(tmp_path):
    from memtomem.tools.memory_writer import IndexedBase, replace_chunk_body

    source = tmp_path / "notes.txt"
    source.write_text("intro\nbody\nafter\n", encoding="utf-8")
    lines = source.read_text(encoding="utf-8").splitlines()

    replace_chunk_body(
        source,
        2,
        2,
        "```python",
        base=IndexedBase("body"),
        expected_source_span_hash=source_span_hash(lines, 2, 2),
    )

    assert source.read_text(encoding="utf-8") == "intro\n```python\nafter\n"


async def test_a_blank_separator_newly_inside_a_fence_is_not_refused(tmp_path):
    from memtomem.tools.memory_writer import replace_lines

    source = tmp_path / "a.md"
    text = "old\n\n~~~text\nexample\n~~~\n## Next\nbody\n"
    source.write_text(text, encoding="utf-8")

    replace_lines(
        source,
        1,
        1,
        "new\n~~~text",
        expected_source_span_hash=source_span_hash(text.splitlines(), 1, 1),
    )

    assert source.read_text(encoding="utf-8").startswith("new\n~~~text\n\n~~~text\n")


async def test_replace_lines_refuses_and_writes_nothing(tmp_path):
    from memtomem.tools.memory_writer import OpenFenceEditError, replace_lines

    source = tmp_path / "a.md"
    source.write_text(THREE, encoding="utf-8")
    before = source.read_bytes()

    with pytest.raises(OpenFenceEditError) as caught:
        replace_lines(
            source,
            3,
            3,
            OPEN_BODY,
            expected_source_span_hash=source_span_hash(THREE.splitlines(), 3, 3),
        )

    assert caught.value.run == "```"
    assert source.read_bytes() == before
