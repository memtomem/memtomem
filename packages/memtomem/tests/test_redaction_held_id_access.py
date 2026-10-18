"""A redaction hold withholds a chunk from id access too (#2777).

A redaction hold (#2629) keeps the chunks of a file whose new content the
guard refused. Search already hides them; these tests pin that every surface
that takes a chunk id answers for them exactly as it answers for an id that
does not exist, and that no write reaches them.

Availability holds (a missing or unavailable source) are the deliberate
contrast: their chunks stay readable by id for recovery (#2547). Each refusal
below has a twin that reads the same chunk under such a hold, so a predicate
widened to every hold fails here as surely as a missing one.

The oracle for "answers as a nonexistent id" is the same call with the same
id after the row is deleted, because not-found messages carry the id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.indexing.engine import PrivacyRejection
from memtomem.models import ORIGIN_CONSOLIDATION_POLICY
from memtomem.server.context import AppContext
from memtomem.server.tools import browse, cross_ref, memory_crud, reflection, search
from memtomem.services import tag_management as tag_svc
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD

pytestmark = pytest.mark.asyncio

# Assembled at runtime so the literal never sits in the tree as a key.
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


async def _index(comp, path: Path, body: str):
    path.write_text(body, encoding="utf-8")
    assert not (await comp.index_engine.index_file(path)).errors
    rows = await comp.storage.list_chunks_by_source(path.resolve())
    assert len(rows) == 1, rows
    return rows[0]


@pytest.fixture
def stack(bm25_only_components):
    comp, mem_dir = bm25_only_components
    return comp, mem_dir, StubCtx(AppContext.from_components(comp))


async def _redaction_held(comp, mem_dir: Path, token: str = "tokHeld1"):
    """A chunk held through the engine's own path, its span left intact.

    The secret goes into a new section after the chunk, so the chunk's source
    span still matches the file. That is the shape in which a write would
    otherwise go through (GHSA-6wq5-pc5r-4fgp): the span check passes, the
    file is rewritten and re-indexed past the guard.
    """
    path = mem_dir / f"{token}.md"
    chunk = await _index(comp, path, f"# N\n\n{token} remembered fact.\n")
    path.write_text(f"# N\n\n{token} remembered fact.\n\n## K\n\naws {_SECRET}\n", encoding="utf-8")
    with pytest.raises(PrivacyRejection):
        await comp.index_engine.index_file(path)
    assert await comp.storage.bm25_search(token) == []
    return path, chunk


async def _availability_held(comp, mem_dir: Path, token: str = "tokAvail1"):
    path = mem_dir / f"{token}.md"
    chunk = await _index(comp, path, f"# M\n\n{token} remembered fact.\n")
    assert await comp.storage.hold_source(path, "scan_missing")
    return path, chunk


async def _hold_reason(comp, path: Path) -> str | None:
    from memtomem.storage.sqlite_helpers import norm_path

    row = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (norm_path(path),))
        .fetchone()
    )
    return row[0] if row else None


async def _as_if_absent(comp, chunk_id, call):
    """``call()`` once more after deleting the row: the not-found oracle."""
    await comp.storage.delete_chunks([chunk_id])
    return await call()


class TestStorage:
    async def test_redaction_hold_is_withheld_and_availability_is_not(self, stack):
        comp, mem_dir, _ = stack
        held_path, held = await _redaction_held(comp, mem_dir)
        avail_path, avail = await _availability_held(comp, mem_dir)

        assert await comp.storage.get_chunk(held.id, exclude_withheld=True) is None
        assert (await comp.storage.get_chunk(held.id)).id == held.id
        assert (await comp.storage.get_chunk(avail.id, exclude_withheld=True)).id == avail.id
        batch = await comp.storage.get_chunks_batch([held.id, avail.id], exclude_withheld=True)
        assert list(batch) == [avail.id]
        assert await _hold_reason(comp, held_path) == REDACTION_BLOCKED_HOLD
        assert avail_path.exists()

    async def test_a_release_right_after_the_read_cannot_return_the_old_row(
        self, stack, monkeypatch
    ):
        """Round 3's interleaving: read, then a clean re-index deletes the row
        and releases the hold, then the answer is formed.

        A check run after the read would see no hold and pass the old row on.
        With the predicate in the read, the row was withheld when it was read.
        """
        comp, mem_dir, ctx = stack
        path, held = await _redaction_held(comp, mem_dir)
        real = comp.storage.get_chunk

        async def read_then_release(chunk_id, **kwargs):
            row = await real(chunk_id, **kwargs)
            path.write_text("# N\n\ntokFixed1 fact.\n", encoding="utf-8")
            assert not (await comp.index_engine.index_file(path)).errors
            return row

        monkeypatch.setattr(comp.storage, "get_chunk", read_then_release)

        read = await browse.mem_read(chunk_id=str(held.id), ctx=ctx)

        assert await _hold_reason(comp, path) is None  # the release did land
        assert "tokHeld1" not in read
        assert read == f"Chunk {held.id} not found."

    async def test_a_symlink_put_at_the_held_path_does_not_lift_the_screen(self, stack):
        """The stored path is compared as stored, never re-resolved."""
        comp, mem_dir, ctx = stack
        path, held = await _redaction_held(comp, mem_dir)
        elsewhere = mem_dir / "elsewhere.txt"
        elsewhere.write_text("unrelated\n", encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(elsewhere)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")

        read = await browse.mem_read(chunk_id=str(held.id), ctx=ctx)

        assert "tokHeld1" not in read
        assert read == await _as_if_absent(
            comp, held.id, lambda: browse.mem_read(chunk_id=str(held.id), ctx=ctx)
        )

    async def test_a_policy_summary_follows_its_parent(self, stack):
        comp, mem_dir, ctx = stack
        parents = {}
        for reason in (REDACTION_BLOCKED_HOLD, "scan_missing"):
            name = "red" if reason == REDACTION_BLOCKED_HOLD else "avail"
            parent = mem_dir / f"{name}.md"
            virtual = mem_dir / f"{name}.md.consolidated.md"
            await _index(comp, parent, f"# P\n\n{name}parentword fact.\n")
            summary = await _index(comp, virtual, f"# S\n\n{name}summaryword fact.\n")
            db = comp.storage._get_db()
            db.execute(
                "UPDATE chunks SET origin=? WHERE source_file=?",
                (ORIGIN_CONSOLIDATION_POLICY, str(virtual.resolve())),
            )
            db.commit()
            assert await comp.storage.hold_source(parent, reason)
            parents[name] = summary

        red = await browse.mem_read(chunk_id=str(parents["red"].id), ctx=ctx)
        avail = await browse.mem_read(chunk_id=str(parents["avail"].id), ctx=ctx)
        assert "redsummaryword" not in red
        assert "availsummaryword" in avail
        assert red == await _as_if_absent(
            comp,
            parents["red"].id,
            lambda: browse.mem_read(chunk_id=str(parents["red"].id), ctx=ctx),
        )

    async def test_tag_update_predicate_refuses_a_withheld_row(self, stack):
        """The predicate sits in the UPDATE, so it holds without the service read."""
        comp, mem_dir, _ = stack
        _, held = await _redaction_held(comp, mem_dir)

        result = await comp.storage.replace_chunk_tags(
            held.id, ["new-tag"], project_context_root=None
        )

        assert result is None
        assert (await comp.storage.get_chunk(held.id)).metadata.tags == held.metadata.tags

    async def test_most_connected_does_not_count_a_withheld_endpoint(self, stack):
        comp, mem_dir, _ = stack
        _, held = await _redaction_held(comp, mem_dir)
        hub = await _index(comp, mem_dir / "hub.md", "# H\n\ntokHub1 fact.\n")
        leaf = await _index(comp, mem_dir / "leaf.md", "# L\n\ntokLeaf1 fact.\n")
        await comp.storage.add_relation(hub.id, held.id, "related")
        await comp.storage.add_relation(hub.id, leaf.id, "related")
        await comp.storage.add_relation(held.id, leaf.id, "related")

        rows = await comp.storage.get_most_connected(limit=5, project_context_root=None)

        by_id = {row["chunk_id"]: row["link_count"] for row in rows}
        assert str(held.id) not in by_id
        assert by_id[str(hub.id)] == 1


class TestReads:
    async def test_mem_read_and_mem_expand(self, stack):
        comp, mem_dir, ctx = stack
        _, held = await _redaction_held(comp, mem_dir)
        _, avail = await _availability_held(comp, mem_dir)
        cid = str(held.id)

        read = await browse.mem_read(chunk_id=cid, ctx=ctx)
        expand = await search.mem_expand(chunk_id=cid, ctx=ctx)

        assert "tokHeld1" not in read and "tokHeld1" not in expand
        assert "tokAvail1" in await browse.mem_read(chunk_id=str(avail.id), ctx=ctx)
        assert "tokAvail1" in await search.mem_expand(chunk_id=str(avail.id), ctx=ctx)
        assert read == await _as_if_absent(
            comp, held.id, lambda: browse.mem_read(chunk_id=cid, ctx=ctx)
        )
        assert expand == await search.mem_expand(chunk_id=cid, ctx=ctx)

    @pytest.mark.parametrize("reason", [REDACTION_BLOCKED_HOLD, "scan_missing"])
    async def test_mem_expand_leaves_out_neighbours_held_before_their_read(
        self, stack, monkeypatch, reason
    ):
        """A hold committed between the anchor read and the window read.

        The anchor was read while readable and may be shown; the neighbours
        read under a redaction hold may not. An availability hold landing at
        the same point changes nothing.
        """
        comp, mem_dir, ctx = stack
        path = mem_dir / "win.md"
        body = "".join(f"## S{i}\n\ntokWin{i} " + "word " * 300 + "\n\n" for i in range(3))
        path.write_text(f"# W\n\n{body}", encoding="utf-8")
        assert not (await comp.index_engine.index_file(path)).errors
        rows = await comp.storage.list_chunks_by_source(path.resolve())
        assert len(rows) >= 2, "the window needs a neighbour"
        anchor = next(r for r in rows if "tokWin0" in r.content)
        real = comp.storage.get_context_windows

        async def hold_then_read(*args, **kwargs):
            await comp.storage.hold_source(path, reason)
            return await real(*args, **kwargs)

        monkeypatch.setattr(comp.storage, "get_context_windows", hold_then_read)

        expand = await search.mem_expand(chunk_id=str(anchor.id), ctx=ctx)

        assert "tokWin0" in expand
        if reason == REDACTION_BLOCKED_HOLD:
            assert "tokWin1" not in expand
            assert "### After" not in expand
        else:
            assert "tokWin1" in expand

    async def test_mem_expand_keeps_physical_adjacency_and_counts(self, stack):
        """A withheld row inside the window is dropped, not stepped over.

        One virtual source holding ordinary rows around a policy summary whose
        parent is redaction-held. Filtering before the ``LIMIT`` would return
        the next ordinary row as adjacent; counting without the predicate would
        report a position over rows the caller cannot read.
        """
        comp, mem_dir, ctx = stack
        parent = mem_dir / "mix.md"
        await _index(comp, parent, "# P\n\ntokMixParent fact.\n")
        virtual = mem_dir / "mix.md.consolidated.md"
        body = "".join(f"## S{i}\n\ntokMix{i} " + "word " * 300 + "\n\n" for i in range(3))
        virtual.write_text(f"# M\n\n{body}", encoding="utf-8")
        assert not (await comp.index_engine.index_file(virtual)).errors
        rows = await comp.storage.list_chunks_by_source(virtual.resolve())
        by_token = {t: next(r for r in rows if t in r.content) for t in ("tokMix0", "tokMix1")}
        db = comp.storage._get_db()
        db.execute(
            "UPDATE chunks SET origin=? WHERE id=?",
            (ORIGIN_CONSOLIDATION_POLICY, str(by_token["tokMix1"].id)),
        )
        db.commit()
        assert await comp.storage.hold_source(parent, REDACTION_BLOCKED_HOLD)

        expand = await search.mem_expand(chunk_id=str(by_token["tokMix0"].id), window=1, ctx=ctx)

        assert "tokMix0" in expand
        assert "tokMix1" not in expand
        assert "tokMix2" not in expand
        assert f"chunk 1/{len(rows) - 1} " in expand

    async def test_mem_related_drops_a_withheld_target(self, stack):
        comp, mem_dir, ctx = stack
        _, held = await _redaction_held(comp, mem_dir)
        _, avail = await _availability_held(comp, mem_dir)
        anchor = await _index(comp, mem_dir / "anchor.md", "# A\n\ntokAnchor1 fact.\n")
        await comp.storage.add_relation(anchor.id, held.id, "related")
        await comp.storage.add_relation(anchor.id, avail.id, "related")

        listing = await cross_ref.mem_related(chunk_id=str(anchor.id), ctx=ctx)

        assert "(1 links)" in listing
        assert "tokAvail1" in listing
        assert "tokHeld1" not in listing and str(held.id) not in listing

    async def test_most_connected_rechecks_a_hub_held_after_the_query(self, stack, monkeypatch):
        """A hold committed between ranking and hydration still keeps the preview out."""
        comp, mem_dir, ctx = stack
        hub = await _index(comp, mem_dir / "late.md", "# H\n\ntokLate1 fact.\n")
        for i in range(2):
            leaf = await _index(comp, mem_dir / f"l{i}.md", f"# L\n\ntokLeafX{i} fact.\n")
            await comp.storage.add_relation(hub.id, leaf.id, "related")
        real = comp.storage.get_most_connected

        async def then_hold(*args, **kwargs):
            rows = await real(*args, **kwargs)
            await comp.storage.hold_source(hub.metadata.source_file, REDACTION_BLOCKED_HOLD)
            return rows

        monkeypatch.setattr(comp.storage, "get_most_connected", then_hold)

        report = await reflection.mem_reflect(ctx=ctx)

        assert "tokLate1" not in report


class TestWrites:
    async def test_mem_delete_and_mem_edit_leave_file_and_hold_in_place(self, stack):
        comp, mem_dir, ctx = stack
        path, held = await _redaction_held(comp, mem_dir)
        before = path.read_bytes()
        cid = str(held.id)

        deleted = await memory_crud.mem_delete(chunk_id=cid, ctx=ctx)
        edited = await memory_crud.mem_edit(chunk_id=cid, new_content="tokZ9 edited.", ctx=ctx)

        assert deleted == f"Error: chunk {cid} not found."
        assert edited == f"Error: chunk {cid} not found."
        assert path.read_bytes() == before
        assert await comp.storage.get_chunk(held.id) is not None
        assert await _hold_reason(comp, path) == REDACTION_BLOCKED_HOLD
        assert await comp.storage.bm25_search("aws") == []

    async def test_refused_before_waiting_for_a_busy_lock(self, stack, monkeypatch):
        """The probe answers first, so a busy source cannot change the answer.

        Without it the request would queue on the sidecar and come back as a
        lock timeout, a reply a nonexistent id never gets.
        """
        from memtomem.context import _atomic
        from memtomem.context._atomic import async_memory_file_lock

        comp, mem_dir, ctx = stack
        path, held = await _redaction_held(comp, mem_dir)
        monkeypatch.setattr(_atomic, "_CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

        async with async_memory_file_lock(path, timeout=5):
            result = await memory_crud.mem_delete(chunk_id=str(held.id), ctx=ctx)

        assert result == f"Error: chunk {held.id} not found."

    async def test_a_hold_committed_while_waiting_for_the_lock_refuses_the_write(
        self, stack, monkeypatch
    ):
        comp, mem_dir, ctx = stack
        path = mem_dir / "race.md"
        chunk = await _index(comp, path, "# R\n\ntokRace1 fact.\n")
        before = path.read_bytes()
        real = comp.storage.get_chunk
        calls = 0

        async def hold_after_probe(chunk_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:  # the re-fetch under the lock
                await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
            return await real(chunk_id, **kwargs)

        monkeypatch.setattr(comp.storage, "get_chunk", hold_after_probe)

        result = await memory_crud.mem_delete(chunk_id=str(chunk.id), ctx=ctx)

        assert calls == 2
        assert result == f"Error: chunk {chunk.id} not found."
        assert path.read_bytes() == before

    async def test_mem_link_and_mem_unlink_write_no_relation(self, stack):
        comp, mem_dir, ctx = stack
        _, held = await _redaction_held(comp, mem_dir)
        other = await _index(comp, mem_dir / "o.md", "# O\n\ntokOther1 fact.\n")
        await comp.storage.add_relation(other.id, held.id, "related")

        linked = await cross_ref.mem_link(
            source_id=str(other.id), target_id=str(held.id), relation_type="supports", ctx=ctx
        )
        unlinked = await cross_ref.mem_unlink(
            source_id=str(other.id), target_id=str(held.id), ctx=ctx
        )

        assert "not found" in linked and "not found" in unlinked
        assert await comp.storage.get_related(other.id) == [(held.id, "related")]

    async def test_mem_increment_access_skips_only_the_withheld_id(self, stack):
        comp, mem_dir, ctx = stack
        _, held = await _redaction_held(comp, mem_dir)
        _, avail = await _availability_held(comp, mem_dir)
        plain = await _index(comp, mem_dir / "p.md", "# P\n\ntokPlain1 fact.\n")

        await search.mem_increment_access(
            chunk_ids=[str(held.id), str(avail.id), str(plain.id)], ctx=ctx
        )

        db = comp.storage._get_db()
        counts = {
            cid: db.execute("SELECT access_count FROM chunks WHERE id=?", (str(cid),)).fetchone()[0]
            for cid in (held.id, avail.id, plain.id)
        }
        assert counts == {held.id: 0, avail.id: 1, plain.id: 1}

    async def test_mem_reflect_save_links_only_the_readable_ids(self, stack):
        comp, mem_dir, ctx = stack
        _, held = await _redaction_held(comp, mem_dir)
        _, avail = await _availability_held(comp, mem_dir)

        await reflection.mem_reflect_save(
            insight="tokInsight1 pattern.", related_chunks=[str(held.id), str(avail.id)], ctx=ctx
        )

        assert await comp.storage.get_related(held.id) == []
        assert len(await comp.storage.get_related(avail.id)) == 1

    @pytest.mark.parametrize("same_tags", [False, True])
    async def test_tag_service_refuses_a_withheld_chunk(self, stack, same_tags):
        """Both the write and the no-op shortcut, which returns the row unwritten."""
        comp, mem_dir, _ = stack
        _, held = await _redaction_held(comp, mem_dir)
        tags = list(held.metadata.tags) if same_tags else ["t"]

        result = await tag_svc.replace_chunk_tags(
            comp.storage, held.id, tags, project_context_root=None
        )

        assert result is None
        assert (await comp.storage.get_chunk(held.id)).metadata.tags == held.metadata.tags

    @pytest.mark.parametrize("hold_at_fetch", [1, 2])
    async def test_locked_source_chunk_screens_both_fetches(
        self, stack, monkeypatch, hold_at_fetch
    ):
        """The web and LangGraph write span: the probe, and the re-fetch under the lock."""
        from memtomem.tools.memory_mutation import locked_source_chunk

        comp, mem_dir, _ = stack
        path = mem_dir / "span.md"
        chunk = await _index(comp, path, "# S\n\ntokSpan1 fact.\n")
        real = comp.storage.get_chunk
        calls = 0

        async def hold_at(chunk_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == hold_at_fetch:
                await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
            return await real(chunk_id, **kwargs)

        monkeypatch.setattr(comp.storage, "get_chunk", hold_at)

        async with locked_source_chunk(
            comp.storage, chunk.id, project_context_root=None, index_guard=None
        ) as (got, reason, _held):
            assert got is None
            assert reason == "not_found"
        assert calls == hold_at_fetch

    @pytest.mark.parametrize("hold", [True, False])
    async def test_web_edit_response_never_echoes_a_row_read_under_the_hold(
        self, stack, monkeypatch, hold
    ):
        """A hold committed after the write, before the response re-fetch.

        The re-fetch reads under the hold and gets nothing, so the response is
        the route's ordinary fallback: the pre-edit row, read while readable.
        The edited row is never echoed. Without a hold, the same fallback.
        """
        from memtomem.web.routes import chunks as chunk_routes
        from memtomem.web.schemas.sources import EditRequest

        comp, mem_dir, _ = stack
        path = mem_dir / "web.md"
        chunk = await _index(comp, path, "# W\n\ntokWeb1 fact.\n")
        real = chunk_routes.resolve_visible_chunk
        calls = 0

        async def response_fetch(storage, chunk_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:  # the response re-fetch, after the edit landed
                if not hold:
                    return None
                await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
            return await real(storage, chunk_id, **kwargs)

        monkeypatch.setattr(chunk_routes, "resolve_visible_chunk", response_fetch)

        out = await chunk_routes.edit_chunk(
            chunk.id,
            EditRequest(new_content="tokWeb2 edited."),
            storage=comp.storage,
            index_engine=comp.index_engine,
            search_pipeline=comp.search_pipeline,
            config=comp.config,
        )

        assert out.content == chunk.content
        assert "tokWeb2" not in out.content
        assert calls == 2


class TestRelease:
    async def test_a_clean_reindex_makes_the_chunk_readable_again(self, stack):
        comp, mem_dir, ctx = stack
        path, held = await _redaction_held(comp, mem_dir)
        path.write_text("# N\n\ntokHeld1 remembered fact.\n", encoding="utf-8")
        assert not (await comp.index_engine.index_file(path)).errors

        assert await _hold_reason(comp, path) is None
        assert "tokHeld1" in await browse.mem_read(chunk_id=str(held.id), ctx=ctx)
