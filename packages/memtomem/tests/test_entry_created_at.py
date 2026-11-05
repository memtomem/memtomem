"""A chunk is dated by its entry's ``> created:`` line (ADR-0043 §5, #3051).

A rebuilt index used to date every chunk by the re-index run, so date-ranged
recall and newest-first ordering followed the rebuild. The chunker now reads
the entry's write time into ``ChunkMetadata.entry_created_at``, merging and
splitting carry it, and ``chunk_content`` copies it onto ``Chunk.created_at``.
The store's update path never writes ``created_at``, so only rows it creates
take the value.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memtomem.chunking.markdown import MarkdownChunker

T1 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
T2 = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)


def _chunks(text: str):
    return MarkdownChunker().chunk_file(Path("/tmp/notes.md"), text)


class TestChunkerReadsTheEntryTime:
    def test_a_header_with_tags_dates_the_chunk_and_is_stripped(self) -> None:
        (chunk,) = _chunks(f'## A\n\n> created: {T1.isoformat()}\n> tags: ["x"]\n\nbody\n')

        assert chunk.metadata.entry_created_at == T1
        assert chunk.content == "body"

    def test_a_header_without_tags_dates_the_chunk_and_stays_in_the_content(self) -> None:
        """The content, and so the hash and id, of a tag-less entry is what it
        was before: only the date is new."""
        (chunk,) = _chunks(f"## A\n\n> created: {T1.isoformat()}\n\nbody\n")

        assert chunk.metadata.entry_created_at == T1
        assert chunk.content == f"> created: {T1.isoformat()}\n\nbody"

    def test_an_entry_without_a_header_has_no_entry_time(self) -> None:
        (chunk,) = _chunks("## A\n\nbody\n")
        assert chunk.metadata.entry_created_at is None

    def test_a_quoted_created_line_mid_section_does_not_count(self) -> None:
        (chunk,) = _chunks(f"## A\n\nbody\n\n> created: {T1.isoformat()}\n")
        assert chunk.metadata.entry_created_at is None

    def test_an_entry_that_opens_with_its_own_heading_is_not_dated_yet(self) -> None:
        """The documented limitation (#2762): the writer puts the header above
        content's own ``## `` heading, where it leads no section of the entry.
        When #2762 moves the header, this flips and the docs change with it."""
        from memtomem.tools.memory_writer import format_entry_block

        block = format_entry_block("## Mine\n\nbody", tags=["t"])
        (chunk,) = _chunks(block)

        assert chunk.metadata.heading_hierarchy == ("## Mine",)
        assert chunk.metadata.entry_created_at is None
        assert chunk.metadata.tags == ()

    def test_a_value_that_is_not_a_time_is_ignored(self) -> None:
        (chunk,) = _chunks("## A\n\n> created: yesterday\n\nbody\n")
        assert chunk.metadata.entry_created_at is None

    @pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
    def test_a_time_that_overflows_in_utc_is_ignored_not_raised(self, value: str) -> None:
        (chunk,) = _chunks(f"## A\n\n> created: {value}\n\nbody\n")
        assert chunk.metadata.entry_created_at is None

    def test_a_time_without_an_offset_is_utc(self) -> None:
        (chunk,) = _chunks("## A\n\n> created: 2026-01-02T03:04:05\n\nbody\n")
        assert chunk.metadata.entry_created_at == T1

    def test_an_offset_is_normalised_to_utc(self) -> None:
        local = T1.astimezone(timezone(timedelta(hours=9)))
        (chunk,) = _chunks(f"## A\n\n> created: {local.isoformat()}\n\nbody\n")
        assert chunk.metadata.entry_created_at == T1
        assert chunk.metadata.entry_created_at.tzinfo == timezone.utc

    def test_nested_parts_inherit_the_entry_time_and_a_sibling_does_not(self) -> None:
        text = (
            f'## Entry\n\n> created: {T1.isoformat()}\n> tags: ["x"]\n\nintro\n\n'
            "### Part\n\npart body\n\n"
            "## Other\n\nother body\n"
        )
        by_heading = {c.metadata.heading_hierarchy[-1]: c for c in _chunks(text)}

        assert by_heading["## Entry"].metadata.entry_created_at == T1
        assert by_heading["### Part"].metadata.entry_created_at == T1
        assert by_heading["## Other"].metadata.entry_created_at is None

    def test_an_oversized_entry_dates_every_piece(self) -> None:
        # About 1,800 tokens against the default 512-token section budget.
        body = "\n\n".join(f"paragraph {i} " + "word " * 120 for i in range(12))
        chunks = _chunks(f'## Big\n\n> created: {T1.isoformat()}\n> tags: ["x"]\n\n{body}\n')

        assert len(chunks) > 1
        assert {c.metadata.entry_created_at for c in chunks} == {T1}


class TestMergedChunksTakeTheEarliestEntry:
    def test_two_short_entries_under_one_title_merge_with_the_earlier_time(self) -> None:
        from memtomem.indexing.engine import _merge_pair

        first, second = _chunks(
            f"## Shared\n\n> created: {T2.isoformat()}\n\nlater\n\n"
            f"## Shared\n\n> created: {T1.isoformat()}\n\nearlier\n"
        )
        merged = _merge_pair(first, second)

        assert merged.metadata.entry_created_at == T1

    def test_a_merge_with_an_undated_chunk_keeps_the_dated_one(self) -> None:
        from memtomem.indexing.engine import _merge_pair

        first, second = _chunks(
            f"## A\n\nundated\n\n## B\n\n> created: {T2.isoformat()}\n\ndated\n"
        )

        assert _merge_pair(first, second).metadata.entry_created_at == T2


def _entry(created: datetime, body: str, title: str = "Note") -> str:
    return f'## {title}\n\n> created: {created.isoformat()}\n> tags: ["x"]\n\n{body}\n'


async def _stored_created(components, fp: Path) -> list[datetime]:
    chunks = await components.storage.list_chunks_by_source(fp)
    return sorted(c.created_at.astimezone(timezone.utc) for c in chunks)


class TestTheStoreDatesNewRowsByTheEntry:
    async def test_a_rebuild_dates_chunks_by_their_entries(self, components, memory_dir) -> None:
        """The #3051 headline for dates: lose the rows, re-index, and the
        timeline is where it was."""
        fp = memory_dir / "notes.md"
        fp.write_text(_entry(T1, "first entry") + "\n" + _entry(T2, "second", "Other"), "utf-8")
        engine = components.index_engine
        await engine.index_file(fp)
        await components.storage.delete_by_source(fp)

        await engine.index_file(fp, force=True)

        assert await _stored_created(components, fp) == [T1, T2]

    async def test_an_unchanged_chunk_keeps_its_stored_time(self, components, memory_dir) -> None:
        """The update path never writes ``created_at``: change only the header's
        time (the tagged header is stripped, so the body and the id are the same)
        and a forced re-index keeps the stored value."""
        fp = memory_dir / "notes.md"
        fp.write_text(_entry(T1, "entry"), "utf-8")
        engine = components.index_engine
        await engine.index_file(fp)
        (stored,) = await components.storage.list_chunks_by_source(fp)
        fp.write_text(_entry(T2, "entry"), "utf-8")

        await engine.index_file(fp, force=True)

        (after,) = await components.storage.list_chunks_by_source(fp)
        assert after.id == stored.id
        assert after.created_at.astimezone(timezone.utc) == T1

    async def test_an_edited_body_keeps_the_entry_time(self, components, memory_dir) -> None:
        """The behaviour change ADR-0043 §5 names: an edit gives the chunk a
        new id, and it used to take the edit time."""
        fp = memory_dir / "notes.md"
        fp.write_text(_entry(T1, "first draft"), "utf-8")
        engine = components.index_engine
        await engine.index_file(fp)
        fp.write_text(_entry(T1, "second draft, rewritten"), "utf-8")

        await engine.index_file(fp)

        assert await _stored_created(components, fp) == [T1]

    async def test_a_full_replacement_edit_drops_the_header_and_takes_the_edit_time(
        self, components, memory_dir
    ) -> None:
        """New content that opens with its own ``## `` heading replaces the
        header by contract (``test_mem_edit_header_preservation.py``), so the
        edited entry has no ``> created:`` line left to be dated by."""
        from memtomem.source_provenance import source_span_hash
        from memtomem.tools.memory_writer import IndexedBase, replace_chunk_body

        fp = memory_dir / "notes.md"
        fp.write_text(_entry(T1, "old body"), "utf-8")
        engine = components.index_engine
        await engine.index_file(fp)
        lines = fp.read_text(encoding="utf-8").splitlines()
        replace_chunk_body(
            fp,
            start_line=1,
            end_line=len(lines),
            new_content="## New\n\nreplaced body",
            base=IndexedBase("old body"),
            expected_source_span_hash=source_span_hash(lines, 1, len(lines)),
        )
        assert "> created:" not in fp.read_text(encoding="utf-8")
        before = datetime.now(timezone.utc)

        await engine.index_file(fp)

        (stored,) = await _stored_created(components, fp)
        assert stored >= before - timedelta(seconds=1)

    async def test_an_entry_without_a_header_is_dated_by_the_index_run(
        self, components, memory_dir
    ) -> None:
        fp = memory_dir / "notes.md"
        fp.write_text("## Hand-written\n\nno header here\n", "utf-8")
        before = datetime.now(timezone.utc)

        await components.index_engine.index_file(fp)

        (stored,) = await _stored_created(components, fp)
        assert stored >= before - timedelta(seconds=1)


class TestMemAddEntriesKeepTheirWriteTime:
    async def test_mem_add_then_rebuild_keeps_the_written_time(self, bm25_only_components) -> None:
        from helpers import StubCtx
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        out = await mem_add(content="handoff body", ctx=StubCtx(AppContext.from_components(comp)))  # type: ignore[arg-type]
        assert not out.startswith("Error:"), out
        (written,) = list(mem_dir.glob("*.md"))
        line = next(ln for ln in written.read_text().splitlines() if ln.startswith("> created:"))
        written_at = datetime.fromisoformat(line[len("> created:") :].strip())
        await comp.storage.delete_by_source(written)

        await comp.index_engine.index_file(written, force=True)

        chunks = await comp.storage.list_chunks_by_source(written)
        assert [c.created_at.astimezone(timezone.utc) for c in chunks] == [written_at]


@pytest.mark.parametrize("field", ["entry_created_at"])
def test_the_field_is_not_a_stored_column(field: str) -> None:
    """Guards the claim in the model comment: the store does not persist it,
    so a row read back carries ``None`` and the differ never compares it."""
    from memtomem.indexing import differ

    assert field not in Path(differ.__file__).read_text(encoding="utf-8")
