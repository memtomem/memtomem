"""Tests for conflict detection."""

import pytest
from memtomem.search.conflict import _jaccard_tokens, ConflictCandidate


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
        chunk = Chunk(content="test", metadata=ChunkMetadata(source_file=Path("/t.md")), embedding=[])
        c = ConflictCandidate(existing_chunk=chunk, similarity=0.9, text_overlap=0.1, conflict_score=0.8)
        assert c.conflict_score == pytest.approx(0.8)
        assert c.similarity > c.text_overlap
