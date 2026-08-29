"""Stored timestamps must be canonical UTC before they meet a lexical bound.

The ROW side of the invariant ``test_temporal_bound_normalization`` covers:
``created_at`` / ``updated_at`` are compared lexically, so a row written with
its caller's offset intact (an imported bundle's ``+09:00``), with no offset
(a naive value), or in SQLite's ``CURRENT_TIMESTAMP`` shape sorts by its
printed digits and lands on the wrong side of every correct bound.

These go through real storage: the stored string is asserted directly, and the
range tests demonstrate the disagreement through actual queries.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import make_chunk
from memtomem.storage.sqlite_helpers import utc_stamp

_asyncio = pytest.mark.asyncio

#: ``2026-01-01T00:00:00+09:00`` is ``2025-12-31T15:00Z``: the printed digits
#: land on the *following* date, so a raw rendering sorts after bounds that
#: the instant it names precedes.
_KST = timezone(timedelta(hours=9))
_ROW_WALL_CLOCK = datetime(2026, 1, 1, 0, 0, tzinfo=_KST)
_ROW_UTC = "2025-12-31T15:00:00.000000+00:00"


def _stored_timestamps(storage, chunk_id) -> tuple[str, str]:
    row = (
        storage._get_db()
        .execute("SELECT created_at, updated_at FROM chunks WHERE id=?", (str(chunk_id),))
        .fetchone()
    )
    assert row is not None
    return row[0], row[1]


class TestUtcStamp:
    def test_an_offset_value_is_converted_not_relabelled(self):
        assert utc_stamp(_ROW_WALL_CLOCK) == _ROW_UTC

    def test_a_naive_value_is_read_as_utc(self):
        assert utc_stamp(datetime(2026, 1, 1)) == "2026-01-01T00:00:00.000000+00:00"

    def test_the_bound_rendering_still_matches_the_old_utc_bound_output(self):
        # ``utc_bound`` now delegates here with ``timespec="auto"``; a
        # whole-second value must keep printing without fractional digits.
        assert utc_stamp(_ROW_WALL_CLOCK, timespec="auto") == "2025-12-31T15:00:00+00:00"


@_asyncio
class TestUpsertRowNormalization:
    async def test_an_offset_created_at_is_stored_as_utc(self, storage):
        chunk = dataclasses.replace(
            make_chunk(content="offset row"),
            created_at=_ROW_WALL_CLOCK,
            updated_at=_ROW_WALL_CLOCK,
        )
        await storage.upsert_chunks([chunk])

        created, updated = _stored_timestamps(storage, chunk.id)
        assert created == _ROW_UTC
        assert updated == _ROW_UTC

    async def test_a_naive_created_at_is_stored_with_an_offset(self, storage):
        chunk = dataclasses.replace(
            make_chunk(content="naive row"),
            created_at=datetime(2026, 1, 1),
        )
        await storage.upsert_chunks([chunk])

        created, _ = _stored_timestamps(storage, chunk.id)
        assert created == "2026-01-01T00:00:00.000000+00:00"

    async def test_the_update_branch_normalizes_updated_at(self, storage):
        chunk = make_chunk(content="update branch")
        await storage.upsert_chunks([chunk])

        touched = dataclasses.replace(chunk, content="edited", updated_at=_ROW_WALL_CLOCK)
        await storage.upsert_chunks([touched])

        _, updated = _stored_timestamps(storage, chunk.id)
        assert updated == _ROW_UTC

    async def test_an_offset_row_stays_inside_the_range_it_belongs_to(self, storage):
        """The end-to-end disagreement: stored raw, the row's ``+09:00`` digits
        sort after a UTC ``until`` bound that its instant precedes, and it
        falls out of the range."""
        chunk = dataclasses.replace(make_chunk(content="ranged row"), created_at=_ROW_WALL_CLOCK)
        await storage.upsert_chunks([chunk])

        an_hour_after = datetime(2025, 12, 31, 16, 0, tzinfo=timezone.utc)
        found = await storage.recall_chunks(until=an_hour_after, limit=10)
        assert [c.content for c in found] == ["ranged row"]

        an_hour_before = datetime(2025, 12, 31, 14, 0, tzinfo=timezone.utc)
        found = await storage.recall_chunks(since=an_hour_before, limit=10)
        assert [c.content for c in found] == ["ranged row"]
        found = await storage.recall_chunks(until=an_hour_before, limit=10)
        assert found == []


@_asyncio
class TestScopeMoveTimestamp:
    async def test_a_scope_move_writes_a_canonical_utc_updated_at(self, storage):
        """``update_chunks_scope_for_source`` used ``CURRENT_TIMESTAMP``, which
        SQLite renders space-separated and offset-less — a shape that sorts
        before every canonical ISO row and skewed ``MAX(updated_at)``."""
        chunk = make_chunk(content="moving row", source="move-me.md")
        await storage.upsert_chunks([chunk])

        moved = await storage.update_chunks_scope_for_source(
            Path("/tmp/move-me.md"), Path("/tmp/moved.md"), "global", None
        )
        assert moved == 1

        _, updated = _stored_timestamps(storage, chunk.id)
        parsed = datetime.fromisoformat(updated)
        assert updated == utc_stamp(parsed)
        assert parsed.tzinfo is not None


@_asyncio
class TestTagMutationTimestamp:
    async def test_a_tag_rename_writes_the_same_precision_as_the_indexer(self, storage):
        """The tag writers stamped their own UTC ``now`` at second precision
        while ``upsert_chunks`` writes microseconds. Both are UTC, but the
        comparison is lexical and ``'+' < '.'``, so a renamed row sorted
        before every indexed row inside its own second."""
        chunk = make_chunk(content="tagged row", tags=("before",))
        await storage.upsert_chunks([chunk])

        renamed = await storage.rename_tag("before", "after")
        assert renamed == 1

        _, updated = _stored_timestamps(storage, chunk.id)
        assert updated == utc_stamp(datetime.fromisoformat(updated))
