"""``mem_consolidate_apply`` takes the summary's id from ``new_chunk_ids`` (#3315).

The other append callers moved to ``appended_chunk_ids`` (#3292), because a
re-index of a shared file also upserts a pending edit's chunk and misses a chunk
that keeps a stored id. Neither happens here: the summary is written to a file
named for that apply alone (run id plus a random token, #2802), so the
re-index's new ids are its chunks.
These tests pin that, and pin the case where the append attribution would have
cost the link: chunk overlap with a summary of several chunks leaves it
``unverified``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.server.tools import memory_crud
from test_consolidation_already_consolidated import _ctx, _originals

pytestmark = pytest.mark.asyncio

_LONG = "\n\n".join(f"## Part {i}\n\n" + ("zqword " * 300) for i in range(4))


async def _apply(components, memory_dir: Path, monkeypatch, summary: str):
    from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

    seen = []
    real = memory_crud._mem_add_core

    async def record(*args, **kwargs):
        out = await real(*args, **kwargs)
        seen.append(out[1])
        return out

    monkeypatch.setattr(memory_crud, "_mem_add_core", record)
    originals = await _originals(components, memory_dir / "notes.md", 3)
    ctx = _ctx(components)
    await mem_consolidate(min_group_size=2, ctx=ctx)
    out = await mem_consolidate_apply(group_id=0, summary=summary, ctx=ctx)
    (stats,) = seen
    return out, stats, originals


async def _summary_rows(components, memory_dir: Path) -> list:
    (path,) = [p for p in memory_dir.rglob("*.md") if p.name != "notes.md"]
    return await components.storage.list_chunks_by_source(path)


async def _targets(components, originals) -> set:
    targets = set()
    for chunk in originals:
        for target_id, relation in await components.storage.get_related(chunk.id):
            if relation == "consolidated_into":
                targets.add(target_id)
    return targets


@pytest.mark.parametrize("summary", ["zqshort summary body", _LONG], ids=["one", "several"])
async def test_new_ids_are_the_summary_chunks(components, memory_dir, monkeypatch, summary):
    out, stats, originals = await _apply(components, memory_dir, monkeypatch, summary)

    rows = await _summary_rows(components, memory_dir)
    assert set(stats.new_chunk_ids) == {c.id for c in rows}
    assert stats.appended_attribution == "exact"
    assert list(stats.appended_chunk_ids) == list(stats.new_chunk_ids)
    assert "Originals linked: 3/3" in out
    assert await _targets(components, originals) == {stats.new_chunk_ids[0]}


async def test_overlap_leaves_the_attribution_unverified_and_the_link_stands(
    components, memory_dir, monkeypatch
):
    components.index_engine._config.chunk_overlap_tokens = 32
    out, stats, originals = await _apply(components, memory_dir, monkeypatch, _LONG)

    rows = await _summary_rows(components, memory_dir)
    assert len(rows) > 1
    assert stats.appended_attribution == "unverified"
    assert stats.appended_chunk_ids == ()
    assert set(stats.new_chunk_ids) == {c.id for c in rows}
    assert "Originals linked: 3/3" in out
    assert await _targets(components, originals) == {stats.new_chunk_ids[0]}
