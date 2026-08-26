"""Storage-side coverage for temporal queries.

Pure-function coverage of `build_timeline`, `format_timeline`, and
`format_activity` lives in `test_tools_logic.py::TestTemporal`. This file
covers the storage method `Storage.get_activity_summary`, which needs a
real DB fixture and so does not fit alongside the pure-function tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata


def _make_chunk(
    content="test",
    tags=(),
    namespace="default",
    source="test.md",
    created_at=None,
):
    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 1024,
    )
    if created_at is not None:
        chunk.created_at = created_at
    return chunk


class TestActivitySummaryStorage:
    @pytest.mark.asyncio
    async def test_counts(self, storage):
        # Pin created_at to a fixed instant so the test cannot straddle UTC
        # midnight between Chunk construction and the strftime() below.
        fixed_dt = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
        c1 = _make_chunk("chunk1", source="a.md", created_at=fixed_dt)
        c2 = _make_chunk("chunk2", source="b.md", created_at=fixed_dt)
        await storage.upsert_chunks([c1, c2])

        summary = await storage.get_activity_summary()
        target_day = fixed_dt.strftime("%Y-%m-%d")
        day_data = [d for d in summary if d["date"] == target_day]
        assert len(day_data) == 1
        assert day_data[0]["created"] == 2

    @pytest.mark.asyncio
    async def test_empty_range(self, storage):
        summary = await storage.get_activity_summary(since="2020-01-01", until="2020-01-02")
        assert len(summary) == 0


class TestActivitySummaryContract:
    """The storage side of the boundary, in real SQL.

    ``get_activity_summary`` compares ``DATE(created_at)`` with ``BETWEEN``-
    style inclusive bounds, so ``until`` names the last day counted. The tool
    tests below pin what ``mem_activity`` sends; these pin what that value
    means once it reaches SQL.
    """

    @pytest.mark.asyncio
    async def test_until_names_the_last_day_counted(self, storage):
        april_30 = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
        may_1 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        await storage.upsert_chunks(
            [
                _make_chunk("april", source="apr.md", created_at=april_30),
                _make_chunk("may", source="may.md", created_at=may_1),
            ]
        )

        summary = await storage.get_activity_summary(since="2026-04-01", until="2026-04-30")

        assert [d["date"] for d in summary] == ["2026-04-30"]


class TestActivityToolBound:
    """``mem_activity`` translates an exclusive instant into the inclusive day
    label the storage contract above expects.

    ``_parse_recall_date(until, end_of_period=True)`` returns the start of the
    *next* period, so passing it straight through counted one day too many:
    ``until="2026-04"`` reached 2026-05-01.
    """

    @staticmethod
    def _spy_app(monkeypatch, captured: dict):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem.server.tools import temporal as temporal_mod

        app = MagicMock()

        async def fake_summary(since=None, until=None, namespace=None):
            captured["since"] = since
            captured["until"] = until
            return []

        app.storage.get_activity_summary = fake_summary
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))
        return temporal_mod

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("until", "expected"),
        [
            ("2026-04", "2026-04-30"),  # month → its last day, not the 1st of May
            ("2026", "2026-12-31"),  # year → its last day
            ("2026-04-06", "2026-04-06"),  # day → itself
        ],
    )
    async def test_until_reaches_storage_as_the_last_day_it_admits(
        self, monkeypatch, until: str, expected: str
    ):
        captured: dict = {}
        temporal_mod = self._spy_app(monkeypatch, captured)

        await temporal_mod.mem_activity(since="2026-01-01", until=until, ctx=None)

        assert captured["until"] == expected

    @pytest.mark.asyncio
    async def test_the_minimum_date_does_not_underflow(self, monkeypatch):
        """The step back is a subtraction, so the earliest representable bound
        is where it would raise ``OverflowError`` — outside the ``ValueError``
        handler. Guarding intraday values keeps every arrival a whole period,
        which always advances first.

        The same bound pins the rendering: ``strftime("%Y")`` does not zero-pad
        on glibc, so this arrived as ``1-01-01`` on Linux while passing on
        macOS. SQL compares these lexically and ``'1-01-01' >= '0001-06-01'``,
        so an unpadded bound selects the wrong rows rather than failing loudly.
        """
        captured: dict = {}
        temporal_mod = self._spy_app(monkeypatch, captured)

        out = await temporal_mod.mem_activity(since="0001-01-01", until="0001-01-01", ctx=None)

        assert not out.startswith("Error: ")
        assert captured["until"] == "0001-01-01"

    @pytest.mark.asyncio
    async def test_the_rendered_range_names_the_same_day(self, monkeypatch):
        """The label and the query have to agree — the old code showed
        ``2026-05-01`` for a April range because it reused the exclusive bound
        for both."""
        captured: dict = {}
        temporal_mod = self._spy_app(monkeypatch, captured)

        out = await temporal_mod.mem_activity(since="2026-04", until="2026-04", ctx=None)

        assert "2026-05-01" not in out
        assert "2026-04-30" in out


class TestTimelineBound:
    @pytest.mark.asyncio
    async def test_a_chunk_on_the_bound_belongs_to_the_next_period(self, monkeypatch):
        """``until_dt`` is the start of the period *after* the requested one,
        so the filter has to be ``>=``. With ``>`` a chunk created exactly at
        2026-05-01T00:00 was reported inside ``until="2026-04"``."""
        from unittest.mock import AsyncMock, MagicMock

        from memtomem.models import SearchResult
        from memtomem.server.tools import temporal as temporal_mod

        on_bound = _make_chunk(
            "may first",
            source="may.md",
            created_at=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        )
        inside = _make_chunk(
            "april",
            source="apr.md",
            created_at=datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc),
        )

        app = MagicMock()
        app.search_pipeline.search = AsyncMock(
            return_value=(
                [
                    SearchResult(chunk=c, score=1.0, rank=i + 1, source="bm25")
                    for i, c in enumerate((inside, on_bound))
                ],
                MagicMock(),
            )
        )
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await temporal_mod.mem_timeline(
            topic="anything", since="2026-04", until="2026-04", ctx=None
        )

        assert "april" in out
        assert "may first" not in out


class TestActivityRejectsIntradayBounds:
    """The summary groups by ``DATE(created_at)``, so it has no way to honour a
    time of day. Rounding one up to the whole day counts hours the caller
    excluded — and silently, which is why this refuses instead."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["since", "until"])
    @pytest.mark.parametrize(
        "value",
        ["2026-04-06T14:30:00+00:00", "2026-04-06 14:30:00+00:00"],
    )
    async def test_a_time_of_day_is_refused(self, monkeypatch, field: str, value: str):
        called = False

        from unittest.mock import AsyncMock, MagicMock

        from memtomem.server.tools import temporal as temporal_mod

        app = MagicMock()

        async def fake_summary(since=None, until=None, namespace=None):
            nonlocal called
            called = True
            return []

        app.storage.get_activity_summary = fake_summary
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        kwargs = {"since": "2026-01-01", "until": "2026-12-31"}
        kwargs[field] = value
        out = await temporal_mod.mem_activity(**kwargs, ctx=None)

        assert out.startswith("Error: ")
        assert field in out
        assert called is False, "the refusal must happen before the query"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "frozen",
        [
            datetime(2026, 4, 6, 0, 0, 0, tzinfo=timezone.utc),  # exactly midnight
            datetime(2026, 4, 6, 13, 45, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 6, 23, 59, 59, 999999, tzinfo=timezone.utc),
        ],
        ids=["midnight", "midday", "last-microsecond"],
    )
    async def test_the_default_range_reaches_through_today(self, monkeypatch, frozen: datetime):
        """The implicit ``now`` is an instant, but it is the documented default
        and means "through today" — so it is neither refused nor stepped back.

        Midnight is the case that matters: ``now`` is already inside the day it
        names, and subtracting from it there lands on yesterday, dropping the
        whole of today. The clock is frozen rather than read twice — comparing
        against a separately-computed ``now()`` both flakes across UTC midnight
        and cannot reach this boundary on purpose.
        """
        from unittest.mock import AsyncMock, MagicMock

        from memtomem.server.tools import temporal as temporal_mod

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        monkeypatch.setattr(temporal_mod, "datetime", _FrozenDatetime)

        app = MagicMock()
        captured: dict = {}

        async def fake_summary(since=None, until=None, namespace=None):
            captured["until"] = until
            return []

        app.storage.get_activity_summary = fake_summary
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await temporal_mod.mem_activity(ctx=None)

        assert not out.startswith("Error: ")
        assert captured["until"] == "2026-04-06"
