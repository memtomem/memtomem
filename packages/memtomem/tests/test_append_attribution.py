"""The indexer names the chunks an append became (#3292).

``IndexingStats.new_chunk_ids`` covers the whole file's re-index: it holds a
pending edit's chunk, and misses the append's own chunk when that chunk keeps an
existing id. ``index_file(append_region=...)`` answers from the text it chunked,
and only when the file is the text the appending caller expected.

The engine tests run on a real BM25-only store and assert ids against the rows
it wrote.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.indexing import append_attribution
from memtomem.indexing.append_attribution import (
    AppendRegion,
    append_region,
    attribute_append,
)
from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.memory_writer import append_blocks, append_entry, read_pre_image_or_empty


async def _append(engine, path: Path, content: str, *, between=None, **entry):
    """Append like the memory tools do, then index with the append's region."""
    pre = read_pre_image_or_empty(path)
    appended = append_entry(path, content, **entry)
    if between is not None:
        between(path)
    return await engine.index_file(
        path, ingress_guarded=True, append_region=append_region(pre, appended)
    )


async def _rows(comp, path: Path) -> list[Chunk]:
    return await comp.storage.list_chunks_by_source(path)


async def _id_holding(comp, path: Path, token: str):
    (chunk_id,) = [c.id for c in await _rows(comp, path) if token in c.content]
    return chunk_id


@pytest.fixture
async def stack(bm25_only_components):
    comp, mem_dir = bm25_only_components
    return comp, comp.index_engine, mem_dir / "notes.md"


# ---------------------------------------------------------------------------
# Attributed
# ---------------------------------------------------------------------------


class TestAttributed:
    @pytest.mark.asyncio
    async def test_a_standalone_entry(self, stack):
        comp, engine, path = stack
        stats = await _append(engine, path, "zqalone entry body.", title="Alone")

        assert stats.appended_attribution == "exact"
        assert stats.appended_chunk_ids == (await _id_holding(comp, path, "zqalone"),)

    @pytest.mark.asyncio
    async def test_a_pending_edit_is_not_the_append(self, stack):
        comp, engine, path = stack
        path.write_text("## Seed\n\nzqseed body indexed already.\n", encoding="utf-8")
        await engine.index_file(path, ingress_guarded=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n## Pending\n\nzqpending body nobody indexed.\n")

        engine._config.min_chunk_tokens = 0
        stats = await _append(engine, path, "zqmine entry body.", title="Mine")

        mine = await _id_holding(comp, path, "zqmine")
        pending = await _id_holding(comp, path, "zqpending")
        assert set(stats.new_chunk_ids) == {mine, pending}
        assert stats.appended_chunk_ids == (mine,)
        assert stats.appended_attribution == "exact"

    @pytest.mark.asyncio
    async def test_an_entry_split_into_several_chunks_in_file_order(self, stack):
        comp, engine, path = stack
        # Each paragraph alone is over the default ``max_chunk_tokens``.
        paragraphs = [f"zqpart{i} " + "filler words here " * 140 for i in range(4)]
        path.write_text("## Before\n\nzqbefore body.\n", encoding="utf-8")
        await engine.index_file(path, ingress_guarded=True)

        stats = await _append(engine, path, "\n\n".join(paragraphs), title="Long")

        rows = {c.id: c for c in await _rows(comp, path)}
        assert len(stats.appended_chunk_ids) > 1
        assert stats.appended_attribution == "exact"
        starts = [rows[i].metadata.start_line for i in stats.appended_chunk_ids]
        assert starts == sorted(starts)
        held = "".join(rows[i].content for i in stats.appended_chunk_ids)
        assert all(f"zqpart{i}" in held for i in range(4))
        assert await _id_holding(comp, path, "zqbefore") not in stats.appended_chunk_ids

    @pytest.mark.asyncio
    async def test_an_entry_that_keeps_an_existing_id(self, stack):
        # Content-hash reuse: the entry's chunk keeps the stored id, so it is
        # absent from ``new_chunk_ids``. Its tags differ from the stored row's,
        # which puts it in the diff's metadata-only bucket.
        comp, engine, path = stack
        engine._config.min_chunk_tokens = 0
        path.write_text("## Reflection\n\nrepeat\n\n## Other\n\nprefix\n", encoding="utf-8")
        await engine.index_file(path, ingress_guarded=True)
        (reused,) = [c.id for c in await _rows(comp, path) if c.content == "repeat"]
        path.write_text("## Other\n\nprefix repeat\n", encoding="utf-8")

        stats = await _append(engine, path, "repeat", title="Reflection", tags=["t"])

        assert reused not in stats.new_chunk_ids
        assert stats.appended_chunk_ids == (reused,)
        assert stats.appended_attribution == "exact"

    @pytest.mark.asyncio
    async def test_a_lone_cr_in_the_entry(self, stack):
        comp, engine, path = stack
        stats = await _append(engine, path, "zqcr first\rsecond line.", title="CR")

        assert stats.appended_attribution == "exact"
        assert stats.appended_chunk_ids == (await _id_holding(comp, path, "zqcr"),)

    @pytest.mark.asyncio
    async def test_a_pre_image_with_invalid_utf8(self, stack):
        comp, engine, path = stack
        path.write_bytes(b"## Bad\n\nzqbad \xff byte.\n")
        await engine.index_file(path, ingress_guarded=True)

        stats = await _append(engine, path, "zqafter bad body.", title="After")

        assert stats.appended_chunk_ids
        assert await _id_holding(comp, path, "zqafter") in stats.appended_chunk_ids


# ---------------------------------------------------------------------------
# Merged: the append's chunk also holds text from before it
# ---------------------------------------------------------------------------


class TestMerged:
    @pytest.mark.asyncio
    async def test_a_short_entry_merged_into_the_previous_section(self, stack):
        comp, engine, path = stack
        await _append(engine, path, "zqfirst short.", title="Same")
        stats = await _append(engine, path, "zqsecond short.", title="Same")

        merged = await _id_holding(comp, path, "zqsecond")
        assert merged == await _id_holding(comp, path, "zqfirst")
        assert stats.appended_chunk_ids == (merged,)
        assert stats.appended_attribution == "merged"

    @pytest.mark.asyncio
    async def test_a_heading_led_entry_whose_header_lands_in_the_previous_chunk(self, stack):
        # #2762: the header is written above the entry's own ``## `` heading.
        comp, engine, path = stack
        engine._config.min_chunk_tokens = 0
        path.write_text("## Earlier\n\nzqearlier body.\n", encoding="utf-8")
        await engine.index_file(path, ingress_guarded=True)

        stats = await _append(engine, path, "## Mine\n\nzqmine body.", tags=["t"])

        assert stats.appended_attribution == "merged"
        assert await _id_holding(comp, path, "zqmine") in stats.appended_chunk_ids

    @pytest.mark.asyncio
    async def test_an_append_that_starts_mid_line(self, stack):
        comp, engine, path = stack
        path.write_text("zqbefore text", encoding="utf-8")
        await engine.index_file(path, ingress_guarded=True)
        pre = read_pre_image_or_empty(path)
        appended = append_blocks(path, [" and zqafter text\n"])

        stats = await engine.index_file(
            path, ingress_guarded=True, append_region=append_region(pre, appended)
        )

        assert stats.appended_attribution == "merged"
        assert stats.appended_chunk_ids == (await _id_holding(comp, path, "zqafter"),)


# ---------------------------------------------------------------------------
# Unverified: no ids at all
# ---------------------------------------------------------------------------


class TestUnverified:
    @pytest.mark.asyncio
    async def test_an_identical_block_appended_after_ours(self, stack):
        # ``P + A + A``: a suffix match would name the other writer's copy.
        comp, engine, path = stack
        engine._config.min_chunk_tokens = 0
        pre = read_pre_image_or_empty(path)
        appended = append_entry(path, "zqtwin body.", title="Twin")
        append_blocks(path, [appended])

        stats = await engine.index_file(
            path, ingress_guarded=True, append_region=append_region(pre, appended)
        )

        assert len([c for c in await _rows(comp, path) if "zqtwin" in c.content]) == 2
        assert stats.appended_chunk_ids == ()
        assert stats.appended_attribution == "unverified"

    @pytest.mark.asyncio
    async def test_an_edit_elsewhere_after_the_append(self, stack):
        comp, engine, path = stack
        path.write_text("## Seed\n\nzqseed body.\n", encoding="utf-8")
        await engine.index_file(path, ingress_guarded=True)

        def edit_seed(p: Path) -> None:
            p.write_text(p.read_text(encoding="utf-8").replace("zqseed", "zqedited"))

        stats = await _append(engine, path, "zqmine body.", title="Mine", between=edit_seed)

        assert stats.appended_chunk_ids == ()
        assert stats.appended_attribution == "unverified"

    @pytest.mark.asyncio
    async def test_not_requested(self, stack):
        comp, engine, path = stack
        append_entry(path, "zqplain body.", title="Plain")
        stats = await engine.index_file(path, ingress_guarded=True)

        assert stats.new_chunk_ids
        assert stats.appended_chunk_ids == ()
        assert stats.appended_attribution == "unverified"


# ---------------------------------------------------------------------------
# The pure parts
# ---------------------------------------------------------------------------


def _chunk(start: int, end: int) -> Chunk:
    return Chunk(
        content=f"lines {start}-{end}",
        metadata=ChunkMetadata(source_file=Path("x.md"), start_line=start, end_line=end),
        content_hash=f"h-{uuid4().hex}",
    )


class TestAttributeAppend:
    TEXT = "## A\n\nbody a\n\n## B\n\nbody b\n"
    START = len("## A\n\nbody a\n")

    def _region(self) -> AppendRegion:
        return AppendRegion(expected_text=self.TEXT, start=self.START)

    def test_the_lines_of_the_region(self):
        a, b = _chunk(1, 3), _chunk(5, 7)
        assert attribute_append(self.TEXT, self._region(), [a, b], {a.id, b.id}) == (
            (b.id,),
            "exact",
        )

    def test_a_chunk_reaching_into_the_region_is_merged(self):
        a, b = _chunk(1, 5), _chunk(6, 7)
        assert attribute_append(self.TEXT, self._region(), [a, b], {a.id, b.id}) == (
            (a.id, b.id),
            "merged",
        )

    def test_other_text_than_expected(self):
        b = _chunk(5, 7)
        assert attribute_append(self.TEXT + "x\n", self._region(), [b], {b.id}) == (
            (),
            "unverified",
        )

    def test_a_touching_chunk_that_was_not_committed(self):
        a, b = _chunk(1, 3), _chunk(5, 7)
        assert attribute_append(self.TEXT, self._region(), [a, b], {a.id}) == ((), "unverified")

    def test_an_unplaced_chunk_anywhere_in_the_file(self):
        unplaced, b = _chunk(0, 0), _chunk(5, 7)
        assert attribute_append(self.TEXT, self._region(), [unplaced, b], {unplaced.id, b.id}) == (
            (),
            "unverified",
        )

    def test_no_chunk_touches_the_region(self):
        # Never a vacuous ``exact``.
        a = _chunk(1, 3)
        assert attribute_append(self.TEXT, self._region(), [a], {a.id}) == ((), "unverified")

    def test_a_blank_region(self):
        text = "## A\n\nbody\n\n\n"
        b = _chunk(1, 3)
        region = AppendRegion(expected_text=text, start=len("## A\n\nbody\n"))
        assert attribute_append(text, region, [b], {b.id}) == ((), "unverified")


class TestAppendRegion:
    def test_offset_is_the_pre_image_text_length(self):
        region = append_region("## A\n\nbody\n".encode(), "\n## B\n\nmore\n")
        assert region == AppendRegion("## A\n\nbody\n\n## B\n\nmore\n", len("## A\n\nbody\n"))

    def test_invalid_utf8_does_not_raise(self):
        region = append_region(b"bad \xff\n", "\nok\n")
        assert region is not None
        assert region.expected_text[region.start :] == "\nok\n"

    def test_a_pre_image_ending_in_a_lone_cr(self):
        region = append_region(b"abc\r", "\nx\n")
        assert region is not None
        assert region.expected_text == "abc\nx\n"
        assert region.expected_text[region.start :] == "x\n"

    def test_windows_text_mode_newlines(self, monkeypatch):
        # The write turns ``\n`` into ``\r\n`` and the read folds it back, so an
        # appended ``\r\n`` reads as two newlines, not one.
        monkeypatch.setattr(append_attribution.os, "linesep", "\r\n")
        region = append_region(b"", "a\r\nb\n")
        assert region is not None
        assert region.expected_text == "a\n\nb\n"
