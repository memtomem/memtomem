"""``mem_add`` reports how many chunks the written entry became (#3400).

``- Chunks indexed`` is the whole file's upsert count: it counts an earlier
section re-chunked in the same pass, misses an entry chunk that kept its id by
content hash, and so cannot say whether the entry stayed in one chunk. The
``- Entry chunks`` line reads ``IndexingStats.appended_chunk_ids`` and
``appended_attribution`` (#3292) instead.

The tests go through the real ``mem_add`` on a BM25-only store and compare the
reported count with the rows the store holds for the entry.
"""

from __future__ import annotations

import re

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext

# Over the default ``min_chunk_tokens`` on its own, under ``max_chunk_tokens``,
# so a section of it is one chunk that does not merge with its neighbours.
LONG = " ".join(f"word{i}" for i in range(260))

ENTRY_LINE = re.compile(r"^- Entry chunks: (.*)$", re.MULTILINE)


def _entry_line(result: str) -> str:
    """The value of the one ``- Entry chunks:`` line; fails unless there is exactly one."""
    values = ENTRY_LINE.findall(result)
    assert len(values) == 1, result
    return values[0]


def _indexed(result: str) -> int:
    (value,) = re.findall(r"^- Chunks indexed: (\d+)$", result, re.MULTILINE)
    return int(value)


async def _add(ctx, **kwargs) -> str:
    from memtomem.server.tools.memory_crud import mem_add

    result = await mem_add(ctx=ctx, **kwargs)  # type: ignore[arg-type]
    assert result.startswith("Memory added to "), result
    return result


async def _rows_holding(app, path, token: str) -> list:
    return [c for c in await app.storage.list_chunks_by_source(path) if token in c.content]


def _append_raw(path, text: str) -> None:
    """Another writer's bytes, not indexed: a pending edit."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture
def store(bm25_only_components):
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    return app, StubCtx(app), mem_dir


class TestExact:
    @pytest.mark.asyncio
    async def test_a_pending_section_is_not_counted(self, store):
        # The pending section is re-chunked in the same pass, so the file-wide
        # count is 2 while the entry became one chunk.
        app, ctx, mem_dir = store
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="One", file="a.md")
        _append_raw(path, "\n## Pending\n\nzqpending " + LONG + "\n")

        result = await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        assert _indexed(result) == 2
        assert _entry_line(result) == "1"
        assert len(await _rows_holding(app, path, "zqtwo")) == 1

    @pytest.mark.asyncio
    async def test_an_entry_that_keeps_an_existing_id_is_counted(self, store):
        # The removal makes the re-appended entry match the stored row by
        # content hash: nothing is upserted, yet the entry is one chunk.
        app, ctx, mem_dir = store
        path = mem_dir / "a.md"
        path.write_text("## Before\n\nzqbefore " + LONG + "\n", encoding="utf-8")
        await app.index_engine.index_file(path, ingress_guarded=True)
        await _add(ctx, content="zqsame " + LONG, title="Same", tags=["t"], file="a.md")
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: text.index("## Same")], encoding="utf-8")

        result = await _add(ctx, content="zqsame " + LONG, title="Same", tags=["t"], file="a.md")

        assert _indexed(result) == 0
        assert _entry_line(result) == "1"
        assert len(await _rows_holding(app, path, "zqsame")) == 1


@pytest.fixture
async def bounded_store(tmp_path, monkeypatch):
    """A BM25-only store whose chunks are bounded by a one-token-per-byte tokenizer."""
    tokenizers = pytest.importorskip("tokenizers")
    from helpers import isolate_memtomem_env
    from memtomem.config import Mem2MemConfig
    from memtomem.server.component_factory import close_components, create_components

    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={char: index for index, char in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(exist_ok=True)
    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "bounded.db"
    config.embedding.dimension = 1024
    config.search.enable_dense = False
    config.indexing = config.indexing.model_copy(
        update={
            "memory_dirs": [mem_dir],
            "hard_max_chunk_tokens": 256,
            "chunk_tokenizer_path": str(tokenizer_path),
            "chunk_context_tokens": 256,
            "chunk_model_tokens": 1024,
        }
    )
    comp = await create_components(config)
    try:
        app = AppContext.from_components(comp)
        yield app, StubCtx(app), mem_dir
    finally:
        await close_components(comp)


class TestSplit:
    @pytest.mark.asyncio
    async def test_an_entry_over_the_hard_budget_reports_its_split(self, bounded_store):
        # The issue's case: an item over the exact token ceiling is split, and
        # the tail holding its citation becomes a chunk of its own.
        app, ctx, mem_dir = bounded_store
        path = mem_dir / "a.md"
        body = "zqbody " + "x" * 300 + "\n\nRelated: zqcite"

        result = await _add(ctx, content=body, title="Item", file="a.md")

        rows = await app.storage.list_chunks_by_source(path)
        assert len(rows) >= 2, "the fixture must split the entry"
        assert _entry_line(result) == str(len(rows))
        assert len({c.id for c in rows}) == len(rows)
        joined = "".join(c.content for c in sorted(rows, key=lambda c: c.metadata.start_line))
        assert "zqbody" in joined and "zqcite" in joined

    @pytest.mark.asyncio
    async def test_a_merged_entry_over_the_hard_budget_reports_every_chunk(self, bounded_store):
        # Merged and split: the entry's header lands in the chunk of the section
        # before it, its body splits under the budget, and the count covers all
        # of them. A body that stays whole still counts 2 here: the header's
        # chunk is one of the entry's, which the qualifier says.
        app, ctx, mem_dir = bounded_store
        path = mem_dir / "a.md"
        path.write_text("## Same\n\nzqpending short\n", encoding="utf-8")
        body = "zqbody " + "x" * 300 + "\n\nRelated: zqcite"

        result = await _add(ctx, content=body, title="Same", file="a.md")

        rows = await app.storage.list_chunks_by_source(path)
        assert len(rows) >= 3, "the fixture must split the entry's body"
        assert any("zqpending" in c.content and "> created:" in c.content for c in rows), (
            "the fixture must merge the entry's header into the section before it"
        )
        qualifier = "(shares its first chunk with text before the entry)"
        assert _entry_line(result) == f"{len(rows)} {qualifier}"


class TestNotExact:
    @pytest.mark.asyncio
    async def test_a_merge_into_a_pending_section_says_so(self, store):
        # The entry continues an unindexed section under the same heading, in
        # one chunk that also holds the other writer's text.
        app, ctx, mem_dir = store
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="Other", file="a.md")
        _append_raw(path, "\n## Same\n\nzqpending short\n")

        result = await _add(ctx, content="zqtwo short", title="Same", file="a.md")

        assert _entry_line(result) == "1 (shares its first chunk with text before the entry)"
        (row,) = await _rows_holding(app, path, "zqtwo")
        assert "zqpending" in row.content

    @pytest.mark.asyncio
    async def test_overlap_leaves_the_count_unknown(self, store):
        # Overlap copies text between neighbours without moving their spans,
        # so the spans cannot say which chunks hold the entry.
        app, ctx, _ = store
        app.index_engine._config.chunk_overlap_tokens = 32
        await _add(ctx, content="zqone " + LONG, title="One", file="a.md")

        result = await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        assert _entry_line(result) == "unknown"

    @pytest.mark.asyncio
    async def test_a_non_markdown_file_leaves_the_count_unknown(self, store):
        # Only Markdown spans are judged: other chunkers decode virtual lines.
        _, ctx, _ = store

        result = await _add(ctx, content="zqone " + LONG, title="One", file="a.json")

        assert _indexed(result) == 1
        assert _entry_line(result) == "unknown"


class TestNotIndexed:
    @pytest.mark.asyncio
    async def test_a_file_type_with_no_chunker(self, store):
        app, ctx, mem_dir = store

        result = await _add(ctx, content="zqone " + LONG, title="One", file="a.txt")

        assert "zqone" in (mem_dir / "a.txt").read_text(encoding="utf-8")
        assert await _rows_holding(app, mem_dir / "a.txt", "zqone") == []
        assert _entry_line(result) == "unknown (this write did not index the file)"

    @pytest.mark.asyncio
    async def test_an_exclusion_installed_during_the_append(self, store, monkeypatch):
        from memtomem.tools import memory_writer

        app, ctx, mem_dir = store
        path = mem_dir / "a.md"
        await _add(ctx, content="zqone " + LONG, title="One", file="a.md")
        real = memory_writer.append_entry

        def append_then_exclude(*args, **kwargs):
            app.config.indexing.exclude_patterns = ["**/a.md"]
            return real(*args, **kwargs)

        monkeypatch.setattr(memory_writer, "append_entry", append_then_exclude)

        result = await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        assert "zqtwo" in path.read_text(encoding="utf-8")
        assert await _rows_holding(app, path, "zqtwo") == []
        assert _entry_line(result) == "unknown (this write did not index the file)"

    @pytest.mark.asyncio
    async def test_an_embedding_failure(self, store, monkeypatch):
        app, ctx, mem_dir = store
        path = mem_dir / "a.md"

        async def fail(*args, **kwargs):
            raise RuntimeError("embedder down")

        # The BM25-only embedder has no dimension, so the engine never calls
        # it; give it the store's, so the embed is attempted and fails.
        embedder = app.index_engine._embedder
        monkeypatch.setattr(embedder, "embed_texts", fail)
        monkeypatch.setattr(type(embedder), "dimension", property(lambda _self: 1024))

        result = await _add(ctx, content="zqtwo " + LONG, title="Two", file="a.md")

        assert "zqtwo" in path.read_text(encoding="utf-8")
        assert _indexed(result) == 0
        assert await _rows_holding(app, path, "zqtwo") == []
        assert _entry_line(result) == "unknown (this write did not index the file)"


class TestReplayAndBatch:
    @pytest.mark.asyncio
    async def test_a_replay_reports_the_original_count(self, store):
        # Part of the stored base result, not of the original-only tail.
        from memtomem.server.tools.memory_crud import _REPLAY_MARKER

        _, ctx, _ = store
        kwargs = {"content": "zqone " + LONG, "title": "One", "file": "a.md"}
        first = await _add(ctx, idempotency_key="k1", **kwargs)
        replay = await _add(ctx, idempotency_key="k1", **kwargs)

        assert replay.endswith(_REPLAY_MARKER), replay
        assert _entry_line(first) == "1"
        assert _entry_line(replay) == "1"

    @pytest.mark.asyncio
    async def test_a_batch_has_no_entry_line(self, store):
        # One append region covers the whole batch, so there is no per-entry count.
        from memtomem.server.tools.memory_crud import mem_batch_add

        _, ctx, _ = store
        result = await mem_batch_add(
            entries=[{"key": "a", "value": "zqa " + LONG}, {"key": "b", "value": "zqb " + LONG}],
            ctx=ctx,
        )  # type: ignore[arg-type]

        assert result.startswith("Batch add complete"), result
        assert ENTRY_LINE.findall(result) == []
