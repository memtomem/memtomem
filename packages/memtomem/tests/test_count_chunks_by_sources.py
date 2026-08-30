"""``count_chunks_by_sources`` against a real SQLite backend (#2261).

The method exists because ``mm purge`` used to size its preview by listing the
chunks and taking ``len()``, which capped the count at 10,000 per file while
the delete it was previewing had no such cap — the preview of a destructive
operation announced less than it was about to remove.

Two things a mocked storage cannot pin: that the count is not capped, and that
the ``IN`` clause is split. ``mm purge`` matches against every source in the
store, so the parameter count is bounded by the store rather than by the code.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.storage import sqlite_backend
from memtomem.storage.sqlite_helpers import norm_path

from helpers import make_chunk

pytestmark = pytest.mark.asyncio


def _insert_rows(storage, source: Path, count: int, *, start_index: int = 0) -> None:
    """Seed ``count`` rows for one source without paying for embeddings.

    The method under test reads only ``chunks``; going through
    ``upsert_chunks`` would spend the test's budget on vectors it never looks
    at, which is what makes a 10,050-row case affordable at all.
    """
    db = storage._get_db()
    db.executemany(
        "INSERT INTO chunks (id, content, content_hash, source_file, start_line, end_line, "
        "created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        [
            (
                str(uuid4()),
                f"row {i}",
                f"h-{uuid4().hex[:12]}",
                norm_path(source),
                i * 10,
                i * 10 + 9,
            )
            for i in range(start_index, start_index + count)
        ],
    )
    db.commit()


class TestCounts:
    async def test_counts_each_file_and_omits_the_empty_ones(self, storage):
        a, b, empty = Path("/tmp/a.md"), Path("/tmp/b.md"), Path("/tmp/empty.md")
        _insert_rows(storage, a, 2)
        _insert_rows(storage, b, 5)

        counts = await storage.count_chunks_by_sources([a, b, empty, Path("/tmp/never.md")])

        # Sparse by contract: absent means zero, and ``sum`` is what the
        # caller wants anyway.
        assert counts == {a: 2, b: 5}
        assert sum(counts.values()) == 7

    async def test_empty_input_is_an_empty_result(self, storage):
        assert await storage.count_chunks_by_sources([]) == {}

    async def test_keys_are_the_caller_s_own_path_objects(self, storage):
        """The result is keyed back through the caller's paths, not the stored text."""
        source = Path("/tmp/keyed.md")
        _insert_rows(storage, source, 3)

        counts = await storage.count_chunks_by_sources([source])

        assert list(counts) == [source]

    async def test_two_spellings_of_one_path_collapse_to_the_last(self, storage):
        """``norm_path`` folds NFD into NFC, so both spellings name one file.

        The documented consequence of keying by the caller's own objects: the
        result cannot hold both, and the entry belongs to whichever spelling
        was passed last. Pinned because the alternative — silently reporting
        the same rows twice, once per spelling — would double a count that
        ``mm purge`` prints as "would delete N".
        """
        nfc = Path(unicodedata.normalize("NFC", "/tmp/한글.md"))
        nfd = Path(unicodedata.normalize("NFD", "/tmp/한글.md"))
        assert str(nfc) != str(nfd)  # the premise: distinct Python paths
        _insert_rows(storage, nfc, 4)

        counts = await storage.count_chunks_by_sources([nfc, nfd])

        assert counts == {nfd: 4}
        assert sum(counts.values()) == 4

    async def test_agrees_with_the_single_file_sibling(self, storage):
        a, b = Path("/tmp/one.md"), Path("/tmp/two.md")
        _insert_rows(storage, a, 4)
        _insert_rows(storage, b, 1)

        counts = await storage.count_chunks_by_sources([a, b])

        assert counts[a] == await storage.count_chunks_by_source(a)
        assert counts[b] == await storage.count_chunks_by_source(b)


class TestPastTheOldCap:
    async def test_a_file_larger_than_the_old_cap_is_counted_whole(self, storage):
        """#2261: 10,050 rows used to be announced as 10,000."""
        source = Path("/tmp/big.md")
        _insert_rows(storage, source, 10_050)

        counts = await storage.count_chunks_by_sources([source])

        assert counts[source] == 10_050

    async def test_the_preview_number_matches_what_apply_deletes(self, storage):
        """The whole point: what purge announces is what purge removes.

        Deliberately small. ``delete_by_source`` binds every row id in one
        statement, so driving this at a size that would exercise the old cap
        would fail on any SQLite carrying the historical 999-variable limit —
        the very limit ``_SQL_MAX_PARAMS`` exists because the project does not
        assume away. That ceiling is ``delete_by_source``'s own defect, tracked
        separately; this test pins the relationship, and
        ``test_a_file_larger_than_the_old_cap_is_counted_whole`` pins the size.
        """
        source = Path("/tmp/parity.md")
        _insert_rows(storage, source, 7)

        previewed = (await storage.count_chunks_by_sources([source]))[source]
        deleted = await storage.delete_by_source(source)

        assert previewed == deleted == 7


class TestBatching:
    async def test_the_in_clause_is_split_at_the_configured_size(self, storage, monkeypatch):
        """Asserted on the statements issued, not on a large input.

        A size-only test is false-green on any SQLite built with the modern
        32,766 host-parameter ceiling: an unbatched query of a few thousand
        paths succeeds there and passes with the batching removed. Counting
        the parameters per statement pins it on every build (same reasoning as
        ``test_get_chunks_batch_splits_into_statements_of_the_configured_size``).
        """
        sources = [Path(f"/tmp/batch-{i}.md") for i in range(7)]
        for source in sources:
            _insert_rows(storage, source, 1)

        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 3)
        real_db = storage._get_read_db()
        seen: list[int] = []

        class _Counting:
            def __getattr__(self, name):
                return getattr(real_db, name)

            def execute(self, sql, params=()):
                if "COUNT(*) FROM chunks WHERE source_file IN" in sql:
                    seen.append(len(params))
                return real_db.execute(sql, params)

        monkeypatch.setattr(storage, "_get_read_db", lambda: _Counting())

        counts = await storage.count_chunks_by_sources(sources)

        assert seen == [3, 3, 1]
        assert counts == {source: 1 for source in sources}


async def test_upserted_chunks_are_counted_too(storage):
    """The seeding shortcut above is a shortcut, not a different code path."""
    chunk = make_chunk(content="real write", source="counted.md")
    await storage.upsert_chunks([chunk])

    counts = await storage.count_chunks_by_sources([chunk.metadata.source_file])

    assert counts == {chunk.metadata.source_file: 1}
