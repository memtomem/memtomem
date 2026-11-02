"""Direct unit tests for time-based search score decay."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.search.decay import apply_score_decay, decay_factor


UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _make_result(score: float, updated_at: datetime, *, rank: int = 1) -> SearchResult:
    chunk = Chunk(
        content="memory",
        metadata=ChunkMetadata(source_file=Path("/tmp/memory.md")),
        updated_at=updated_at,
    )
    return SearchResult(chunk=chunk, score=score, rank=rank, source="test")


@pytest.mark.parametrize(
    ("age_days", "half_life_days", "expected"),
    [
        (0, 30, 1.0),
        (30, 30, 0.5),
        (60, 30, 0.25),
        (-1, 30, 1.0),
        (10, 0, 1.0),
        (10, -30, 1.0),
    ],
)
def test_decay_factor_boundaries(age_days, half_life_days, expected):
    assert decay_factor(age_days, half_life_days) == pytest.approx(expected)


def test_apply_score_decay_rescores_sorts_and_reranks():
    two_months_old = _make_result(0.9, NOW - timedelta(days=60), rank=1)
    one_month_old = _make_result(0.8, NOW - timedelta(days=30), rank=2)
    fresh = _make_result(0.6, NOW, rank=3)

    decayed = apply_score_decay(
        [two_months_old, one_month_old, fresh],
        half_life_days=30,
        now=NOW,
    )

    assert [result.chunk.id for result in decayed] == [
        fresh.chunk.id,
        one_month_old.chunk.id,
        two_months_old.chunk.id,
    ]
    assert [result.score for result in decayed] == pytest.approx([0.6, 0.4, 0.225])
    assert [result.rank for result in decayed] == [1, 2, 3]


def test_apply_score_decay_empty_results():
    assert apply_score_decay([], half_life_days=30, now=NOW) == []


def test_apply_score_decay_future_timestamp_keeps_score_unchanged():
    future = _make_result(0.75, NOW + timedelta(days=2))

    decayed = apply_score_decay([future], half_life_days=30, now=NOW)

    assert len(decayed) == 1
    assert decayed[0].score == pytest.approx(0.75)


def test_apply_score_decay_treats_naive_timestamp_as_utc():
    naive_updated_at = (NOW - timedelta(days=30)).replace(tzinfo=None)
    result = _make_result(0.8, naive_updated_at)

    decayed = apply_score_decay([result], half_life_days=30, now=NOW)

    assert decayed[0].score == pytest.approx(0.4)


def test_apply_score_decay_non_positive_half_life_returns_original_results():
    result = _make_result(0.8, NOW - timedelta(days=30))
    results = [result]

    assert apply_score_decay(results, half_life_days=0, now=NOW) is results
