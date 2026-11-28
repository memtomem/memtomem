"""#3669: a chunk delete may not put the entries after it inside a code fence.

The Markdown chunker reads a fence as running to the next closer of its kind,
or to the end of the file when none follows. Removing a span that holds the
closer of a fence an earlier line opened lets that fence run on over the
entries after the span, which then lose their own chunk, heading and tags in
the index. Both delete surfaces that write the file (MCP ``mem_delete`` and the
web ``DELETE /api/chunks/{id}``) go through ``memory_writer.remove_lines``,
which judges the file before and after the removal as the edit writers do
(#3633) and refuses before writing.

A span normally starts at a heading, which is outside any fence, so the
fixtures reach the shape the way it happens in practice: the file is indexed,
then one row outside the target span is edited by hand into one row, which
keeps the target's line numbers and span hash valid.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.chunking.markdown import unclosed_fence
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import STALE_SOURCE_PROVENANCE_DETAIL, source_span_hash
from memtomem.tools import memory_mutation
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import chunks as chunk_routes

pytestmark = pytest.mark.asyncio

SURFACES = ("web", "mcp")

THREE = "## A\n\nBody of A.\n\n## B\n\nBody of B.\n\n## C\n\nBody of C.\n"
FENCED_B = "## B\n\n```python\nprint(1)\n```\n\n"
ALL = ["## A", "## B", "## C"]


def _refusal(run: str = "```") -> str:
    from memtomem.tools.memory_writer import open_fence_delete_refusal

    refusal = open_fence_delete_refusal(run)
    # Pinned apart from the formatter: a delete is not worded as an edit.
    assert refusal.startswith(f"Deleting this entry would leave a {run} code fence open")
    return refusal


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


def _hand_edit(source, old_row: str, new_row: str) -> None:
    """Replace one row outside the target span with one row, as a hand edit would."""
    text = source.read_text(encoding="utf-8")
    assert text.count(old_row) == 1
    source.write_text(text.replace(old_row, new_row), encoding="utf-8")


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def _delete(comp, surface, chunk_id):
    """``(succeeded, refusal text or None, raw)`` for one chunk delete through ``surface``."""
    if surface == "mcp":
        ctx = StubCtx(AppContext.from_components(comp))
        raw = await memory_crud.mem_delete(chunk_id=str(chunk_id), ctx=ctx)
        if raw.startswith("Memory deleted"):
            return True, None, raw
        return False, raw.removeprefix("Error: "), raw
    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        raw = await c.delete(f"/api/chunks/{chunk_id}")
    if raw.status_code == 200:
        return True, None, raw
    detail = raw.json().get("detail") if raw.status_code == 409 else None
    return False, detail if isinstance(detail, str) else None, raw


@pytest.fixture
def spies(bm25_only_components, monkeypatch):
    """Witnesses that a refusal restored, re-indexed and invalidated nothing."""
    comp, _ = bm25_only_components
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    restores = []
    for module in (memory_crud, memory_mutation, chunk_routes):
        restore = Mock(wraps=module.restore_pre_image_quietly)
        monkeypatch.setattr(module, "restore_pre_image_quietly", restore)
        restores.append(restore)
    invalidate = Mock(wraps=comp.search_pipeline.invalidate_cache)
    monkeypatch.setattr(comp.search_pipeline, "invalidate_cache", invalidate)
    return index, restores, invalidate


def _assert_untouched(spies, surface):
    index, restores, invalidate = spies
    index.assert_not_awaited()
    for restore in restores:
        restore.assert_not_called()
    if surface == "web":
        # The route's refusal arm leaves ``mutation_attempted`` unset.
        invalidate.assert_not_called()


# ── refused ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "c_section",
    [
        # Both versions end outside a fence: after the delete, A's opener closes
        # at C's own closer, so only the tail check sees ``## C`` fenced.
        pytest.param("## C\n\n```text\nexample\n```\n", id="closed-eof"),
        # After the delete, A's opener runs to the end of the file.
        pytest.param("## C\n\nBody of C.\n", id="open-eof"),
    ],
)
async def test_removing_the_closer_of_an_earlier_opener_is_refused(
    bm25_only_components, spies, surface, c_section
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, "## A\n\nBody of A.\n\n" + FENCED_B + c_section)
    assert await _headings(comp, source) == ALL
    b = await _chunk(comp, source, "## B")
    # On disk, A's bare opener now closes at B's closer: ``## C`` stays outside.
    _hand_edit(source, "Body of A.", "```")
    assert unclosed_fence(source.read_text(encoding="utf-8")) is None
    before = source.read_bytes()
    spies[0].reset_mock()

    ok, refusal, raw = await _delete(comp, surface, b.id)
    headings = await _headings(comp, source)

    assert (ok, headings) == (False, ALL), raw
    assert refusal == _refusal()
    assert source.read_bytes() == before
    _assert_untouched(spies, surface)


@pytest.mark.parametrize("surface", SURFACES)
async def test_removing_the_last_entry_may_not_leave_the_file_inside_a_fence(
    bm25_only_components, spies, surface
):
    comp, mem_dir = bm25_only_components
    text = "## A\n\nBody of A.\n\n## B\n\nBody of B.\n\n## C\n\n```python\nx\n```\n"
    source = await _index(comp, mem_dir, text)
    c = await _chunk(comp, source, "## C")
    # B's bare opener now closes at C's closer; removing C leaves it open.
    _hand_edit(source, "Body of B.", "```")
    assert unclosed_fence(source.read_text(encoding="utf-8")) is None
    before = source.read_bytes()
    spies[0].reset_mock()

    ok, refusal, raw = await _delete(comp, surface, c.id)
    ends_open = unclosed_fence(source.read_text(encoding="utf-8"))

    assert (ok, ends_open) == (False, None), raw
    assert refusal == _refusal()
    assert source.read_bytes() == before
    _assert_untouched(spies, surface)


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_tail_whose_line_breaks_changed_after_indexing_is_refused(
    bm25_only_components, spies, surface
):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, THREE)
    a = await _chunk(comp, source, "## A")
    # Changed after A's span, so its hash still holds: a separator the rewrite
    # would count as a line break and the chunker would not.
    _hand_edit(source, "Body of C.", "Body\u2028of C.")
    lines = source.read_text(encoding="utf-8").splitlines()
    assert (
        source_span_hash(lines, a.metadata.start_line, a.metadata.end_line)
        == a.metadata.source_span_hash
    )
    before = source.read_bytes()
    spies[0].reset_mock()

    ok, refusal, raw = await _delete(comp, surface, a.id)
    kept = "\u2028" in source.read_text(encoding="utf-8")

    assert (ok, kept) == (False, True), raw
    assert refusal == STALE_SOURCE_PROVENANCE_DETAIL
    assert source.read_bytes() == before
    _assert_untouched(spies, surface)


# ── allowed ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_chunk_holding_a_closed_fence_is_deleted(bm25_only_components, surface):
    comp, mem_dir = bm25_only_components
    source = await _index(comp, mem_dir, "## A\n\nBody of A.\n\n" + FENCED_B + "## C\n\nBody.\n")
    b = await _chunk(comp, source, "## B")

    ok, _, raw = await _delete(comp, surface, b.id)

    assert ok, raw
    assert "print(1)" not in source.read_text(encoding="utf-8")
    assert await _headings(comp, source) == ["## A", "## C"]


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_already_inside_a_fence_can_still_be_deleted_from(
    bm25_only_components, surface
):
    comp, mem_dir = bm25_only_components
    text = "## A\n\nBody of A.\n\n## B\n\nBody of B.\n\n## C\n\n```text\nnever closed\n"
    source = await _index(comp, mem_dir, text)
    a = await _chunk(comp, source, "## A")

    ok, _, raw = await _delete(comp, surface, a.id)

    assert ok, raw
    assert source.read_text(encoding="utf-8").startswith("## B")


# ── the writer ───────────────────────────────────────────────────────────


async def test_remove_lines_refuses_and_writes_nothing(tmp_path):
    from memtomem.tools.memory_writer import OpenFenceEditError, remove_lines

    source = tmp_path / "a.md"
    text = "intro\n```\nkeep\n```python\nx\n```\nafter\n"
    source.write_text(text, encoding="utf-8")
    before = source.read_bytes()

    with pytest.raises(OpenFenceEditError) as caught:
        remove_lines(
            source,
            4,
            6,
            expected_source_span_hash=source_span_hash(text.splitlines(), 4, 6),
        )

    assert caught.value.run == "```"
    assert source.read_bytes() == before


async def test_a_non_markdown_source_is_not_judged(tmp_path):
    from memtomem.tools.memory_writer import remove_lines

    source = tmp_path / "notes.txt"
    text = "intro\n```\nkeep\n```python\nx\n```\nafter\n"
    source.write_text(text, encoding="utf-8")

    remove_lines(
        source,
        4,
        6,
        expected_source_span_hash=source_span_hash(text.splitlines(), 4, 6),
    )

    assert source.read_text(encoding="utf-8") == "intro\n```\nkeep\nafter\n"
