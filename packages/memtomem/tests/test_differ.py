"""Tests for indexing/differ.py compute_diff — pure hash-based chunk diffing."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from memtomem.indexing.differ import compute_diff
from memtomem.models import Chunk, ChunkMetadata


def _mk(content: str) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/t.md")),
        embedding=[],
    )


class TestComputeDiff:
    def test_all_new_when_existing_is_empty(self):
        chunks = [_mk("a"), _mk("b")]
        result = compute_diff({}, chunks)

        assert result.to_upsert == chunks
        assert result.to_delete == []
        assert result.unchanged == []

    def test_all_unchanged_when_every_hash_matches(self):
        chunks = [_mk("a"), _mk("b")]
        existing = {
            str(uuid4()): chunks[0].content_hash,
            str(uuid4()): chunks[1].content_hash,
        }

        result = compute_diff(existing, chunks)

        assert len(result.unchanged) == 2
        assert result.to_upsert == []
        assert result.to_delete == []

    def test_mixed_new_unchanged_and_stale(self):
        unchanged = _mk("keep")
        new = _mk("new")
        stale_id = uuid4()
        existing = {
            str(uuid4()): unchanged.content_hash,
            str(stale_id): "hash-no-longer-present",
        }

        result = compute_diff(existing, [unchanged, new])

        assert result.unchanged == [unchanged]
        assert result.to_upsert == [new]
        assert result.to_delete == [stale_id]

    def test_deletions_when_new_chunks_empty(self):
        stale_a, stale_b = uuid4(), uuid4()
        existing = {str(stale_a): "h1", str(stale_b): "h2"}

        result = compute_diff(existing, [])

        assert set(result.to_delete) == {stale_a, stale_b}
        assert result.to_upsert == []
        assert result.unchanged == []

    def test_duplicate_content_hash_reuses_each_id_at_most_once(self):
        # Two new chunks with identical content share one hash. Two existing
        # chunk IDs also share that hash. Each ID must be reused exactly once
        # — no ID collisions allowed.
        chunks = [_mk("dup"), _mk("dup")]
        assert chunks[0].content_hash == chunks[1].content_hash

        id_a, id_b = uuid4(), uuid4()
        existing = {str(id_a): chunks[0].content_hash, str(id_b): chunks[0].content_hash}

        result = compute_diff(existing, chunks)

        assert len(result.unchanged) == 2
        assert result.to_upsert == []
        assert result.to_delete == []
        reused_ids = {str(c.id) for c in result.unchanged}
        assert reused_ids == {str(id_a), str(id_b)}

    def test_duplicate_hash_partial_reuse_spills_to_upsert(self):
        # Three new chunks share a hash but only two existing IDs match —
        # the third chunk must go to to_upsert rather than silently
        # reusing a duplicate ID.
        chunks = [_mk("x"), _mk("x"), _mk("x")]
        id_a, id_b = uuid4(), uuid4()
        existing = {str(id_a): chunks[0].content_hash, str(id_b): chunks[0].content_hash}

        result = compute_diff(existing, chunks)

        assert len(result.unchanged) == 2
        assert len(result.to_upsert) == 1
        assert result.to_delete == []

    def test_reordering_is_recognized_as_unchanged(self):
        a, b = _mk("first"), _mk("second")
        existing = {str(uuid4()): a.content_hash, str(uuid4()): b.content_hash}

        # Pass in the opposite order — hash-based matching should not care.
        result = compute_diff(existing, [b, a])

        assert len(result.unchanged) == 2
        assert result.to_upsert == []
        assert result.to_delete == []
