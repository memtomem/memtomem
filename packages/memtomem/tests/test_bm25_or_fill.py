"""The BM25 OR pass fills a short AND result instead of waiting for an empty one (#3662).

The ladder used to run the OR form of a query only when the AND form returned
no rows at all. A query with one word that only a newer note holds then made
the AND pass non-empty, and an older note it supersedes — which shares every
other word — never reached the caller: a search returned one side of a
disagreement with no sign that the other side existed. The default install is
BM25-only, so this was the default behaviour.

Now, when the AND pass returns at least one row but fewer than ``top_k``, one
statement returns the AND rows followed by the OR rows that are not AND
matches. The AND rows keep their positions and scores; the fill rows are
scaled so that no fill row scores above the last AND row, because the rescue
merge and the BM25-only passthrough sort by score.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem.config import SearchConfig, SessionSummaryConfig
from memtomem.models import Chunk, ChunkMetadata, NamespaceFilter
from memtomem.search.pipeline import SearchPipeline
from memtomem.storage.base import SearchMetadataFilter
from memtomem.storage.sqlite_backend import _column_match

from helpers import make_chunk as _make_chunk

_FILLER = " ".join(f"word{i}" for i in range(200))

AUGUST = "CI pushes container images to cr-otter-2207.internal."
OCTOBER = (
    "Replacing the August registry decision: CI now pushes container images to "
    "cr-falcon-5813.internal. The August host is read-only."
)


def _unrelated(n: int) -> list:
    """Chunks that share no query word, so every query term has a positive IDF."""
    return [
        _make_chunk(f"unrelated note number {i} about gardening", source=f"misc/{i}.md")
        for i in range(n)
    ]


def _raw_scores(storage, query: str, *, use_or: bool) -> dict[str, float]:
    """``abs(rank)`` per chunk id for one column-qualified MATCH, no filters."""
    rows = (
        storage._get_db()
        .execute(
            "SELECT c.id, fts.rank FROM chunks_fts fts JOIN chunks c ON c.rowid = fts.rowid "
            "WHERE chunks_fts MATCH ?",
            [_column_match("content", query, use_or=use_or)],
        )
        .fetchall()
    )
    return {row[0]: abs(row[1]) for row in rows}


class _MatchTrace:
    """Counts the FTS statements a search runs, on every connection it can use."""

    def __init__(self, storage):
        self.statements: list[str] = []
        self._conns = [storage._get_db(), *storage._read_pool]

    def __enter__(self):
        for conn in self._conns:
            conn.set_trace_callback(self._record)
        return self

    def __exit__(self, *exc):
        for conn in self._conns:
            conn.set_trace_callback(None)

    def _record(self, statement: str) -> None:
        if "FROM chunks_fts fts" in statement:
            self.statements.append(statement)


class TestTheSupersededNoteIsReturned:
    """The issue's shape: a word only the newer note holds."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        [
            "container registry host CI push images",
            "CI container image registry host push",
            "CI image registry",
        ],
    )
    async def test_both_notes_come_back_october_first(self, storage, query):
        august = _make_chunk(AUGUST, source="notes/2026-08-04.md")
        october = _make_chunk(OCTOBER, source="notes/2026-10-14.md")
        await storage.upsert_chunks([august, october, *_unrelated(5)])

        results = await storage.bm25_search(query, top_k=20)

        assert [r.chunk.id for r in results] == [october.id, august.id], (
            f"{query!r}: {[Path(r.chunk.metadata.source_file).name for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_a_query_sharing_no_word_with_the_older_note_still_misses_it(self, storage):
        """The bound of the fix: OR needs at least one shared term."""
        august = _make_chunk(AUGUST, source="notes/2026-08-04.md")
        october = _make_chunk(OCTOBER, source="notes/2026-10-14.md")
        await storage.upsert_chunks([august, october, *_unrelated(5)])

        results = await storage.bm25_search("registry decision August read-only", top_k=20)

        assert [r.chunk.id for r in results] == [october.id]

    @pytest.mark.asyncio
    async def test_the_bm25_only_pipeline_returns_both(self, bm25_only_components):
        comp, _mem_dir = bm25_only_components
        assert comp.config.search.enable_dense is False
        august = _make_chunk(AUGUST, source="notes/2026-08-04.md")
        october = _make_chunk(OCTOBER, source="notes/2026-10-14.md")
        await comp.storage.upsert_chunks([august, october, *_unrelated(5)])

        results, stats = await comp.search_pipeline.search(
            "container registry host CI push images", top_k=10, record=False
        )

        assert stats.score_scale == "bm25"
        assert [r.chunk.id for r in results] == [october.id, august.id]


class TestTheFillKeepsTheAndRowsAndTheFilters:
    @pytest.mark.asyncio
    async def test_and_rows_first_then_eligible_or_rows_only(self, storage):
        both = _make_chunk("pangolin and quokka share a note", source="notes/both.md")
        eligible = _make_chunk("pangolin diets", source="notes/pangolin.md")
        other_ns = _make_chunk(
            "pangolin pangolin habitats", source="notes/other.md", namespace="elsewhere"
        )
        path_only = _make_chunk("nothing relevant", source="plans/quokka.md")
        await storage.upsert_chunks([both, eligible, other_ns, path_only, *_unrelated(5)])

        results = await storage.bm25_search(
            "pangolin quokka", top_k=10, namespace_filter=NamespaceFilter.parse("default")
        )

        assert [r.chunk.id for r in results] == [both.id, eligible.id]
        assert [r.rank for r in results] == [1, 2]

    @pytest.mark.asyncio
    async def test_a_held_source_stays_out_of_the_fill(self, storage):
        both = _make_chunk("pangolin and quokka share a note", source="notes/both.md")
        held = _make_chunk("pangolin pangolin pangolin", source="notes/held.md")
        eligible = _make_chunk(f"pangolin {_FILLER}", source="notes/long.md")
        await storage.upsert_chunks([both, held, eligible, *_unrelated(5)])
        assert str(held.id) in _raw_scores(storage, "pangolin quokka", use_or=True)
        assert await storage.hold_source(held.metadata.source_file, "test")

        results = await storage.bm25_search("pangolin quokka", top_k=10)

        assert [r.chunk.id for r in results] == [both.id, eligible.id]

    @pytest.mark.asyncio
    async def test_metadata_and_source_filters_bind_in_the_fill_arm(self, storage):
        both = _make_chunk(
            "pangolin and quokka share a note", tags=("zoo",), source="notes/both.md"
        )
        untagged = _make_chunk("pangolin pangolin pangolin", source="notes/untagged.md")
        tagged_elsewhere = _make_chunk("pangolin pangolin", tags=("zoo",), source="other/far.md")
        eligible = _make_chunk(f"pangolin {_FILLER}", tags=("zoo",), source="notes/long.md")
        await storage.upsert_chunks([both, untagged, tagged_elsewhere, eligible, *_unrelated(5)])

        results = await storage.bm25_search(
            "pangolin quokka",
            top_k=10,
            metadata_filter=SearchMetadataFilter(tags_any=("zoo",)),
            source_filter="notes/",
        )

        assert [r.chunk.id for r in results] == [both.id, eligible.id]

    @pytest.mark.asyncio
    async def test_another_projects_or_hit_stays_out_of_the_fill(self, storage, tmp_path):
        proj_a, proj_b = tmp_path / "proj_a", tmp_path / "proj_b"

        def at(content: str, root: Path) -> Chunk:
            return Chunk(
                content=content,
                metadata=ChunkMetadata(
                    source_file=root / ".memtomem" / "memories" / f"{len(content)}.md",
                    scope="project_shared",
                    project_root=root,
                ),
            )

        both = _make_chunk("pangolin and quokka share a note", source="notes/both.md")
        mine = at(f"pangolin {_FILLER}", proj_a)
        theirs = at("pangolin pangolin pangolin", proj_b)
        await storage.upsert_chunks([both, mine, theirs, *_unrelated(5)])
        raw_or = _raw_scores(storage, "pangolin quokka", use_or=True)
        assert raw_or[str(theirs.id)] > raw_or[str(mine.id)], "the other project must rank higher"

        results = await storage.bm25_search(
            "pangolin quokka", top_k=10, project_context_root=proj_a
        )

        assert [r.chunk.id for r in results] == [both.id, mine.id]

    @pytest.mark.asyncio
    async def test_the_fill_stops_at_top_k(self, storage):
        both = _make_chunk("pangolin and quokka share a note", source="notes/both.md")
        # Longer bodies score lower, so the OR order is the list order.
        fill = [
            _make_chunk(
                "pangolin " + " ".join(f"pad{j}" for j in range(4 * i)), source=f"notes/p{i}.md"
            )
            for i in range(5)
        ]
        await storage.upsert_chunks([both, *fill, *_unrelated(5)])

        results = await storage.bm25_search("pangolin quokka", top_k=3)

        assert [r.chunk.id for r in results] == [both.id, fill[0].id, fill[1].id]

    @pytest.mark.asyncio
    async def test_the_filename_phase_fills_too(self, storage):
        and_path = _make_chunk("nothing relevant", source="plans/wombat-quokka.md")
        # Shorter names score higher in the path column.
        or_paths = [
            _make_chunk("still nothing", source=f"plans/wombat{'-x' * i}.md") for i in range(4)
        ]
        await storage.upsert_chunks([and_path, *or_paths, *_unrelated(5)])

        results = await storage.bm25_search("wombat quokka", top_k=3)

        assert [r.chunk.id for r in results] == [and_path.id, or_paths[0].id, or_paths[1].id]


class TestTheHostParameterFloor:
    """The fill binds the filters twice; under the pre-3.32 ceiling it must not raise."""

    @pytest.mark.asyncio
    async def test_a_long_namespace_list_keeps_the_and_rows(self, storage):
        both = _make_chunk("pangolin and quokka share a note", source="notes/both.md")
        eligible = _make_chunk("pangolin diets", source="notes/pangolin.md")
        await storage.upsert_chunks([both, eligible, *_unrelated(5)])
        namespaces = NamespaceFilter.parse(",".join(["default", *(f"ns{i}" for i in range(499))]))
        conns = [storage._get_db(), *storage._read_pool]
        previous = [c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999) for c in conns]
        try:
            results = await storage.bm25_search(
                "pangolin quokka", top_k=10, namespace_filter=namespaces
            )
            filled = await storage.bm25_search(
                "pangolin quokka", top_k=10, namespace_filter=NamespaceFilter.parse("default")
            )
        finally:
            for conn, limit in zip(conns, previous, strict=True):
                conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)

        assert [r.chunk.id for r in results] == [both.id]
        assert [r.chunk.id for r in filled] == [both.id, eligible.id]


class TestScores:
    @pytest.mark.asyncio
    async def test_fill_scores_never_exceed_the_last_and_row(self, storage):
        # A long AND row scores low; short single-term rows score high on OR.
        and_row = _make_chunk(f"{_FILLER} pangolin quokka {_FILLER}", source="notes/long.md")
        hi = _make_chunk("pangolin pangolin pangolin", source="notes/hi.md")
        lo = _make_chunk(f"pangolin {' '.join(f'pad{i}' for i in range(20))}", source="notes/lo.md")
        await storage.upsert_chunks([and_row, hi, lo, *_unrelated(10)])
        raw_and = _raw_scores(storage, "pangolin quokka", use_or=False)
        raw_or = _raw_scores(storage, "pangolin quokka", use_or=True)
        assert raw_or[str(hi.id)] > raw_and[str(and_row.id)], "the fixture lost its inversion"
        assert raw_or[str(hi.id)] > raw_or[str(lo.id)]

        results = await storage.bm25_search("pangolin quokka", top_k=10)

        assert [r.chunk.id for r in results] == [and_row.id, hi.id, lo.id]
        scores = [r.score for r in results]
        assert scores[0] == raw_and[str(and_row.id)], "the AND row's score changed"
        assert scores == sorted(scores, reverse=True), scores
        assert scores[1] <= scores[0]
        assert scores[1] / scores[2] == pytest.approx(raw_or[str(hi.id)] / raw_or[str(lo.id)])

    @pytest.mark.asyncio
    async def test_the_cap_is_the_last_and_row_not_the_first(self, storage):
        strong = _make_chunk(
            "pangolin quokka " + " ".join(f"pad{i}" for i in range(10)), source="notes/a1.md"
        )
        weak = _make_chunk(f"{_FILLER} pangolin quokka {_FILLER}", source="notes/a2.md")
        hi = _make_chunk("pangolin pangolin pangolin", source="notes/hi.md")
        await storage.upsert_chunks([strong, weak, hi, *_unrelated(10)])
        raw_and = _raw_scores(storage, "pangolin quokka", use_or=False)
        raw_or = _raw_scores(storage, "pangolin quokka", use_or=True)
        assert raw_and[str(strong.id)] > raw_and[str(weak.id)]
        assert raw_or[str(hi.id)] > raw_and[str(weak.id)], "the fixture lost its inversion"

        results = await storage.bm25_search("pangolin quokka", top_k=10)

        assert [r.chunk.id for r in results] == [strong.id, weak.id, hi.id]
        assert results[0].score == raw_and[str(strong.id)]
        assert results[1].score == raw_and[str(weak.id)]
        assert results[2].score == pytest.approx(raw_and[str(weak.id)])
        assert results[2].score <= results[1].score

    @pytest.mark.asyncio
    async def test_fill_scores_below_the_last_and_row_are_kept(self, storage):
        and_row = _make_chunk("pangolin quokka", source="notes/short.md")
        weak = _make_chunk(f"{_FILLER} pangolin {_FILLER}", source="notes/weak.md")
        await storage.upsert_chunks([and_row, weak, *_unrelated(10)])
        raw_or = _raw_scores(storage, "pangolin quokka", use_or=True)

        results = await storage.bm25_search("pangolin quokka", top_k=10)

        assert [r.chunk.id for r in results] == [and_row.id, weak.id]
        assert results[1].score == raw_or[str(weak.id)]


class TestStatements:
    @pytest.mark.asyncio
    async def test_a_full_and_result_runs_one_statement(self, storage):
        rows = [_make_chunk(f"pangolin quokka {i}", source=f"notes/{i}.md") for i in range(3)]
        await storage.upsert_chunks([*rows, _make_chunk("pangolin only", source="notes/p.md")])

        with _MatchTrace(storage) as trace:
            results = await storage.bm25_search("pangolin quokka", top_k=2)

        assert len(results) == 2
        assert len(trace.statements) == 1, trace.statements

    @pytest.mark.asyncio
    async def test_a_single_term_query_runs_one_statement(self, storage):
        await storage.upsert_chunks([_make_chunk("pangolin", source="notes/p.md")])

        with _MatchTrace(storage) as trace:
            results = await storage.bm25_search("pangolin", top_k=5)

        assert len(results) == 1
        assert len(trace.statements) == 1, trace.statements

    @pytest.mark.asyncio
    async def test_a_partial_and_result_runs_two(self, storage):
        both = _make_chunk("pangolin quokka", source="notes/both.md")
        await storage.upsert_chunks([both, _make_chunk("pangolin only", source="notes/p.md")])

        with _MatchTrace(storage) as trace:
            results = await storage.bm25_search("pangolin quokka", top_k=5)

        assert len(results) == 2
        assert len(trace.statements) == 2, trace.statements


class TestOneSnapshot:
    """The returned list comes from one statement, so a write between the two cannot mix."""

    @pytest.mark.asyncio
    async def test_an_and_row_deleted_between_the_statements_is_not_returned(
        self, storage, monkeypatch
    ):
        both = _make_chunk("pangolin quokka", source="notes/both.md")
        eligible = _make_chunk("pangolin only", source="notes/p.md")
        await storage.upsert_chunks([both, eligible, *_unrelated(5)])
        fired = _delete_before_the_second_statement(storage, monkeypatch, both)

        results = await storage.bm25_search("pangolin quokka", top_k=5)

        assert fired, "the write between the statements never ran"
        assert [r.chunk.id for r in results] == [eligible.id]

    @pytest.mark.asyncio
    async def test_an_empty_fill_falls_through_to_the_filename_phase(self, storage, monkeypatch):
        only = _make_chunk("pangolin quokka", source="notes/both.md")
        by_name = _make_chunk("nothing relevant", source="plans/pangolin.md")
        await storage.upsert_chunks([only, by_name, *_unrelated(5)])
        fired = _delete_before_the_second_statement(storage, monkeypatch, only)

        results = await storage.bm25_search("pangolin quokka", top_k=5)

        assert fired, "the write between the statements never ran"
        assert [r.chunk.id for r in results] == [by_name.id]


def _delete_before_the_second_statement(storage, monkeypatch, victim) -> list[bool]:
    """Delete *victim* from another connection once the first FTS statement is read out.

    The connection a search runs on is wrapped so that, when the second FTS
    statement is about to run, a separate connection deletes the row and
    commits. Returns the list that records the write, so a test can tell it ran.
    """
    db_path = str(storage._config.sqlite_path)
    fired: list[bool] = []

    def delete() -> None:
        writer = sqlite3.connect(db_path)
        try:
            (rowid,) = writer.execute(
                "SELECT rowid FROM chunks WHERE id = ?", [str(victim.id)]
            ).fetchone()
            writer.execute("DELETE FROM chunks_fts WHERE rowid = ?", [rowid])
            writer.execute("DELETE FROM chunks WHERE rowid = ?", [rowid])
            writer.commit()
        finally:
            writer.close()
        fired.append(True)

    class _Conn:
        def __init__(self, inner):
            self._inner = inner
            self._seen = 0

        def execute(self, sql, params=()):
            if "FROM chunks_fts fts" in sql:
                self._seen += 1
                if self._seen == 2 and not fired:
                    delete()
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_run = storage._run_read_in_thread

    async def run(db, fn):
        return await real_run(db, lambda conn: fn(_Conn(conn)))

    monkeypatch.setattr(storage, "_run_read_in_thread", run)
    return fired


class TestTheSummaryLookup:
    """Summary lookup gets the fill like every caller; the score cap decides the threshold."""

    @staticmethod
    def _pipeline(storage, threshold: float) -> SearchPipeline:
        embedder = AsyncMock()
        embedder.embed_query = AsyncMock(return_value=[0.1] * 8)
        return SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=SearchConfig(enable_bm25=True, enable_dense=False),
            session_summary_config=SessionSummaryConfig(
                expansion_lookup_top_k=5, expansion_score_threshold=threshold
            ),
        )

    async def _seed(self, storage):
        ns = "archive:session:s1"
        and_summary = _make_chunk(
            f"{_FILLER} pangolin quokka {_FILLER}", namespace=ns, source="sessions/a.md"
        )
        or_summary = _make_chunk("pangolin pangolin pangolin", namespace=ns, source="sessions/b.md")
        target_a = _make_chunk("target a", source="src/a.md")
        target_b = _make_chunk("target b", source="src/b.md")
        filler = [
            _make_chunk(f"unrelated summary {i}", namespace=ns, source=f"sessions/f{i}.md")
            for i in range(10)
        ]
        await storage.upsert_chunks([and_summary, or_summary, target_a, target_b, *filler])
        await storage.add_chunk_link(and_summary.id, target_a.id, "summarizes", "default")
        await storage.add_chunk_link(or_summary.id, target_b.id, "summarizes", "default")
        raw_and = _raw_scores(storage, "pangolin quokka", use_or=False)[str(and_summary.id)]
        raw_or = _raw_scores(storage, "pangolin quokka", use_or=True)[str(or_summary.id)]
        assert raw_or > raw_and, "the fixture lost its inversion"
        return raw_and, raw_or, target_a, target_b

    @pytest.mark.asyncio
    async def test_a_fill_summary_cannot_pass_where_the_and_summary_did_not(self, storage):
        raw_and, raw_or, _a, _b = await self._seed(storage)
        threshold = (raw_and + raw_or) / 2

        sources = await self._pipeline(storage, threshold)._session_summary_boost_sources(
            "pangolin quokka"
        )

        assert sources == set()

    @pytest.mark.asyncio
    async def test_a_fill_summary_above_the_threshold_adds_its_sources(self, storage):
        raw_and, _raw_or, target_a, target_b = await self._seed(storage)

        sources = await self._pipeline(storage, 0.0)._session_summary_boost_sources(
            "pangolin quokka"
        )

        names = {Path(s).name for s in sources}
        assert names == {
            Path(target_a.metadata.source_file).name,
            Path(target_b.metadata.source_file).name,
        }
        assert raw_and > 0.0


class TestUnderAMorphologicalTokenizer:
    @pytest.mark.asyncio
    async def test_kiwi_partial_and_is_filled(self, storage):
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")
        storage.set_fts_tokenizer("kiwipiepy")
        query = "회의를 했습니다"
        both = _make_chunk("어제 회의를 했습니다", source="notes/ko-a.md")
        meeting = _make_chunk("회의를 진행합니다", source="notes/ko-b.md")
        await storage.upsert_chunks([both, meeting, *_unrelated(5)])
        and_expr = _column_match("content", query, use_or=False, tokenizer="kiwipiepy")
        or_expr = _column_match("content", query, use_or=True, tokenizer="kiwipiepy")
        db = storage._get_db()
        count = "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?"
        assert db.execute(count, [and_expr]).fetchone()[0] == 1, "the AND form must be partial"
        assert db.execute(count, [or_expr]).fetchone()[0] == 2

        results = await storage.bm25_search(query, top_k=5)

        assert [r.chunk.id for r in results] == [both.id, meeting.id]
