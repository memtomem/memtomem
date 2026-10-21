"""Editing an agent summary keeps its originals covered (#2808).

``mem_consolidate`` leaves out an original with an outgoing ``consolidated_into``
edge, and the edge points at the summary's chunk id. Editing the summary changes
its text, so the re-index gives it a new id and the old row's edges cascade with
it: the group was listed again. A re-index of a summary's own file now carries
those edges to the chunk that replaces it, when that is unambiguous — the file
held exactly one chunk, and it is named like one the apply writes.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID

import pytest

from memtomem.memory_scope import consolidation_file_name, is_consolidation_summary_file


def _ctx(components):
    from tests.test_tools_logic import _fake_ctx

    return _fake_ctx(components)


async def _apply(components, memory_dir: Path, n: int = 3):
    """Index *n* real notes, consolidate them; return (originals, summary chunk)."""
    from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

    notes = memory_dir / "notes.md"
    notes.write_text(
        "".join(f"## Note {i}\n\noriginal note number {i} body.\n\n" for i in range(n)),
        encoding="utf-8",
    )
    await components.index_engine.index_file(notes)
    originals = await components.storage.list_chunks_by_source(notes)
    assert len(originals) == n
    ctx = _ctx(components)
    await mem_consolidate(min_group_size=2, ctx=ctx)
    out = await mem_consolidate_apply(group_id=0, summary="the first summary", ctx=ctx)
    assert f"Originals linked: {n}/{n}" in out
    summary = await components.storage.get_chunk(
        UUID(out.split("Summary chunk id: ")[1].split("\n")[0])
    )
    assert summary is not None
    return originals, summary


async def _targets(components, originals) -> dict[UUID, list[UUID]]:
    return await components.storage.get_consolidation_targets([c.id for c in originals])


async def _listed(components) -> str:
    from memtomem.server.tools.consolidation import mem_consolidate

    return await mem_consolidate(min_group_size=2, ctx=_ctx(components))


async def _only_chunk(components, path: Path):
    (chunk,) = await components.storage.list_chunks_by_source(path)
    return chunk


class TestCarry:
    async def test_editing_the_summary_keeps_its_originals_covered(self, components, memory_dir):
        """The issue's case: apply, ``mem_edit`` the summary, scan."""
        from memtomem.server.tools.memory_crud import mem_edit

        originals, summary = await _apply(components, memory_dir)
        out = await mem_edit(
            chunk_id=str(summary.id), new_content="the corrected summary", ctx=_ctx(components)
        )
        assert "Memory updated" in out

        assert await components.storage.get_chunk(summary.id) is None
        edited = await _only_chunk(components, Path(summary.metadata.source_file))
        assert "the corrected summary" in edited.content
        assert await _targets(components, originals) == {c.id: [edited.id] for c in originals}
        assert "No consolidation candidates found." in await _listed(components)

    async def test_a_hand_edit_and_reindex_keeps_coverage(self, components, memory_dir):
        originals, summary = await _apply(components, memory_dir)
        path = Path(summary.metadata.source_file)
        path.write_text(
            path.read_text(encoding="utf-8").replace("the first summary", "rewritten by hand"),
            encoding="utf-8",
        )
        await components.index_engine.index_file(path)

        edited = await _only_chunk(components, path)
        assert edited.id != summary.id
        assert await _targets(components, originals) == {c.id: [edited.id] for c in originals}

    async def test_a_migrated_summary_keeps_coverage_through_an_edit(
        self, components, memory_dir, tmp_path
    ):
        """``memory-migrate`` moves a file to ``<to_dir>/<same name>`` keeping ids, then edit."""
        from memtomem.server.tools.memory_crud import mem_edit

        originals, summary = await _apply(components, memory_dir)
        src = Path(summary.metadata.source_file)
        to_dir = memory_dir / "moved"
        to_dir.mkdir()
        tgt = (to_dir / src.name).resolve()
        shutil.move(str(src), str(tgt))
        assert await components.storage.update_chunks_scope_for_source(src, tgt, "user", None)
        assert await _targets(components, originals) == {c.id: [summary.id] for c in originals}

        out = await mem_edit(
            chunk_id=str(summary.id), new_content="edited after the move", ctx=_ctx(components)
        )

        assert "Memory updated" in out
        assert await components.storage.get_chunk(summary.id) is None
        edited = await _only_chunk(components, tgt)
        assert "edited after the move" in edited.content
        assert await _targets(components, originals) == {c.id: [edited.id] for c in originals}

    async def test_a_failure_after_the_carry_rolls_it_back(
        self, components, memory_dir, monkeypatch
    ):
        """The carry commits with the delete and the upserts, or not at all."""
        originals, summary = await _apply(components, memory_dir)
        path = Path(summary.metadata.source_file)
        path.write_text(
            path.read_text(encoding="utf-8").replace("the first summary", "rewritten"),
            encoding="utf-8",
        )
        engine = components.index_engine
        carried: list[int] = []
        real_carry = components.storage.carry_consolidation_edges

        async def carry(source_ids, target_id):
            carried.append(await real_carry(source_ids, target_id))
            return carried[-1]

        async def fail_after(_path):
            raise RuntimeError("injected: failure after the carry")

        monkeypatch.setattr(components.storage, "carry_consolidation_edges", carry)
        monkeypatch.setattr(engine, "_record_source_commit", fail_after)
        with pytest.raises(RuntimeError, match="after the carry"):
            await engine.index_file(path)

        assert carried == [3]
        assert await components.storage.get_chunk(summary.id) is not None
        assert await _targets(components, originals) == {c.id: [summary.id] for c in originals}

    async def test_a_row_added_during_the_reindex_cancels_the_carry(
        self, components, memory_dir, monkeypatch
    ):
        """A writer without the file's lock can add a row after the diff's snapshot."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        originals, summary = await _apply(components, memory_dir)
        path = Path(summary.metadata.source_file)
        path.write_text(
            path.read_text(encoding="utf-8").replace("the first summary", "rewritten"),
            encoding="utf-8",
        )
        intruder = Chunk(
            content="imported meanwhile",
            metadata=ChunkMetadata(source_file=summary.metadata.source_file, start_line=90),
            embedding=[0.1] * 1024,
        )
        real_state = storage.get_chunk_index_state
        calls: list[int] = []

        async def state_then_import(source_file):
            state = await real_state(source_file)
            if not calls:
                await storage.upsert_chunks([intruder])
            calls.append(len(state))
            return state

        monkeypatch.setattr(storage, "get_chunk_index_state", state_then_import)
        await components.index_engine.index_file(path)

        assert calls[:2] == [1, 2]
        assert await storage.get_chunk(intruder.id) is not None
        assert await _targets(components, originals) == {}

    async def test_a_failed_carry_rolls_the_reindex_back(self, components, memory_dir, monkeypatch):
        from memtomem.server.tools.memory_crud import mem_edit

        originals, summary = await _apply(components, memory_dir)
        path = Path(summary.metadata.source_file)
        before = path.read_text(encoding="utf-8")

        async def broken(source_ids, target_id):
            raise RuntimeError("injected: carry failed")

        monkeypatch.setattr(components.storage, "carry_consolidation_edges", broken)
        out = await mem_edit(
            chunk_id=str(summary.id), new_content="never lands", ctx=_ctx(components)
        )

        assert out.startswith("Error")
        assert path.read_text(encoding="utf-8") == before
        assert await _targets(components, originals) == {c.id: [summary.id] for c in originals}


class TestNoCarry:
    async def test_deleting_the_summary_lists_its_originals_again(self, components, memory_dir):
        from memtomem.server.tools.memory_crud import mem_delete

        originals, summary = await _apply(components, memory_dir)
        await mem_delete(chunk_id=str(summary.id), ctx=_ctx(components))

        assert await _targets(components, originals) == {}
        assert "notes.md" in await _listed(components)

    @pytest.mark.parametrize("title", ["Another entry", "Consolidated: notes.md"])
    async def test_deleting_the_summary_beside_another_entry_carries_nothing(
        self, components, memory_dir, title
    ):
        """A second entry in the file makes the carry ambiguous, whatever its title."""
        originals, summary = await _apply(components, memory_dir)
        path = Path(summary.metadata.source_file)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {title}\n\n" + "an unrelated entry appended later. " * 400 + "\n")
        await components.index_engine.index_file(path)
        rows = await components.storage.list_chunks_by_source(path)
        assert len(rows) >= 2
        head = rows[0]
        assert "the first summary" in head.content
        # A same-title entry merges into the summary's chunk and re-mints it while
        # the file still held one chunk, so the coverage follows it there.
        assert await _targets(components, originals) == {c.id: [head.id] for c in originals}

        # Remove the summary entry by hand: a chunk spanning a merged entry is a
        # source fragment, which ``mem_delete`` refuses to rewrite.
        text = path.read_text(encoding="utf-8")
        path.write_text(text[text.index("\n## ", text.index("## ") + 1) + 1 :], encoding="utf-8")
        await components.index_engine.index_file(path)

        rest = await components.storage.list_chunks_by_source(path)
        assert rest  # the appended entry survives
        assert await _targets(components, originals) == {}
        assert "notes.md" in await _listed(components)

    async def test_an_edited_original_loses_its_own_coverage(self, components, memory_dir):
        """An original whose text changed is not covered by a summary of its old text."""
        from memtomem.server.tools.memory_crud import mem_edit

        originals, summary = await _apply(components, memory_dir)
        await mem_edit(
            chunk_id=str(originals[0].id), new_content="changed note", ctx=_ctx(components)
        )

        rows = await components.storage.list_chunks_by_source(memory_dir / "notes.md")
        edited = next(c for c in rows if "changed note" in c.content)
        targets = await components.storage.get_consolidation_targets([c.id for c in rows])
        assert edited.id not in targets
        assert targets == {c.id: [summary.id] for c in originals[1:]}

    async def test_a_summary_in_a_day_file_is_not_carried(self, components, memory_dir):
        """The carry is for a summary's own file; a legacy day-file summary keeps today's behaviour."""
        from memtomem.server.tools.memory_crud import mem_edit

        storage = components.storage
        notes = memory_dir / "notes.md"
        notes.write_text("## A\n\nnote a.\n\n## B\n\nnote b.\n", encoding="utf-8")
        await components.index_engine.index_file(notes)
        originals = await storage.list_chunks_by_source(notes)
        day = memory_dir / "2026-10-21.md"
        day.write_text("## Consolidated: notes.md\n\nan old summary.\n", encoding="utf-8")
        await components.index_engine.index_file(day)
        summary = await _only_chunk(components, day)
        for c in originals:
            await storage.add_relation(c.id, summary.id, "consolidated_into")

        await mem_edit(
            chunk_id=str(summary.id), new_content="edited old summary", ctx=_ctx(components)
        )

        assert await _targets(components, originals) == {}


class TestTarget:
    async def test_the_first_stored_chunk_is_the_target(self, components, memory_dir):
        """An upsert the store ignored has no row; the carry skips it for the next chunk.

        Called directly: through ``_index_file`` an ignored upsert needs a row
        already holding its identity, and the in-transaction recount declines
        the carry first. This pins the guard behind that one.
        """
        from memtomem.models import Chunk, ChunkMetadata

        originals, summary = await _apply(components, memory_dir)
        path = Path(summary.metadata.source_file)
        ignored, written = (
            Chunk(content=text, metadata=ChunkMetadata(source_file=path), embedding=[0.1] * 1024)
            for text in ("ignored", "written")
        )
        await components.storage.upsert_chunks([written])
        engine = components.index_engine

        await engine._carry_summary_coverage(
            path, [ignored, written], [], [written], [c.id for c in originals]
        )

        targets = await _targets(components, originals)
        assert all(written.id in found and ignored.id not in found for found in targets.values())

    async def test_an_existing_edge_on_the_target_is_kept(self, components, memory_dir):
        storage = components.storage
        originals, summary = await _apply(components, memory_dir)
        await storage.delete_relation(originals[0].id, summary.id)
        await storage.add_relation(originals[0].id, summary.id, "related")

        linked = await storage.carry_consolidation_edges([c.id for c in originals], summary.id)

        # Two pairs already hold the edge and one holds the user's ``related`` link.
        assert linked == 0
        assert (originals[0].id, "related") in await storage.get_related(summary.id)


class TestName:
    @pytest.mark.parametrize("source", ["notes.md", "한국어.md", "a b.c_d.md", "x" * 120 + ".md"])
    def test_the_name_round_trips_the_writer(self, source):
        written = Path(consolidation_file_name(source, 42, date_str="2026-10-21"))
        assert is_consolidation_summary_file(written)
        assert is_consolidation_summary_file(Path("/elsewhere") / written.name)

    @pytest.mark.parametrize(
        "name",
        [
            "2026-10-21.md",
            "notes.md.consolidated.md",
            "2026-10-21-notes-run42.md",
            "2026-10-21-notes-run42-ABCDEF012345.md",
            "2026-10-21-notes-run42-0123456789ab.md.bak",
        ],
    )
    def test_other_names_are_not_summaries(self, name):
        assert not is_consolidation_summary_file(Path(name))
