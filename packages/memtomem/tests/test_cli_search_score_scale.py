"""The table's ``Score`` column says what scale it is on.

A fused score is a rank sum, not a similarity: ``weight / (k + rank)`` summed
over the legs that returned the document. A single-leg top hit is therefore
``1/(60+1) = 0.0164``, which reads as "1.6% match" to anyone who assumes a
percentage. #1767 put ``score_scale`` in the JSON payload; these pin the
same provenance onto the human-facing table.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.models import SearchResult
from memtomem.search.pipeline import ScoreScale
from memtomem.search.pipeline import RetrievalStats

from helpers import make_chunk


def _result(score: float = 0.0164) -> SearchResult:
    chunk = make_chunk("connection pool exhausted", source="incident.md")
    return SearchResult(chunk=chunk, score=score, rank=1, source="bm25")


def _invoke(monkeypatch, *args, stats: RetrievalStats):
    pipeline_mock = AsyncMock(return_value=([_result()], stats))
    comp = SimpleNamespace(
        search_pipeline=SimpleNamespace(search=pipeline_mock),
        config=SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[])),
    )

    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    return CliRunner().invoke(cli, ["search", *args, "redis"])


class TestTableScoreScale:
    @pytest.mark.parametrize(
        ("scale", "expected"),
        [
            ("rrf", "RRF fusion score, not a similarity or a percentage"),
            ("rerank", "cross-encoder score; the range is model-dependent"),
            ("bm25", "BM25 keyword score, not a similarity or a percentage"),
            ("dense", "cosine similarity"),
        ],
    )
    def test_table_captions_every_ranked_scale(
        self, monkeypatch, scale: ScoreScale, expected: str
    ) -> None:
        stats = RetrievalStats(bm25_candidates=1, final_total=1, score_scale=scale)

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert result.exit_code == 0
        assert f"Score: {expected}" in result.stdout

    def test_the_rrf_caption_lands_under_the_number_it_explains(self, monkeypatch) -> None:
        """Ordering is the point: the caption is useless above the table."""
        stats = RetrievalStats(bm25_candidates=1, final_total=1, score_scale="rrf")

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert result.stdout.index("0.0164") < result.stdout.index("Score: RRF")

    def test_an_unranked_result_set_gets_no_caption(self, monkeypatch) -> None:
        """``score_scale="none"`` means nothing ranked it; a caption would lie."""
        stats = RetrievalStats(bm25_candidates=1, final_total=1, score_scale="none")

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert result.exit_code == 0
        assert "incident.md" in result.stdout, "the table itself must have rendered"
        assert "Score:" not in result.stdout

    def test_a_missing_scale_gets_no_caption(self, monkeypatch) -> None:
        stats = RetrievalStats(bm25_candidates=1, final_total=1)

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert result.exit_code == 0
        assert "incident.md" in result.stdout, "the table itself must have rendered"
        assert "Score:" not in result.stdout

    def test_the_caption_says_modifiers_can_rescale_the_number(self, monkeypatch) -> None:
        """``score_scale`` is the BASE scale: decay and the boost stages
        multiply on top, so naming a bare cosine/fusion value would be wrong
        whenever a modifier is enabled."""
        stats = RetrievalStats(bm25_candidates=1, final_total=1, score_scale="dense")

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert "decay/boost stages, when enabled, scale it" in result.stdout

    def test_the_leg_footer_is_still_printed(self, monkeypatch) -> None:
        """The caption is additive — it must not displace the existing footer."""
        stats = RetrievalStats(
            bm25_candidates=1, dense_candidates=2, final_total=1, score_scale="rrf"
        )

        result = _invoke(monkeypatch, "--format", "table", stats=stats)

        assert "1 BM25 + 2 dense → 1 results" in result.stdout

    def test_json_stdout_stays_a_bare_list(self, monkeypatch) -> None:
        """Machine consumers pipe stdout; the caption is table-only."""
        stats = RetrievalStats(bm25_candidates=1, final_total=1, score_scale="rrf")

        result = _invoke(monkeypatch, "--format", "json", stats=stats)

        payload = json.loads(result.stdout)
        assert payload[0]["score_scale"] == "rrf"
        assert "Score:" not in result.stdout
