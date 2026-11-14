"""A session's write provenance names the chunks an append became (#3315).

``record_write_provenance`` used to record ``IndexingStats.new_chunk_ids``, the
chunks the whole file's re-index upserted. For an append that set holds a
pending edit's chunk, and misses the append's own chunk when it kept an
existing id, so a session could name text it did not write, or have no event
for a write it made, with nothing flagging either. The recorder now takes the
indexer's ``appended_chunk_ids`` whenever it attributed the append (#3292), and
a ``merged`` attribution marks the session ``provenance_incomplete`` even when
nothing was deleted. No existing flag is relaxed.

The tool tests go through the real ``mem_add`` with a session started, on a
BM25-only store, and assert recorded ids against the rows the store holds.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import _provenance
from memtomem.server.tools._provenance import PROVENANCE_KIND, record_write_provenance

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


async def _add(ctx, **kwargs) -> str:
    from memtomem.server.tools.memory_crud import mem_add

    result = await mem_add(ctx=ctx, **kwargs)  # type: ignore[arg-type]
    assert not result.startswith("Error"), result
    return result


async def _recorded(app, session_id) -> list[list[str]]:
    """Each provenance event's ids, in event order."""
    return [
        e["chunk_ids"]
        for e in await app.storage.get_session_events(session_id)
        if (e.get("metadata") or {}).get("provenance") == PROVENANCE_KIND
    ]


async def _incomplete(app, session_id) -> bool:
    row = await app.storage.get_session(session_id)
    return bool(row["metadata"].get("provenance_incomplete"))


async def _id_holding(app, path, *tokens) -> str:
    (chunk_id,) = [
        str(c.id)
        for c in await app.storage.list_chunks_by_source(path)
        if all(t in c.content for t in tokens)
    ]
    return chunk_id


def _append_raw(path, text: str) -> None:
    """Another writer's bytes, not indexed: a pending edit."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# What the record names
# ---------------------------------------------------------------------------


class TestTheRecordNamesTheAppend:
    @pytest.mark.asyncio
    async def test_a_pending_section_is_not_recorded(self, session):
        # Before #3315 the second event named the pending chunk too, and the
        # session stayed unflagged: nothing was deleted.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="One", file="a.md")
        _append_raw(path, "\n## Pending\n\nzqpending " + LONG + "\n")

        await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        one = await _id_holding(app, path, "zqone")
        two = await _id_holding(app, path, "zqtwo")
        assert await _recorded(app, sid) == [[one], [two]]
        assert not await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_an_entry_that_keeps_an_existing_id_is_recorded(self, session):
        # The pending removal makes the appended chunk match the stored row by
        # content hash, so it keeps that id and ``new_chunk_ids`` is empty:
        # before #3315 this write recorded no event at all.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        path.write_text("## Before\n\nzqbefore " + LONG + "\n", encoding="utf-8")
        await app.index_engine.index_file(path, ingress_guarded=True)
        await _add(ctx, content="zqsame " + LONG, title="Same", tags=["t"], file="a.md")
        kept = await _id_holding(app, path, "zqsame")
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: text.index("## Same")], encoding="utf-8")

        await _add(ctx, content="zqsame " + LONG, title="Same", tags=["t"], file="a.md")

        assert await _id_holding(app, path, "zqsame") == kept
        assert await _recorded(app, sid) == [[kept], [kept]]
        assert not await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_untitled_day_file_appends_each_name_their_own_chunk(self, session):
        # The default path: every untitled entry gets a heading of its own, so
        # short entries do not merge and the attribution is exact.
        app, ctx, mem_dir, sid = session
        for token in ("zqdayone", "zqdaytwo", "zqdaythree"):
            await _add(ctx, content=f"{token} short note")

        (path,) = [p for p in mem_dir.rglob("*.md")]
        expected = [
            [await _id_holding(app, path, t)] for t in ("zqdayone", "zqdaytwo", "zqdaythree")
        ]
        assert await _recorded(app, sid) == expected
        assert not await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_an_entry_split_into_several_chunks_is_recorded_in_file_order(self, session):
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _add(ctx, content="zqbefore " + LONG, title="Before", file="a.md")
        # Each paragraph alone is over the default ``max_chunk_tokens``.
        paragraphs = [f"zqpart{i} " + "filler words here " * 140 for i in range(3)]
        _append_raw(path, "\n## Pending\n\nzqpending " + LONG + "\n")

        await _add(ctx, content="\n\n".join(paragraphs), title="Long", file="a.md")

        rows = sorted(
            (c for c in await app.storage.list_chunks_by_source(path) if "zqpart" in c.content),
            key=lambda c: c.metadata.start_line,
        )
        assert len(rows) > 1
        events = [
            e
            for e in await app.storage.get_session_events(sid)
            if (e.get("metadata") or {}).get("provenance") == PROVENANCE_KIND
        ]
        assert events[1]["chunk_ids"] == [str(c.id) for c in rows]
        assert events[1]["metadata"]["chunk_count"] == len(rows)
        assert not await _incomplete(app, sid)


# ---------------------------------------------------------------------------
# When the session says its record is not exact
# ---------------------------------------------------------------------------


class TestTheSessionIsFlagged:
    @pytest.mark.asyncio
    async def test_a_merge_into_a_pending_section(self, session):
        # The appended entry continues an unindexed section under the same
        # heading, in one chunk. Nothing is deleted, so before #3315 no rule
        # saw it and the record named the other writer's text as this one's.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="Other", file="a.md")
        _append_raw(path, "\n## Same\n\nzqpending short\n")

        await _add(ctx, content="zqtwo short", title="Same", file="a.md")

        merged = await _id_holding(app, path, "zqpending", "zqtwo")
        assert (await _recorded(app, sid))[1] == [merged]
        assert await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_a_pending_edit_of_an_earlier_section_still_flags(self, session):
        # The append itself is exact, but the edit deleted a chunk: an id an
        # earlier event recorded may now hold other text. The deletion rule
        # stays for every attribution.
        app, ctx, mem_dir, sid = session
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="One", file="a.md")
        path.write_text(
            path.read_text(encoding="utf-8").replace("zqone", "zqedited"), encoding="utf-8"
        )

        await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        assert (await _recorded(app, sid))[1] == [await _id_holding(app, path, "zqtwo")]
        assert await _incomplete(app, sid)

    @pytest.mark.asyncio
    async def test_an_unverified_append_keeps_the_whole_reindex(self, session):
        # Overlap copies text between neighbours without moving their spans,
        # so the indexer does not attribute the append: the record keeps
        # ``new_chunk_ids`` and the deletion the overlap rewrite causes flags it.
        app, ctx, mem_dir, sid = session
        app.index_engine._config.chunk_overlap_tokens = 32
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="One", file="a.md")

        await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        second = (await _recorded(app, sid))[1]
        assert await _id_holding(app, path, "zqone") in second
        assert len(second) == 2
        assert await _incomplete(app, sid)


# ---------------------------------------------------------------------------
# The recorder's selection, driven directly
# ---------------------------------------------------------------------------


class _Stats:
    def __init__(self, new, appended=(), attribution="unverified"):
        self.new_chunk_ids = tuple(new)
        self.appended_chunk_ids = tuple(appended)
        self.appended_attribution = attribution


class TestRecorderSelection:
    async def _app(self, components, session_id="s1"):
        app = AppContext.from_components(components)
        await app.storage.create_session(session_id, "planner", "default")
        app.current_session_id = session_id
        return app

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attribution", ["exact", "merged"])
    async def test_an_attributed_append_records_its_own_ids(self, components, attribution):
        app = await self._app(components)
        new, own = [uuid4(), uuid4()], [uuid4()]

        await record_write_provenance(
            app, session_id="s1", event_type="add", stats=_Stats(new, own, attribution)
        )

        (event,) = await app.storage.get_session_events("s1")
        assert event["chunk_ids"] == [str(own[0])]
        assert event["metadata"]["chunk_count"] == 1

    @pytest.mark.asyncio
    async def test_an_unverified_append_records_the_reindex(self, components):
        # ``appended_chunk_ids`` is empty whenever the engine says unverified;
        # a populated one here proves the attribution, not the list, decides.
        app = await self._app(components)
        new = [uuid4(), uuid4()]

        await record_write_provenance(
            app, session_id="s1", event_type="add", stats=_Stats(new, [uuid4()], "unverified")
        )

        (event,) = await app.storage.get_session_events("s1")
        assert event["chunk_ids"] == [str(i) for i in new]

    @pytest.mark.asyncio
    async def test_stats_without_attribution_record_the_reindex(self, components):
        class _Bare:
            new_chunk_ids = (uuid4(),)

        app = await self._app(components)

        await record_write_provenance(app, session_id="s1", event_type="index", stats=_Bare())

        (event,) = await app.storage.get_session_events("s1")
        assert event["chunk_ids"] == [str(_Bare.new_chunk_ids[0])]

    @pytest.mark.asyncio
    async def test_truncation_counts_the_selected_ids(self, components, monkeypatch):
        monkeypatch.setattr(_provenance, "MAX_IDS_PER_EVENT", 3)
        app = await self._app(components)
        own = [uuid4() for _ in range(5)]

        await record_write_provenance(
            app, session_id="s1", event_type="add", stats=_Stats([uuid4()], own, "exact")
        )

        (event,) = await app.storage.get_session_events("s1")
        assert event["chunk_ids"] == [str(i) for i in own[:3]]
        assert event["metadata"]["chunk_count"] == 5
        assert event["metadata"]["truncated"] is True
        assert (await app.storage.get_session("s1"))["metadata"]["provenance_incomplete"] is True
