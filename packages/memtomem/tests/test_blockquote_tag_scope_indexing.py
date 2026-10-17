"""An entry's blockquote tags reach its sub-heading chunks end to end (#2747).

The chunker side is pinned in ``test_chunking_blockquote_tags.py``. These
drive the stored rows: the MCP ``mem_add`` write, a source already indexed
by a release that dropped the tags, and an edit of the entry's tags line.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import Mock

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud

# Long enough that the engine keeps the parts as separate chunks instead of
# merging small sections into one.
_PARTS = "\n\n".join(
    f"### Part {i}\n\n" + " ".join(f"part{i}-word{j}" for j in range(400)) for i in range(3)
)
_ENTRY = f'## Beta\n\n> created: 2026-10-17T00:00:00+00:00\n> tags: ["beta"]\n\n{_PARTS}\n'


async def _part_tags(comp, source) -> dict[str, set[str]]:
    """Tags per ``### Part`` heading, after checking every chunk under it agrees."""
    by_heading: dict[str, list[set[str]]] = {}
    for c in await comp.storage.list_chunks_by_source(source):
        heading = c.metadata.heading_hierarchy[-1]
        if heading.startswith("### Part"):
            by_heading.setdefault(heading, []).append(set(c.metadata.tags))
    assert len(by_heading) == 3, by_heading
    for heading, tag_sets in by_heading.items():
        assert all(t == tag_sets[0] for t in tag_sets), (heading, tag_sets)
    return {heading: tag_sets[0] for heading, tag_sets in by_heading.items()}


@pytest.mark.asyncio
async def test_mcp_mem_add_tags_every_sub_heading_chunk(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))

    await memory_crud.mem_add(content=_PARTS, title="Beta", tags=["beta"], file="entry.md", ctx=ctx)

    assert await _part_tags(comp, mem_dir / "entry.md") == {
        f"### Part {i}": {"beta"} for i in range(3)
    }


@pytest.mark.asyncio
async def test_a_release_bump_retags_rows_an_older_chunker_wrote(bm25_only_components, monkeypatch):
    """Rows written before the fix carry no tags on the sub-heading chunks.
    The receipt must not keep them that way past an upgrade, and the repair
    is a metadata-only update, not a re-embed."""
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    source = mem_dir / "legacy.md"
    source.write_text(_ENTRY, encoding="utf-8")

    real_chunk_content = engine.chunk_content

    def legacy_chunk_content(*args, **kwargs):
        # The pre-fix chunker: only the section the blockquote leads is tagged.
        return [
            dataclasses.replace(c, metadata=dataclasses.replace(c.metadata, tags=()))
            for c in real_chunk_content(*args, **kwargs)
        ]

    monkeypatch.setattr(engine, "chunk_content", legacy_chunk_content)
    assert not (await engine.index_file(source)).errors
    monkeypatch.setattr(engine, "chunk_content", real_chunk_content)
    legacy_ids = {c.id for c in await comp.storage.list_chunks_by_source(source)}

    # Same release: the receipt written for the legacy rows is honoured, so
    # the seed is a valid stand-in for a source indexed before the fix.
    assert not (await engine.index_file(source)).errors
    assert await _part_tags(comp, source) == {f"### Part {i}": set() for i in range(3)}

    update_metadata = Mock(wraps=comp.storage.update_chunk_metadata)
    upsert = Mock(wraps=comp.storage.upsert_chunks)
    monkeypatch.setattr(comp.storage, "update_chunk_metadata", update_metadata)
    monkeypatch.setattr(comp.storage, "upsert_chunks", upsert)
    monkeypatch.setattr("memtomem.indexing.engine._memtomem_version", "99.99.99")
    assert not (await engine.index_file(source)).errors

    assert await _part_tags(comp, source) == {f"### Part {i}": {"beta"} for i in range(3)}
    assert {c.id for c in await comp.storage.list_chunks_by_source(source)} == legacy_ids
    update_metadata.assert_called_once()
    upsert.assert_not_called()


@pytest.mark.asyncio
async def test_editing_the_entry_tags_line_moves_its_sub_heading_chunks(bm25_only_components):
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    source = mem_dir / "edited.md"
    source.write_text(_ENTRY, encoding="utf-8")
    assert not (await engine.index_file(source)).errors

    source.write_text(_ENTRY.replace('["beta"]', '["gamma"]'), encoding="utf-8")
    assert not (await engine.index_file(source)).errors
    assert await _part_tags(comp, source) == {f"### Part {i}": {"gamma"} for i in range(3)}

    source.write_text(_ENTRY.replace('> tags: ["beta"]\n', ""), encoding="utf-8")
    assert not (await engine.index_file(source)).errors
    assert await _part_tags(comp, source) == {f"### Part {i}": set() for i in range(3)}
