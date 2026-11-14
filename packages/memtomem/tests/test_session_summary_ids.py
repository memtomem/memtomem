"""A session summary's chunk id comes from ``new_chunk_ids`` (#3315).

The append callers moved to ``appended_chunk_ids`` (#3292) because re-indexing
a shared file also upserts a pending edit's chunk and misses a chunk that keeps
a stored id. Neither happens to a summary that this call indexes first: it
replaces a file named for its session alone, so the new ids are its chunks.
These tests pin that, including the case where the append attribution would
have lost the id (chunk overlap and a summary of several chunks leave it
``unverified``). A file another indexer reached first gets no id and keeps
that indexer's namespace; the last test pins that until #3340 fixes it.
"""

from __future__ import annotations

import uuid

import pytest

from memtomem.indexing.append_attribution import AppendRegion
from memtomem.indexing.redaction_exemption import indexer_text
from memtomem.server.context import AppContext
from memtomem.server.tools import session

pytestmark = pytest.mark.asyncio

_LONG = "\n\n".join(f"## Part {i}\n\n" + ("zqword " * 300) for i in range(4))


def _prepare(app: AppContext, summary: str):
    return session._prepare_summary_archive(
        app, session_id=str(uuid.uuid4()), agent_id=None, summary=summary, event_counts={}
    )


async def _rows(app: AppContext, prepared) -> list:
    rows = await app.storage.list_chunks_by_source(prepared.target, limit=None)
    return sorted(rows, key=lambda c: c.metadata.start_line)


async def _write(app: AppContext, prepared, monkeypatch):
    seen = []
    real = app.index_engine.index_file

    async def record(*args, **kwargs):
        stats = await real(*args, **kwargs)
        seen.append(stats)
        return stats

    monkeypatch.setattr(app.index_engine, "index_file", record)
    line, chunk_id = await session._write_summary_archive(app, prepared)
    (stats,) = seen
    return line, chunk_id, stats


@pytest.mark.parametrize("overlap", [0, 32])
@pytest.mark.parametrize(
    ("summary", "several"), [("zqshort summary body", False), (_LONG, True)], ids=["one", "several"]
)
async def test_the_new_ids_are_the_summary_in_file_order(
    bm25_only_components, monkeypatch, overlap, summary, several
):
    components, _ = bm25_only_components
    components.index_engine._config.chunk_overlap_tokens = overlap
    app = AppContext.from_components(components)
    prepared = _prepare(app, summary)

    line, chunk_id, stats = await _write(app, prepared, monkeypatch)

    rows = await _rows(app, prepared)
    assert (len(rows) > 1) is several
    assert list(stats.new_chunk_ids) == [r.id for r in rows]
    assert chunk_id == rows[0].id
    assert rows[0].metadata.start_line == 1
    assert {r.metadata.namespace for r in rows} == {prepared.namespace}
    assert line == f"- Summary chunk: {prepared.namespace} ({len(rows)} chunks)"


async def test_overlap_leaves_the_whole_file_attribution_unverified(bm25_only_components):
    """The contract #3315 suggested for this caller (the whole file as the
    expected text, region at 0) has no ids to give once overlap is on."""
    components, _ = bm25_only_components
    components.index_engine._config.chunk_overlap_tokens = 32
    app = AppContext.from_components(components)
    prepared = _prepare(app, _LONG)
    prepared.target.parent.mkdir(parents=True, exist_ok=True)
    prepared.target.write_bytes(prepared.content.encode("utf-8"))
    region = AppendRegion(
        expected_text=indexer_text(prepared.content.encode("utf-8"), errors="replace"), start=0
    )

    stats = await app.index_engine.index_file(
        prepared.target, namespace=prepared.namespace, append_region=region
    )

    rows = await _rows(app, prepared)
    assert len(rows) > 1
    assert stats.appended_attribution == "unverified"
    assert stats.appended_chunk_ids == ()
    assert stats.new_chunk_ids[0] == rows[0].id


async def test_a_file_another_indexer_reached_first_gets_no_archive_id(
    bm25_only_components,
):
    """Today's behaviour, pinned so a fix for #3340 has to change it: the
    rows keep the first indexer's namespace and the summary gets no id."""
    components, _ = bm25_only_components
    app = AppContext.from_components(components)
    prepared = _prepare(app, "zqshort summary body")
    prepared.target.parent.mkdir(parents=True, exist_ok=True)
    prepared.target.write_bytes(prepared.content.encode("utf-8"))
    await app.index_engine.index_file(prepared.target)
    (before,) = await _rows(app, prepared)
    assert before.metadata.namespace == "default"

    line, chunk_id = await session._write_summary_archive(app, prepared)

    (after,) = await _rows(app, prepared)
    assert after.id == before.id
    assert after.metadata.namespace == "default"
    assert (line, chunk_id) == (None, None)
