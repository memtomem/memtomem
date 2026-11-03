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


class TestTimelineWindowSelectsAtRetrieval:
    """``since``/``until`` select inside the retrievers, before the ``top_k``
    cut (#3005). Filtering after the cut let out-of-window matches fill every
    slot, so an in-window memory ranked below them never reached the timeline.
    """

    LIMIT = 3
    # More distractors than ``limit + 1`` slots: with only one bound forwarded,
    # the other side's distractors still fill the whole retrieval.
    DISTRACTORS = LIMIT + 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("side", "distractor_at"),
        [
            ("older", datetime(2026, 3, 10, tzinfo=timezone.utc)),
            ("newer", datetime(2026, 5, 1, tzinfo=timezone.utc)),
        ],
    )
    async def test_an_in_window_memory_outranked_by_out_of_window_ones_is_kept(
        self, bm25_only_components, side: str, distractor_at: datetime
    ):
        from helpers import StubCtx

        from memtomem.server.context import AppContext
        from memtomem.server.tools.temporal import mem_timeline

        comp, _mem_dir = bm25_only_components
        # Short notes dense in the topic term outrank the one long note that
        # mentions it once, so BM25 puts every distractor above the target.
        distractors = [
            _make_chunk(
                f"zephyrine zephyrine zephyrine {side} distractor {i}",
                source=f"{side}-{i}.md",
                created_at=distractor_at,
            )
            for i in range(self.DISTRACTORS)
        ]
        target = _make_chunk(
            "aprilmarker: zephyrine came up once in a long planning note that "
            "mostly discusses budgets, hiring, release dates, office moves, "
            "vendor contracts, onboarding checklists and the quarterly offsite.",
            source="target.md",
            created_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        )
        await comp.storage.upsert_chunks([*distractors, target])

        # The premise: without a window the target ranks below every distractor.
        unbounded, _ = await comp.search_pipeline.search(
            query="zephyrine", top_k=self.DISTRACTORS + 1
        )
        ids = [r.chunk.id for r in unbounded]
        assert sorted(ids) == sorted([*(c.id for c in distractors), target.id])
        assert ids[-1] == target.id

        out = await mem_timeline(
            topic="zephyrine",
            since="2026-04",
            until="2026-04",
            limit=self.LIMIT,
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "aprilmarker" in out
        assert "distractor" not in out
        assert "Total: 1 memories" in out


def _timeline_app(results_for_top_k):
    """A stub app whose pipeline honours the requested ``top_k``."""
    from unittest.mock import AsyncMock, MagicMock

    from memtomem.search.pipeline import RetrievalStats

    async def search(**kwargs):
        return results_for_top_k(kwargs["top_k"]), RetrievalStats()

    app = MagicMock()
    app.search_pipeline.search = AsyncMock(side_effect=search)
    return app


def _april_results(count: int):
    from memtomem.models import SearchResult

    return [
        SearchResult(
            chunk=_make_chunk(
                f"april note {i}",
                source=f"apr-{i}.md",
                created_at=datetime(2026, 4, 1 + i % 28, tzinfo=timezone.utc),
            ),
            score=1.0 - i / 1000,
            rank=i + 1,
            source="bm25",
        )
        for i in range(count)
    ]


class TestTimelineForwardsTheWindow:
    @pytest.mark.asyncio
    async def test_the_bounds_and_one_extra_slot_reach_the_pipeline(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda _k: [])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        await temporal_mod.mem_timeline(
            topic="anything", since="2026-04", until="2026-04", limit=7, ctx=None
        )

        kwargs = app.search_pipeline.search.await_args.kwargs
        assert kwargs["created_from"] == datetime(2026, 4, 1, tzinfo=timezone.utc)
        assert kwargs["created_before"] == datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert kwargs["top_k"] == 8

    @pytest.mark.asyncio
    async def test_no_window_forwards_no_bounds(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda _k: [])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        await temporal_mod.mem_timeline(topic="anything", ctx=None)

        kwargs = app.search_pipeline.search.await_args.kwargs
        assert kwargs["created_from"] is None
        assert kwargs["created_before"] is None

    @pytest.mark.asyncio
    async def test_mem_do_dispatch_forwards_the_bounds(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod
        from memtomem.server.tools.meta import mem_do

        app = _timeline_app(lambda _k: [])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        await mem_do(
            action="timeline",
            params={"topic": "anything", "since": "2026-04-02", "until": "2026-04-09"},
            ctx=None,
        )

        kwargs = app.search_pipeline.search.await_args.kwargs
        assert kwargs["created_from"] == datetime(2026, 4, 2, tzinfo=timezone.utc)
        assert kwargs["created_before"] == datetime(2026, 4, 10, tzinfo=timezone.utc)


class TestTimelineOverflowNote:
    @pytest.mark.asyncio
    async def test_more_matches_than_limit_are_reported(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda k: _april_results(10)[:k])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await temporal_mod.mem_timeline(
            topic="april", since="2026-04", until="2026-04", limit=3, ctx=None
        )

        assert "Total: 3 memories" in out
        assert out.endswith(
            "More memories in this period match; showing the top 3. "
            "Raise limit (max 500) to see more."
        )

    @pytest.mark.asyncio
    async def test_exactly_limit_matches_get_no_note(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda k: _april_results(3)[:k])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await temporal_mod.mem_timeline(
            topic="april", since="2026-04", until="2026-04", limit=3, ctx=None
        )

        assert "Total: 3 memories" in out
        assert "More memories" not in out

    @pytest.mark.asyncio
    async def test_at_the_maximum_limit_the_note_does_not_suggest_raising_it(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda k: _april_results(501)[:k])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await temporal_mod.mem_timeline(
            topic="april", since="2026-04", until="2026-04", limit=500, ctx=None
        )

        assert "Total: 500 memories" in out
        assert "Raise limit" not in out
        assert out.endswith(
            "More memories in this period match; showing the top 500. "
            "Try a narrower topic or since/until; the timeline may still be incomplete."
        )


class TestTimelineRefusesBeforeSearching:
    @pytest.mark.asyncio
    async def test_a_malformed_bound_is_reported_even_when_nothing_would_match(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda _k: [])
        get_app = AsyncMock(return_value=app)
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", get_app)

        out = await temporal_mod.mem_timeline(topic="anything", since="2026-13", ctx=None)

        assert out.startswith("Error: ")
        get_app.assert_not_awaited()
        app.search_pipeline.search.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topic", ["", "   "])
    async def test_a_blank_topic_does_not_browse_the_window(self, monkeypatch, topic):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod

        app = _timeline_app(lambda k: _april_results(5)[:k])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await temporal_mod.mem_timeline(
            topic=topic, since="2026-04", until="2026-04", ctx=None
        )

        assert out == f"No memories found for topic '{topic}'."
        app.search_pipeline.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_null_topic_through_mem_do_does_not_browse_the_window(self, monkeypatch):
        from unittest.mock import AsyncMock

        from memtomem.server.tools import temporal as temporal_mod
        from memtomem.server.tools.meta import mem_do

        app = _timeline_app(lambda k: _april_results(5)[:k])
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))

        out = await mem_do(
            action="timeline",
            params={"topic": None, "since": "2026-04", "until": "2026-04"},
            ctx=None,
        )

        assert out == "No memories found for topic 'None'."
        app.search_pipeline.search.assert_not_awaited()
