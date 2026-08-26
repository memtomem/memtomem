"""Temporal bounds must be UTC before they reach a lexical SQL comparison.

``created_at`` is a UTC ISO-8601 string and SQLite has no datetime type, so
``created_at >= ?`` orders by printed digits. A bound left in its original
zone therefore sorts by its own wall-clock reading rather than by the instant
it names, and rows fall out of ranges they belong to.

These go through real SQL rather than asserting on the rendered parameter:
the whole failure is that a string comparison disagrees with a temporal one,
which only a query can demonstrate.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from helpers import make_chunk
from memtomem.storage.base import SearchMetadataFilter
from memtomem.storage.sqlite_helpers import utc_bound, utc_bound_from_iso

#: Applied per async class rather than module-wide: ``TestUtcBound`` is
#: synchronous and pytest-asyncio warns when the mark lands on a plain
#: function.
_asyncio = pytest.mark.asyncio

#: The row under test, and a bound naming an instant one hour before it while
#: *printing* as a later date. ``2026-01-01T00:00:00+09:00`` is
#: ``2025-12-31T15:00Z``; the row is an hour after that, so every range
#: starting at the bound contains it.
_ROW_AT = datetime(2025, 12, 31, 16, 0, tzinfo=timezone.utc)
_BOUND_BEFORE_ROW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=9)))


async def _store_row_at(storage, when: datetime):
    chunk = dataclasses.replace(make_chunk(content="bounded row"), created_at=when)
    await storage.upsert_chunks([chunk])
    return chunk


class TestUtcBound:
    def test_an_offset_bound_is_converted_not_relabelled(self):
        assert utc_bound(_BOUND_BEFORE_ROW) == "2025-12-31T15:00:00+00:00"

    def test_a_naive_bound_is_read_as_utc(self):
        assert utc_bound(datetime(2026, 1, 1)) == "2026-01-01T00:00:00+00:00"

    def test_the_string_form_reports_which_argument_was_bad(self):
        with pytest.raises(ValueError, match="since must be an ISO-8601 timestamp"):
            utc_bound_from_iso("not-a-timestamp", field="since")


@_asyncio
class TestRecallBounds:
    async def test_an_offset_since_still_finds_a_row_that_follows_it(self, storage):
        """Compared as raw strings, ``2026-01-01T00:00:00+09:00`` sorts after
        the row's ``2025-12-31T16:00Z`` and the row disappears — even though
        the bound names an earlier instant."""
        await _store_row_at(storage, _ROW_AT)

        found = await storage.recall_chunks(since=_BOUND_BEFORE_ROW, limit=10)

        assert [c.content for c in found] == ["bounded row"]

    async def test_an_offset_until_still_excludes_a_row_that_follows_it(self, storage):
        """The mirror case: the row is after the bound, so an exclusive
        ``until`` must leave it out."""
        await _store_row_at(storage, _ROW_AT)

        found = await storage.recall_chunks(until=_BOUND_BEFORE_ROW, limit=10)

        assert found == []


@_asyncio
class TestMetadataFilterBounds:
    async def test_an_offset_created_from_still_finds_a_row_that_follows_it(self, storage):
        """Same bound, the other filter path. Only the web route normalized
        these, so every other caller — MCP, CLI, in-process — got the raw
        value."""
        await _store_row_at(storage, _ROW_AT)

        found = await storage.recall_chunks(
            limit=10,
            metadata_filter=SearchMetadataFilter(created_from=_BOUND_BEFORE_ROW),
        )

        assert [c.content for c in found] == ["bounded row"]

    async def test_an_offset_created_before_still_excludes_a_row_that_follows_it(self, storage):
        await _store_row_at(storage, _ROW_AT)

        found = await storage.recall_chunks(
            limit=10,
            metadata_filter=SearchMetadataFilter(created_before=_BOUND_BEFORE_ROW),
        )

        assert found == []


def _record_query_at(storage, text: str, when: datetime) -> None:
    """Insert a history row at a chosen instant.

    ``save_query_history`` stamps ``now()``, and these need a row whose
    printed digits sit on a known side of the bound. Written at the same
    ``timespec="seconds"`` precision the production insert uses, because that
    precision is half of what the second test is about.
    """
    storage._get_db().execute(
        "INSERT INTO query_history (query_text, query_embedding, result_chunk_ids, "
        "result_scores, created_at) VALUES (?, ?, ?, ?, ?)",
        (text, b"", "[]", "[]", when.isoformat(timespec="seconds")),
    )


@_asyncio
class TestQueryHistoryBounds:
    async def test_an_offset_since_still_finds_a_run_that_follows_it(self, storage):
        """``get_query_history`` bound its ``since`` argument to SQL exactly as
        the caller typed it — no parsing at all — while the sibling
        ``get_search_runs`` right below it normalized.

        The bound needs a *positive* offset so its printed digits land on the
        following date; a negative one prints earlier and the raw comparison
        happens to include the row, which proves nothing.
        """
        _record_query_at(storage, "bounded query", _ROW_AT)

        found = await storage.get_query_history(limit=5, since=_BOUND_BEFORE_ROW.isoformat())

        assert [r["query_text"] for r in found] == ["bounded query"]

    async def test_a_fractional_since_still_finds_a_run_in_the_same_second(self, storage):
        """``query_history.created_at`` is written at second precision, so a
        bound carrying fractional seconds sorts after every row inside its own
        second — a poll at ``…:00.500`` would miss a run recorded at
        ``…:00.800`` and stored as ``…:00``."""
        second = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        _record_query_at(storage, "same second", second)

        found = await storage.get_query_history(
            limit=5, since=second.replace(microsecond=500_000).isoformat()
        )

        assert [r["query_text"] for r in found] == ["same second"]

    async def test_a_malformed_since_is_refused_by_name(self, storage):
        with pytest.raises(ValueError, match="since must be an ISO-8601 timestamp"):
            await storage.get_query_history(limit=5, since="yesterday")


@_asyncio
class TestSearchRunBounds:
    """``get_search_runs`` normalized correctly before this change; folding its
    inline logic into the shared helper must not regress either axis, and it
    floored to seconds — which the helper now has to be told to keep."""

    async def _record_run(self, storage, when: datetime, run_id: str) -> None:
        storage._get_db().execute(
            "INSERT INTO query_history (query_text, query_embedding, result_chunk_ids, "
            "result_scores, created_at, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("run query", b"", "[]", "[]", when.isoformat(timespec="seconds"), run_id),
        )

    async def test_an_offset_since_still_finds_a_run_that_follows_it(self, storage):
        await self._record_run(storage, _ROW_AT, "run-offset")

        found = await storage.get_search_runs(limit=5, since=_BOUND_BEFORE_ROW.isoformat())

        assert [r["run_id"] for r in found] == ["run-offset"]

    async def test_a_fractional_since_still_finds_a_run_in_the_same_second(self, storage):
        second = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        await self._record_run(storage, second, "run-fractional")

        found = await storage.get_search_runs(
            limit=5, since=second.replace(microsecond=500_000).isoformat()
        )

        assert [r["run_id"] for r in found] == ["run-fractional"]
