"""Tests for conflict detection."""

import logging

import pytest
from memtomem.search.conflict import _jaccard_tokens, ConflictCandidate, detect_conflicts


class TestJaccardTokens:
    def test_identical(self):
        assert _jaccard_tokens("hello world", "hello world") == pytest.approx(1.0)

    def test_completely_different(self):
        assert _jaccard_tokens("hello world", "foo bar") == pytest.approx(0.0)

    def test_partial_overlap(self):
        j = _jaccard_tokens("hello world foo", "hello bar baz")
        # intersection={"hello"}, union={"hello","world","foo","bar","baz"} -> 1/5=0.2
        assert j == pytest.approx(0.2)

    def test_empty_string(self):
        assert _jaccard_tokens("", "hello") == pytest.approx(0.0)
        assert _jaccard_tokens("hello", "") == pytest.approx(0.0)

    def test_case_insensitive(self):
        assert _jaccard_tokens("Hello World", "hello world") == pytest.approx(1.0)

    def test_single_word_match(self):
        j = _jaccard_tokens("deploy", "deploy production server")
        # intersection={"deploy"}, union={"deploy","production","server"} -> 1/3
        assert j == pytest.approx(1 / 3)


class TestConflictCandidate:
    def test_conflict_score(self):
        from pathlib import Path
        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="test", metadata=ChunkMetadata(source_file=Path("/t.md")), embedding=[]
        )
        c = ConflictCandidate(
            existing_chunk=chunk, similarity=0.9, text_overlap=0.1, conflict_score=0.8
        )
        assert c.conflict_score == pytest.approx(0.8)
        assert c.similarity > c.text_overlap


class TestDetectConflictsFailure:
    """Conflict detection failure must surface as WARNING, not silent debug."""

    @pytest.mark.asyncio
    async def test_embedder_failure_logs_warning(self, caplog):
        class _BrokenEmbedder:
            async def embed_query(self, _text: str):
                raise RuntimeError("embedder unavailable")

        class _DummyStorage:
            async def dense_search(self, *args, **kwargs):
                return []

        with caplog.at_level(logging.WARNING, logger="memtomem.search.conflict"):
            result = await detect_conflicts("new content", _DummyStorage(), _BrokenEmbedder())

        assert result == []
        assert any(
            rec.levelno == logging.WARNING and "Conflict detection failed" in rec.message
            for rec in caplog.records
        ), "Expected WARNING log when conflict detection fails (not silent debug)"

    @pytest.mark.asyncio
    async def test_storage_failure_logs_warning(self, caplog):
        class _DummyEmbedder:
            async def embed_query(self, _text: str):
                return [0.0, 0.0, 0.0]

        class _BrokenStorage:
            async def dense_search(self, *args, **kwargs):
                raise RuntimeError("storage unavailable")

        with caplog.at_level(logging.WARNING, logger="memtomem.search.conflict"):
            result = await detect_conflicts("new content", _BrokenStorage(), _DummyEmbedder())

        assert result == []
        assert any(
            rec.levelno == logging.WARNING and "Conflict detection failed" in rec.message
            for rec in caplog.records
        )


class _StubEmbedder:
    def __init__(self, vector=None):
        self.vector = vector if vector is not None else [0.1, 0.2, 0.3]

    async def embed_query(self, text):
        return self.vector


class _StubStorage:
    """Dense search that replays canned results and records its arguments."""

    dense_enabled = True

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def dense_search(self, embedding, top_k=20, **kwargs):
        self.calls.append({"embedding": embedding, "top_k": top_k, **kwargs})
        return self.results


def _result(content, score):
    from pathlib import Path

    from memtomem.models import Chunk, ChunkMetadata
    from memtomem.storage.base import SearchResult

    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/notes.md")),
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=score, rank=1, source="dense")


class TestFindNeighbours:
    @pytest.mark.asyncio
    async def test_labels_each_neighbour_and_sorts_by_dense_score(self):
        from memtomem.search.conflict import find_neighbours

        content = "deploy with blue-green rollout"
        storage = _StubStorage(
            [
                # same topic, different words -> conflict shape
                _result("release via canary staged traffic shifting", 0.9),
                # near-identical wording -> restatement shape
                _result("deploy with blue-green rollout everywhere", 0.8),
                # weak score, low overlap -> neither
                _result("unrelated invoice processing notes", 0.2),
            ]
        )
        got = await find_neighbours(content, storage, _StubEmbedder())

        assert [n.label for n in got] == [
            "potential_conflict",
            "restatement_candidate",
            "related",
        ]
        assert [n.dense_score for n in got] == [0.9, 0.8, 0.2]

    @pytest.mark.asyncio
    async def test_threshold_edges_are_inclusive_on_the_labelled_side(self):
        from memtomem.search.conflict import (
            CONFLICT_OVERLAP_MAX,
            DENSE_SCORE_THRESHOLD,
            _label,
        )

        # Exactly at the dense threshold with no overlap: conflict shape.
        assert _label(DENSE_SCORE_THRESHOLD, 0.0) == "potential_conflict"
        # A hair below: not enough evidence that it is the same topic.
        assert _label(DENSE_SCORE_THRESHOLD - 0.01, 0.0) == "related"
        # Overlap exactly at the conflict ceiling is no longer "different words".
        assert _label(0.99, CONFLICT_OVERLAP_MAX) == "related"

    @pytest.mark.asyncio
    async def test_applies_no_score_filter_unlike_detect_conflicts(self):
        from memtomem.search.conflict import find_neighbours

        storage = _StubStorage([_result("faint echo", 0.01)])
        got = await find_neighbours("something else", storage, _StubEmbedder())
        assert len(got) == 1

    @pytest.mark.asyncio
    async def test_passes_top_k_and_project_root_through_to_storage(self):
        from pathlib import Path

        from memtomem.search.conflict import find_neighbours

        storage = _StubStorage([])
        await find_neighbours(
            "x", storage, _StubEmbedder(), top_k=3, project_context_root=Path("/proj")
        )
        assert storage.calls[0]["top_k"] == 3
        assert storage.calls[0]["project_context_root"] == Path("/proj")

    @pytest.mark.asyncio
    async def test_errors_propagate_instead_of_becoming_an_empty_list(self):
        from memtomem.search.conflict import find_neighbours

        class _Mismatch:
            dense_enabled = True

            async def dense_search(self, *a, **k):
                raise ValueError("Embedding dimension mismatch: query has 3d")

        with pytest.raises(ValueError, match="dimension mismatch"):
            await find_neighbours("x", _Mismatch(), _StubEmbedder())


class TestDetectConflictsStillFilters:
    @pytest.mark.asyncio
    async def test_keeps_only_high_score_low_overlap_and_ranks_by_conflict_score(self):
        content = "deploy with blue-green rollout"
        storage = _StubStorage(
            [
                _result("release via canary staged traffic shifting", 0.9),
                _result("deploy with blue-green rollout everywhere", 0.95),  # too much overlap
                _result("something entirely different and unrelated", 0.5),  # below threshold
            ]
        )
        got = await detect_conflicts(content, storage, _StubEmbedder())
        assert len(got) == 1
        assert got[0].similarity == 0.9
        assert got[0].conflict_score == pytest.approx(0.9 - got[0].text_overlap)

    @pytest.mark.asyncio
    async def test_max_candidates_slices_the_result(self):
        content = "alpha beta gamma"
        storage = _StubStorage([_result(f"zeta{i} eta theta", 0.9) for i in range(4)])
        got = await detect_conflicts(content, storage, _StubEmbedder(), max_candidates=2)
        assert len(got) == 2
