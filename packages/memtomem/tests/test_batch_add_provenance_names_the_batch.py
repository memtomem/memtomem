"""``mem_batch_add``'s provenance event names the chunks its batch became (#3315).

The batch recorded ``IndexingStats.new_chunk_ids``, the whole file's re-index:
a section another writer had appended and nobody had indexed was recorded as
this session's, and an entry whose chunk kept an existing id by content hash
was missing from the event, with nothing marking the session either way. The
batch now passes one ``append_region`` for all its entries, so the event lists
the chunks the indexer attributes to the batch. A batch of only empty entries
writes nothing and records nothing.

Every test goes through the real ``mem_batch_add`` with a session started, on a
BM25-only store, and compares the recorded ids with the rows the store holds.
Rows are listed without the default limit, and each expected row is counted so
a token that misses a fragment cannot pass for the whole set.
"""

from __future__ import annotations

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools._provenance import PROVENANCE_KIND

# Over the default ``min_chunk_tokens`` on its own, under ``max_chunk_tokens``,
# so a section of it is one chunk that does not merge with its neighbours.
LONG = " ".join(f"word{i}" for i in range(260))


@pytest.fixture
async def session(bm25_only_components):
    from memtomem.server.tools.session import mem_session_end, mem_session_start

    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_session_start(ctx=ctx)  # type: ignore[arg-type]
    session_id = app.current_session_id
    assert session_id is not None
    yield app, ctx, mem_dir, session_id
    await mem_session_end(ctx=ctx)  # type: ignore[arg-type]


@pytest.fixture
def seen_stats(session):
    """Every ``IndexingStats`` the engine returns during the test."""
    app = session[0]
    seen: list = []
    index_file = app.index_engine.index_file

    async def spy(*args, **kwargs):
        stats = await index_file(*args, **kwargs)
        seen.append(stats)
        return stats

    app.index_engine.index_file = spy  # type: ignore[method-assign]
    return seen


async def _batch(ctx, entries, **kwargs) -> str:
    from memtomem.server.tools.memory_crud import mem_batch_add

    result = await mem_batch_add(entries=entries, ctx=ctx, **kwargs)  # type: ignore[arg-type]
    assert not result.startswith("Error"), result
    return result


async def _recorded(app, session_id) -> list[list[str]]:
    """Each ``batch_add`` provenance event's ids, in event order."""
    return [
        e["chunk_ids"]
        for e in await app.storage.get_session_events(session_id)
        if (e.get("metadata") or {}).get("provenance") == PROVENANCE_KIND
        and e["event_type"] == "batch_add"
    ]


async def _incomplete(app, session_id) -> bool:
    row = await app.storage.get_session(session_id)
    return bool(row["metadata"].get("provenance_incomplete"))


async def _ids_holding(app, path, token: str, *, count: int) -> list[str]:
    """The ids of the rows holding ``token``, in file order; exactly ``count`` of them."""
    rows = sorted(
        (
            c
            for c in await app.storage.list_chunks_by_source(path, limit=None)
            if token in c.content
        ),
        key=lambda c: c.metadata.start_line,
    )
    assert len(rows) == count, [c.content[:40] for c in rows]
    return [str(c.id) for c in rows]


async def _indexed(app, path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    await app.index_engine.index_file(path, ingress_guarded=True)


def _append_raw(path, text: str) -> None:
    """Another writer's bytes, not indexed: a pending edit."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


class TestTheEventNamesTheBatch:
    @pytest.mark.asyncio
    async def test_a_pending_section_is_not_recorded(self, session):
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _indexed(app, path, "## Before\n\nzqbefore " + LONG + "\n")
        _append_raw(path, "\n## Pending\n\nzqpending " + LONG + "\n")

        await _batch(
            ctx,
            [{"key": "One", "value": "zqown " + LONG}, {"key": "Two", "value": "zqown " + LONG}],
            file="a.md",
        )

        assert await _recorded(app, sid) == [await _ids_holding(app, path, "zqown", count=2)]
        await _ids_holding(app, path, "zqpending", count=1)
        assert not await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_an_entry_that_keeps_an_existing_id_is_recorded(self, session):
        # The pending removal makes the re-added entry's chunk match the stored
        # row by content hash, so it keeps that id and is absent from
        # ``new_chunk_ids``: before #3315 the event named only the new entry.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _indexed(app, path, "## Before\n\nzqbefore " + LONG + "\n")
        same = {"key": "Same", "value": "zqsame " + LONG, "tags": ["t"]}
        await _batch(ctx, [same], file="a.md")
        (kept,) = await _ids_holding(app, path, "zqsame", count=1)
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: text.index("## Same")], encoding="utf-8")

        await _batch(
            ctx, [same, {"key": "New", "value": "zqnew " + LONG, "tags": ["t"]}], file="a.md"
        )

        assert await _ids_holding(app, path, "zqsame", count=1) == [kept]
        (new,) = await _ids_holding(app, path, "zqnew", count=1)
        assert (await _recorded(app, sid))[1] == [kept, new]
        assert not await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_a_heading_led_entry_in_a_new_file_is_exact(self, session, seen_stats):
        # The entry's header and its own heading open one chunk at line 1, the
        # blank line the block begins with; that line is the batch's too.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"

        await _batch(ctx, [{"key": "", "value": "## Own\n\nzqown " + LONG}], file="a.md")

        assert seen_stats[-1].appended_attribution == "exact"
        assert await _recorded(app, sid) == [await _ids_holding(app, path, "zqown", count=1)]
        assert not await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_more_entries_than_one_listing_page(self, session):
        app, ctx, mem_dir, sid = session

        await _batch(ctx, [{"key": "", "value": f"zqmany{i:03d} short"} for i in range(120)])

        (path,) = list(mem_dir.rglob("*.md"))
        assert await _recorded(app, sid) == [await _ids_holding(app, path, "zqmany", count=120)]
        assert not await _incomplete(app, sid)


class TestWhatTheEventCannotSay:
    @pytest.mark.asyncio
    async def test_a_merge_into_a_pending_section_flags_the_session(self, session):
        # The first entry continues an unindexed section under the same heading,
        # in one chunk. Nothing is deleted, so no rule saw it before #3315.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _indexed(app, path, "## Other\n\nzqother " + LONG + "\n")
        _append_raw(path, "\n## Same\n\nzqpending short\n")

        await _batch(ctx, [{"key": "Same", "value": "zqown short"}], file="a.md")

        (merged,) = await _ids_holding(app, path, "zqown", count=1)
        assert merged in await _ids_holding(app, path, "zqpending", count=1)
        assert await _recorded(app, sid) == [[merged]]
        assert await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_an_unverified_batch_keeps_the_whole_reindex(self, session, seen_stats):
        # Overlap copies text between neighbours without moving their spans, so
        # the indexer does not attribute the batch: the event lists
        # ``new_chunk_ids`` as before.
        app, ctx, mem_dir, sid = session
        app.index_engine._config.chunk_overlap_tokens = 32

        await _batch(
            ctx,
            [{"key": "One", "value": "zqone " + LONG}, {"key": "Two", "value": "zqtwo " + LONG}],
            file="a.md",
        )

        stats = seen_stats[-1]
        assert stats.appended_attribution == "unverified"
        assert await _recorded(app, sid) == [[str(i) for i in stats.new_chunk_ids]]

    @pytest.mark.asyncio
    async def test_a_batch_of_only_empty_entries_records_nothing(self, session, seen_stats):
        # It wrote nothing; the re-index's only new chunk is the other writer's.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _indexed(app, path, "## Before\n\nzqbefore " + LONG + "\n")
        _append_raw(path, "\n## Pending\n\nzqpending " + LONG + "\n")

        await _batch(ctx, [{"key": "Empty", "value": ""}], file="a.md")

        assert [str(i) for i in seen_stats[-1].new_chunk_ids] == await _ids_holding(
            app, path, "zqpending", count=1
        )
        assert await _recorded(app, sid) == []
        assert not await _incomplete(app, sid)
