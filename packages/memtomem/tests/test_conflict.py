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
