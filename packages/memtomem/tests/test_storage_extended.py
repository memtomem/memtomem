"""Extended storage tests covering methods not exercised by existing test suite.

Tests vector search (dense_search), FTS rebuild, chunk hashes, embedding
retrieval, access counting, size distribution, and embedding meta reset.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import make_chunk
from memtomem.models import NamespaceFilter
from memtomem.config import StorageConfig
from memtomem.storage.sqlite_backend import SqliteBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _varied_embedding(seed: float = 0.1, dim: int = 1024) -> list[float]:
    """Return a deterministic but varied embedding vector."""
    return [seed + i * 0.0001 for i in range(dim)]


def _similar_embedding(base: list[float], delta: float = 0.001) -> list[float]:
    """Return an embedding close to *base* (high cosine similarity)."""
    return [v + delta for v in base]


def _distant_embedding(dim: int = 1024) -> list[float]:
    """Return a vector far from the default embeddings."""
    return [0.9 - i * 0.0001 for i in range(dim)]


class TestStorageExtended:
    """Storage backend methods that need additional coverage."""

    # ---- dense_search --------------------------------------------------------

    async def test_dense_search_returns_results(self, components):
        """Insert chunks with known embeddings, search with a similar vector."""
        storage = components.storage
        emb = _varied_embedding(0.2)
        chunk = make_chunk(content="dense search target", embedding=emb)
        await storage.upsert_chunks([chunk])

        results = await storage.dense_search(emb, top_k=5)
        assert len(results) >= 1
        assert results[0].chunk.content == "dense search target"
        assert results[0].source == "dense"

    async def test_dense_search_similar_vector_ranks_higher(self, components):
        """A query embedding close to a chunk should rank it above distant chunks."""
        storage = components.storage
        emb_a = _varied_embedding(0.1)
        emb_b = _distant_embedding()
        chunk_a = make_chunk(content="nearby chunk", embedding=emb_a, source="a.md")
        chunk_b = make_chunk(content="distant chunk", embedding=emb_b, source="b.md")
        await storage.upsert_chunks([chunk_a, chunk_b])

        query = _similar_embedding(emb_a, delta=0.0005)
        results = await storage.dense_search(query, top_k=5)
        assert len(results) == 2
        assert results[0].chunk.content == "nearby chunk"

    async def test_dense_search_respects_top_k(self, components):
        storage = components.storage
        chunks = [
            make_chunk(
                content=f"chunk {i}", source=f"f{i}.md", embedding=_varied_embedding(0.1 + i * 0.01)
            )
            for i in range(5)
        ]
        await storage.upsert_chunks(chunks)

        results = await storage.dense_search(_varied_embedding(0.1), top_k=2)
        assert len(results) == 2

    async def test_dense_search_namespace_filter(self, components):
        storage = components.storage
        emb = _varied_embedding(0.3)
        chunk_a = make_chunk(content="ns-work", namespace="work", embedding=emb, source="w.md")
        chunk_b = make_chunk(
            content="ns-personal",
            namespace="personal",
            embedding=_similar_embedding(emb),
            source="p.md",
        )
        await storage.upsert_chunks([chunk_a, chunk_b])

        ns_filter = NamespaceFilter.parse("work")
        results = await storage.dense_search(emb, top_k=10, namespace_filter=ns_filter)
        namespaces = {r.chunk.metadata.namespace for r in results}
        assert "personal" not in namespaces
        assert len(results) >= 1

    async def test_dense_search_empty_db_returns_empty(self, components):
        storage = components.storage
        results = await storage.dense_search([0.1] * 1024, top_k=5)
        assert results == []

    async def test_dense_search_dimension_mismatch_raises(self, components):
        storage = components.storage
        chunk = make_chunk(content="dim check", embedding=[0.1] * 1024)
        await storage.upsert_chunks([chunk])
        with pytest.raises((ValueError, Exception)):
            await storage.dense_search([0.1] * 512, top_k=5)

    # ---- rebuild_fts ---------------------------------------------------------

    async def test_rebuild_fts_preserves_searchability(self, components):
        """After rebuild_fts, BM25 search should still find indexed content."""
        storage = components.storage
        chunk = make_chunk(content="unique giraffe content for rebuild test")
        await storage.upsert_chunks([chunk])

        rebuilt = await storage.rebuild_fts()
        assert rebuilt >= 1

        results = await storage.bm25_search("giraffe", top_k=5)
        assert len(results) >= 1
        assert "giraffe" in results[0].chunk.content

    async def test_rebuild_fts_empty_db(self, components):
        storage = components.storage
        rebuilt = await storage.rebuild_fts()
        assert rebuilt == 0

    async def test_rebuild_fts_returns_correct_count(self, components):
        storage = components.storage
        chunks = [make_chunk(content=f"rebuild content {i}", source=f"rb{i}.md") for i in range(4)]
        await storage.upsert_chunks(chunks)

        count = await storage.rebuild_fts()
        assert count == 4

    async def test_rebuild_fts_streams_across_batch_boundary(
        self, components, monkeypatch: pytest.MonkeyPatch
    ):
        """Corpora larger than ``_REBUILD_FTS_BATCH_SIZE`` rebuild correctly
        (regression guard for the streaming implementation, issue #278).

        Shrinks the batch size so we can cross the boundary with a small
        corpus without slowing the test down.
        """
        from memtomem.storage import sqlite_backend

        monkeypatch.setattr(sqlite_backend, "_REBUILD_FTS_BATCH_SIZE", 3)

        storage = components.storage
        chunks = [
            make_chunk(content=f"streamed giraffe {i}", source=f"batch{i}.md") for i in range(10)
        ]
        await storage.upsert_chunks(chunks)

        count = await storage.rebuild_fts()
        assert count == 10

        # Searchability must hold across the batch boundary.
        results = await storage.bm25_search("giraffe", top_k=10)
        assert len(results) >= 1

    # ---- get_chunk_hashes ----------------------------------------------------

    async def test_get_chunk_hashes_returns_mapping(self, components):
        storage = components.storage
        chunk = make_chunk(content="hash test content", source="hashed.md")
        await storage.upsert_chunks([chunk])

        hashes = await storage.get_chunk_hashes(Path("/tmp/hashed.md"))
        assert len(hashes) == 1
        values = list(hashes.values())
        assert values[0] == chunk.content_hash

    async def test_get_chunk_hashes_unknown_source(self, components):
        storage = components.storage
        hashes = await storage.get_chunk_hashes(Path("/tmp/nonexistent.md"))
        assert hashes == {}

    async def test_get_chunk_hashes_multiple_chunks_same_source(self, components):
        storage = components.storage
        c1 = make_chunk(content="first section", source="multi.md")
        c2 = make_chunk(content="second section", source="multi.md")
        await storage.upsert_chunks([c1, c2])

        hashes = await storage.get_chunk_hashes(Path("/tmp/multi.md"))
        assert len(hashes) == 2
        hash_values = set(hashes.values())
        assert c1.content_hash in hash_values
        assert c2.content_hash in hash_values

    async def test_get_chunk_index_state_includes_heading_hierarchy(self, components):
        storage = components.storage
        chunk = make_chunk(content="indexed state", source="state.md")
        chunk.metadata = dataclasses.replace(chunk.metadata, heading_hierarchy=("Parent", "Child"))
        await storage.upsert_chunks([chunk])

        state = await storage.get_chunk_index_state(Path("/tmp/state.md"))

        assert state == {str(chunk.id): (chunk.content_hash, ("Parent", "Child"))}

    # ---- get_embeddings_for_chunks -------------------------------------------

    async def test_get_embeddings_for_chunks_returns_vectors(self, components):
        storage = components.storage
        emb = _varied_embedding(0.5)
        chunk = make_chunk(content="embedding fetch test", embedding=emb)
        await storage.upsert_chunks([chunk])

        result = await storage.get_embeddings_for_chunks([str(chunk.id)])
        assert str(chunk.id) in result
        retrieved = result[str(chunk.id)]
        # Vectors should be close to original (f32 serialization may lose tiny precision)
        assert len(retrieved) == 1024
        assert abs(retrieved[0] - emb[0]) < 0.01

    async def test_get_embeddings_for_chunks_empty_list(self, components):
        storage = components.storage
        result = await storage.get_embeddings_for_chunks([])
        assert result == {}

    async def test_get_embeddings_for_chunks_missing_id(self, components):
        storage = components.storage
        fake_id = str(uuid.uuid4())
        result = await storage.get_embeddings_for_chunks([fake_id])
        assert fake_id not in result

    # ---- increment_access / get_access_counts --------------------------------

    async def test_increment_access_and_get(self, components):
        storage = components.storage
        chunk = make_chunk(content="access test content")
        await storage.upsert_chunks([chunk])

        counts_before = await storage.get_access_counts([chunk.id])
        assert counts_before.get(str(chunk.id), 0) == 0

        await storage.increment_access([chunk.id])
        counts_after = await storage.get_access_counts([chunk.id])
        assert counts_after[str(chunk.id)] == 1

    async def test_increment_access_multiple_times(self, components):
        storage = components.storage
        chunk = make_chunk(content="multi access test")
        await storage.upsert_chunks([chunk])

        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])

        counts = await storage.get_access_counts([chunk.id])
        assert counts[str(chunk.id)] == 3

    async def test_increment_access_empty_list(self, components):
        """Should not raise on empty input."""
        storage = components.storage
        await storage.increment_access([])

    async def test_get_access_counts_empty_list(self, components):
        storage = components.storage
        result = await storage.get_access_counts([])
        assert result == {}

    # ---- get_chunk_size_distribution -----------------------------------------

    async def test_chunk_size_distribution_returns_buckets(self, components):
        storage = components.storage
        chunk = make_chunk(content="x" * 300)  # ~100 estimated tokens
        await storage.upsert_chunks([chunk])

        dist = await storage.get_chunk_size_distribution()
        assert isinstance(dist, list)
        bucket_names = {d["bucket"] for d in dist}
        assert "0-32" in bucket_names
        assert "1024+" in bucket_names

        # The 300-char chunk has ~100 tokens -> "64-128" bucket
        target = next(d for d in dist if d["bucket"] == "64-128")
        assert target["count"] >= 1

    async def test_chunk_size_distribution_empty_db(self, components):
        storage = components.storage
        dist = await storage.get_chunk_size_distribution()
        assert isinstance(dist, list)
        total = sum(d["count"] for d in dist)
        assert total == 0

    async def test_chunk_size_distribution_with_source_filter(self, components):
        storage = components.storage
        c1 = make_chunk(content="a" * 150, source="filtered.md")
        c2 = make_chunk(content="b" * 150, source="other.md")
        await storage.upsert_chunks([c1, c2])

        dist = await storage.get_chunk_size_distribution(source_file=Path("/tmp/filtered.md"))
        total = sum(d["count"] for d in dist)
        assert total == 1

    # ---- get_dense_coverage --------------------------------------------------

    async def test_dense_coverage_empty_db(self, components):
        storage = components.storage
        cov = await storage.get_dense_coverage()
        assert cov == {"total": 0, "with_dense": 0}

    async def test_dense_coverage_full_after_upsert(self, components):
        storage = components.storage
        c1 = make_chunk(content="one", embedding=_varied_embedding(0.1), source="a.md")
        c2 = make_chunk(content="two", embedding=_varied_embedding(0.2), source="b.md")
        await storage.upsert_chunks([c1, c2])

        cov = await storage.get_dense_coverage()
        assert cov == {"total": 2, "with_dense": 2}

    async def test_dense_coverage_ignores_stale_vec_sidecar(self, components):
        # Codex carry-over on #898: if ``chunks_vec`` keeps a row whose
        # rowid no longer points at a ``chunks`` row (interrupted
        # upsert, concurrent writer — orphan_gc.py treats this state
        # as expected), the rollup must still report retrievable
        # chunks. A raw ``COUNT(*) FROM chunks_vec`` would show 100%
        # coverage while a real chunk is silently BM25-only, hiding
        # the exact failure mode this telemetry exists to surface.
        from memtomem.storage.sqlite_helpers import serialize_f32

        storage = components.storage
        chunk = make_chunk(content="real", embedding=_varied_embedding(0.1))
        await storage.upsert_chunks([chunk])

        # Inject a stale vec sidecar at a rowid that no ``chunks`` row
        # owns (production source: a partial commit between the chunks
        # delete and the chunks_vec delete in a delete-by-source path).
        db = storage._get_db()
        db.execute(
            "INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
            (999999, serialize_f32(_varied_embedding(0.5))),
        )
        db.commit()

        cov = await storage.get_dense_coverage()
        assert cov["total"] == 1
        # 1 retrievable chunk; the stale sidecar must not count toward
        # ``with_dense`` — otherwise a real BM25-only chunk would be
        # masked by an orphan row.
        assert cov["with_dense"] == 1

    async def test_dense_coverage_zero_when_vec_table_dropped(self, components):
        # ``reset_embedding_meta`` drops and recreates ``chunks_vec`` but
        # leaves ``chunks`` intact, which is the production shape of the
        # BM25-only failure mode this telemetry surfaces. The new vec
        # table is created with the requested dimension but holds no
        # rows until something re-embeds, so coverage should land at 0/N.
        storage = components.storage
        chunk = make_chunk(content="before reset", embedding=_varied_embedding(0.1))
        await storage.upsert_chunks([chunk])

        await storage.reset_embedding_meta(dimension=1024, provider="onnx", model="bge-m3")

        cov = await storage.get_dense_coverage()
        assert cov["total"] == 1
        assert cov["with_dense"] == 0

    # ---- reset_embedding_meta ------------------------------------------------

    async def test_reset_embedding_meta_changes_dimension(self, components):
        storage = components.storage
        chunk = make_chunk(content="before reset", embedding=[0.1] * 1024)
        await storage.upsert_chunks([chunk])

        await storage.reset_embedding_meta(
            dimension=768, provider="openai", model="text-embedding-3-small"
        )

        # Old vector data is gone; DB should accept 768-dim vectors now
        new_chunk = make_chunk(content="after reset", embedding=[0.2] * 768, source="new.md")
        await storage.upsert_chunks([new_chunk])

        results = await storage.dense_search([0.2] * 768, top_k=5)
        assert len(results) >= 1

    async def test_clear_embedding_mismatch_zeroes_both_flags(self, components):
        """clear_embedding_mismatch() must reset both private tuples to None."""
        storage = components.storage
        storage._dim_mismatch = (768, 1024)
        storage._model_mismatch = ("ollama", "nomic-embed-text", "ollama", "bge-m3")
        assert storage.embedding_mismatch is not None

        storage.clear_embedding_mismatch()

        assert storage._dim_mismatch is None
        assert storage._model_mismatch is None
        assert storage.embedding_mismatch is None

    async def test_reset_embedding_meta_clears_mismatch_flags(self, components):
        """reset_embedding_meta() must clear any pending mismatch flags."""
        storage = components.storage
        storage._dim_mismatch = (768, 1024)
        storage._model_mismatch = (
            "ollama",
            "nomic-embed-text",
            "openai",
            "text-embedding-3-small",
        )
        assert storage.embedding_mismatch is not None

        await storage.reset_embedding_meta(
            dimension=768, provider="openai", model="text-embedding-3-small"
        )

        assert storage.embedding_mismatch is None
        assert storage._dim_mismatch is None
        assert storage._model_mismatch is None

    async def test_legacy_onnx_db_enters_policy_degraded_mode(self, tmp_path):
        db_path = tmp_path / "legacy-policy.db"
        cfg = StorageConfig(sqlite_path=db_path)

        legacy = SqliteBackend(
            cfg,
            dimension=8,
            embedding_provider="onnx",
            embedding_model="test-model",
        )
        await legacy.initialize()
        await legacy.close()

        current = SqliteBackend(
            cfg,
            dimension=8,
            embedding_provider="onnx",
            embedding_model="test-model",
            embedding_policy_fingerprint="onnx:v1:max_sequence_tokens=1024",
            embedding_max_sequence_tokens=1024,
        )
        await current.initialize()
        try:
            mismatch = current.embedding_mismatch
            assert mismatch is not None
            assert mismatch["policy_mismatch"] is True
            assert mismatch["stored"]["max_sequence_tokens"] == 0
            assert mismatch["configured"]["max_sequence_tokens"] == 1024

            await current.reset_embedding_meta(
                dimension=8,
                provider="onnx",
                model="test-model",
                policy_fingerprint="onnx:v1:max_sequence_tokens=1024",
                max_sequence_tokens=1024,
            )
            assert current.embedding_mismatch is None
            assert current.stored_embedding_info["max_sequence_tokens"] == 1024
        finally:
            await current.close()

    # ---- reset_all -----------------------------------------------------------

    async def test_reset_all_deletes_all_chunks(self, components):
        storage = components.storage
        chunks = [make_chunk(content=f"chunk {i}", source=f"f{i}.md") for i in range(5)]
        await storage.upsert_chunks(chunks)
        stats_before = await storage.get_stats()
        assert stats_before["total_chunks"] == 5

        deleted = await storage.reset_all()

        stats_after = await storage.get_stats()
        assert stats_after["total_chunks"] == 0
        assert deleted["chunks"] == 5

    async def test_reset_all_on_empty_db(self, components):
        storage = components.storage
        deleted = await storage.reset_all()
        assert deleted["chunks"] == 0
        stats = await storage.get_stats()
        assert stats["total_chunks"] == 0

    async def test_reset_all_preserves_embedding_meta(self, components):
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="test")])
        await storage.reset_all()

        # Embedding dimension should survive reset
        stored_dim = storage._get_stored_dimension()
        assert stored_dim is not None

    async def test_reset_all_allows_reindex(self, components):
        """After reset, new chunks can be indexed normally."""
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="old data")])
        await storage.reset_all()

        new_chunk = make_chunk(content="new data", source="new.md")
        await storage.upsert_chunks([new_chunk])
        stats = await storage.get_stats()
        assert stats["total_chunks"] == 1

    async def test_reset_all_clears_ai_summary_cache(self, components):
        """``reset_all`` honours its "Delete ALL data" contract by
        clearing user-derived AI summary records, even though the
        ``_memtomem_meta`` table itself is preserved for embedding
        config. Without this, a user-triggered reset would leave LLM
        summaries of the source content on disk, and the next
        ``get_all_ai_summaries`` call would return prose for chunks
        that no longer exist — breaking the privacy contract and the
        Source-tab drift count. Pin so a future refactor of the meta
        preservation rule can't silently regress."""
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="some content")])
        await storage.set_ai_summary(Path("/tmp/test.md"), "Sensitive AI prose.", "sig", "en")
        assert await storage.get_ai_summary(Path("/tmp/test.md")) is not None

        deleted = await storage.reset_all()

        # Counter must surface the cleared summaries so operators see
        # the receipt; absence of the key would let a regression slip
        # through with all-zero counts.
        assert deleted.get("ai_summaries") == 1
        assert await storage.get_ai_summary(Path("/tmp/test.md")) is None
        # ``get_all_ai_summaries`` must also report empty — the prefix
        # scan is the surface that drives the Source-tab banner.
        assert await storage.get_all_ai_summaries() == {}

    async def test_reset_all_preserves_embedding_meta_after_summary_clear(self, components):
        """The fix that clears ``ai_summary:*`` rows must NOT take out
        the embedding-config rows that share the same ``_memtomem_meta``
        table — a too-broad ``DELETE FROM _memtomem_meta`` would force
        every user to re-pick their embedding model after a reset.
        Belt-and-suspenders alongside the existing
        ``test_reset_all_preserves_embedding_meta`` (this variant runs
        the path *with* a summary present so the LIKE filter is
        actually exercised)."""
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="some content")])
        await storage.set_ai_summary(Path("/tmp/test.md"), "S", "sig", "en")

        await storage.reset_all()

        stored_dim = storage._get_stored_dimension()
        assert stored_dim is not None

    # ---- get_source_summaries (heuristic preview) ---------------------------

    async def test_source_summaries_picks_first_chunk_by_start_line(self, components):
        """First-chunk pick is by ``start_line ASC`` — not insertion order.
        Insert deeper section first so insertion order disagrees with the
        expected pick; the test fails if the SQL silently falls back to
        ``rowid``."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        src = Path("/tmp/notes.md")
        chunks = [
            Chunk(
                content="Deep body.",
                metadata=ChunkMetadata(
                    source_file=src,
                    heading_hierarchy=("# Notes", "## Deep"),
                    start_line=50,
                ),
                content_hash="hash-deep",
                embedding=[0.1] * 1024,
            ),
            Chunk(
                content="First paragraph of the file.",
                metadata=ChunkMetadata(
                    source_file=src,
                    heading_hierarchy=("# Notes",),
                    start_line=1,
                ),
                content_hash="hash-first",
                embedding=[0.1] * 1024,
            ),
        ]
        await storage.upsert_chunks(chunks)

        # ``norm_path`` resolves symlinks (macOS ``/tmp`` → ``/private/tmp``);
        # pull the dict's value rather than hard-coding the unresolved key.
        summaries = await storage.get_source_summaries()
        assert len(summaries) == 1
        hh, content = next(iter(summaries.values()))
        assert hh == ["# Notes"]
        assert content == "First paragraph of the file."

    async def test_source_content_match_includes_heading_hierarchy(self, components):
        """Sources filter body matching should include headings too.

        Markdown headings are stored in ``heading_hierarchy`` rather than the
        chunk body, so searching for a heading-only Korean term such as
        "이름" must still return the source.
        """
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        src = Path("/tmp/korean-heading.md")
        chunk = Chunk(
            content="본문에는 검색어가 없습니다.",
            metadata=ChunkMetadata(
                source_file=src,
                heading_hierarchy=("## Secret 이름 오타",),
                start_line=1,
            ),
            content_hash="hash-ko-heading",
            embedding=[0.1] * 1024,
        )
        await storage.upsert_chunks([chunk])

        matches = await storage.search_source_files_by_content("이름")

        assert any(p.name == "korean-heading.md" for p in matches)

    async def test_source_summaries_handles_empty_hierarchy(self, components):
        """Files with body content but no heading come back with an empty
        hierarchy list — callers substitute fallback UI."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        await storage.upsert_chunks(
            [
                Chunk(
                    content="Just text.",
                    metadata=ChunkMetadata(
                        source_file=Path("/tmp/raw.md"),
                        heading_hierarchy=(),
                        start_line=1,
                    ),
                    content_hash="hash-raw",
                    embedding=[0.1] * 1024,
                ),
            ]
        )
        summaries = await storage.get_source_summaries()
        hh, content = next(iter(summaries.values()))
        assert hh == []
        assert content == "Just text."

    # ---- AI summary cache (CRUD + drift) -----------------------------------

    async def test_ai_summary_roundtrip(self, components):
        """``set`` → ``get`` returns the full record (summary, signature,
        language, generated_at)."""
        storage = components.storage
        path = Path("/tmp/notes.md")
        await storage.set_ai_summary(
            path, summary="Two-sentence prose.", signature="abc123", language="en"
        )
        rec = await storage.get_ai_summary(path)
        assert rec is not None
        assert rec["summary"] == "Two-sentence prose."
        assert rec["signature"] == "abc123"
        assert rec["language"] == "en"
        assert "generated_at" in rec

    async def test_ai_summary_overwrite(self, components):
        """``set`` overwrites prior values — no append-only semantics."""
        storage = components.storage
        path = Path("/tmp/notes.md")
        await storage.set_ai_summary(path, "first", "sig1", "en")
        await storage.set_ai_summary(path, "second", "sig2", "ko")
        rec = await storage.get_ai_summary(path)
        assert rec["summary"] == "second"
        assert rec["signature"] == "sig2"
        assert rec["language"] == "ko"

    async def test_ai_summary_missing_returns_none(self, components):
        storage = components.storage
        rec = await storage.get_ai_summary(Path("/tmp/never-saved.md"))
        assert rec is None

    async def test_corrupt_ai_summary_row_dropped_and_logged_without_path(self, components, caplog):
        """A corrupt-JSON AI-summary row is dropped from the result but
        logged at DEBUG so the shrinking set isn't invisible (#1613). The
        log must NOT contain the source path — keys can be secret-shaped
        (feedback_canonical_path_leak_resolved_root); only a fingerprint
        is emitted."""
        import logging

        storage = components.storage
        good = Path("/tmp/good.md")
        await storage.set_ai_summary(good, "valid prose", "sig", "en")

        # Inject a corrupt row directly under a secret-shaped path.
        secret_path = "/tmp/notes-sk-live-abc123XYZ.md"
        db = storage._get_db()
        db.execute(
            "INSERT OR REPLACE INTO _memtomem_meta (key, value) VALUES (?, ?)",
            (f"ai_summary:{secret_path}", "{not valid json"),
        )
        db.commit()

        with caplog.at_level(logging.DEBUG, logger="memtomem.storage.sqlite_backend"):
            summaries = await storage.get_all_ai_summaries()

        # Corrupt row dropped, good row survives.
        assert secret_path not in summaries
        assert any("good.md" in k for k in summaries)
        # Failure is observable...
        corrupt_logs = [r for r in caplog.records if "corrupt JSON" in r.message]
        assert corrupt_logs, "corrupt-JSON drop must be logged at DEBUG"
        # ...but the path never leaks into any log record.
        assert not any("sk-live-abc123XYZ" in r.getMessage() for r in caplog.records)

    async def test_corrupt_single_ai_summary_logged_without_path(self, components, caplog):
        """The single-row accessor ``get_ai_summary`` must not leak the
        secret-shaped source path either — it logs a key fingerprint on
        corrupt JSON, same as the bulk path (#1613)."""
        import logging

        from memtomem.storage.sqlite_backend import _ai_summary_key

        storage = components.storage
        secret_path = Path("/tmp/session-sk-live-QQQ999.md")
        # Inject under the exact key the accessor computes (path
        # normalization must match, or the lookup misses the row).
        db = storage._get_db()
        db.execute(
            "INSERT OR REPLACE INTO _memtomem_meta (key, value) VALUES (?, ?)",
            (_ai_summary_key(secret_path), "}corrupt{"),
        )
        db.commit()

        with caplog.at_level(logging.DEBUG, logger="memtomem.storage.sqlite_backend"):
            rec = await storage.get_ai_summary(secret_path)

        assert rec is None
        assert any("Corrupt ai_summary" in r.getMessage() for r in caplog.records)
        assert not any("sk-live-QQQ999" in r.getMessage() for r in caplog.records)

    async def test_delete_ai_summary_clears_existing_row(self, components):
        """``delete_ai_summary`` is the targeted eviction primitive used
        by the summarizer's stale-cache cleanup paths (zero-chunk
        reindex, LLM-failure on a signature-drifted source). Confirm it
        actually removes the row — without this, the privacy-leak
        regressions surface only in higher-level integration tests."""
        storage = components.storage
        path = Path("/tmp/dropme.md")
        await storage.set_ai_summary(path, "prose", "sig", "en")
        assert await storage.get_ai_summary(path) is not None

        await storage.delete_ai_summary(path)

        assert await storage.get_ai_summary(path) is None

    async def test_delete_ai_summary_missing_is_noop(self, components):
        """Idempotent — deleting a missing row must not raise.
        The summarizer calls this on best-effort paths where it can't
        always know whether a cache row exists; a raise here would
        propagate up and break ``maybe_update_ai_summary``'s fail-soft
        guarantee for indexing."""
        storage = components.storage
        # No prior set — must not raise.
        await storage.delete_ai_summary(Path("/tmp/never-saved.md"))

    async def test_update_chunks_scope_for_source_moves_ai_summary(self, components):
        """``update_chunks_scope_for_source`` rewrites a source's path
        in place (used by ``mm context memory migrate`` and similar
        in-tree relocations). The cache key is path-derived, so an
        in-place rewrite without summary migration would leave the new
        path summary-less *and* leave an orphan ``ai_summary:<old>``
        row contributing to drift counts. Pin the rename: after the
        migration, the old key is gone, the new key holds the same
        record (path migrations don't change what the file is about,
        so the prose stays valid)."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        old_path = Path("/tmp/old-location.md")
        new_path = Path("/tmp/new-location.md")
        await storage.upsert_chunks(
            [
                Chunk(
                    content="body",
                    metadata=ChunkMetadata(
                        source_file=old_path,
                        heading_hierarchy=("# H",),
                        start_line=1,
                    ),
                    content_hash="hash-mig",
                    embedding=[0.1] * 1024,
                ),
            ]
        )
        await storage.set_ai_summary(old_path, "Migrated prose.", "sig-mig", "en")

        moved = await storage.update_chunks_scope_for_source(
            old_path,
            new_path,
            new_scope="project_shared",
            new_project_root=Path("/tmp"),
        )
        assert moved == 1

        # Old path's cache row is gone — no orphan to leak into drift
        # counts or ``get_all_ai_summaries``.
        assert await storage.get_ai_summary(old_path) is None
        # New path carries the migrated record; same prose, same
        # signature, same language tag.
        rec = await storage.get_ai_summary(new_path)
        assert rec is not None
        assert rec["summary"] == "Migrated prose."
        assert rec["signature"] == "sig-mig"
        assert rec["language"] == "en"

    async def test_update_chunks_scope_for_source_no_summary_is_noop(self, components):
        """When no AI summary exists for the moved source (LLM was
        disabled, or generation hadn't run yet), the rename path is a
        clean no-op — chunks move, no spurious meta rows appear at the
        destination. Defends the ``IF EXISTS``-style branch against a
        future "always insert at new" simplification."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        old_path = Path("/tmp/no-summary.md")
        new_path = Path("/tmp/no-summary-moved.md")
        await storage.upsert_chunks(
            [
                Chunk(
                    content="body",
                    metadata=ChunkMetadata(
                        source_file=old_path,
                        heading_hierarchy=("# H",),
                        start_line=1,
                    ),
                    content_hash="hash-ns",
                    embedding=[0.1] * 1024,
                ),
            ]
        )

        await storage.update_chunks_scope_for_source(
            old_path,
            new_path,
            new_scope="user",
            new_project_root=None,
        )

        assert await storage.get_ai_summary(old_path) is None
        assert await storage.get_ai_summary(new_path) is None
        # And neither side appears in the all-summaries scan.
        assert await storage.get_all_ai_summaries() == {}

    async def test_get_all_ai_summaries_excludes_other_meta_keys(self, components):
        """Embedding-dimension and other non-summary rows in
        ``_memtomem_meta`` must not leak into the summary listing — pin
        the prefix scan."""
        storage = components.storage
        # ``initialize`` populates ``embedding_dimension``; force a read
        # to guarantee at least one non-summary row sits alongside ours.
        _ = storage._get_stored_dimension()
        await storage.set_ai_summary(Path("/tmp/a.md"), "AAA", "sig-a", "en")
        await storage.set_ai_summary(Path("/tmp/b.md"), "BBB", "sig-b", "ko")

        summaries = await storage.get_all_ai_summaries()
        assert len(summaries) == 2
        for rec in summaries.values():
            assert "summary" in rec
            assert "language" in rec

    async def test_count_language_drift_excludes_matching_entries(self, components):
        """``count_language_drift("ko")`` counts only entries where
        ``language != "ko"``. Pin the negative comparison so a future bug
        flipping ``!=`` to ``==`` shows up immediately."""
        storage = components.storage
        await storage.set_ai_summary(Path("/tmp/en1.md"), "S1", "sig1", "en")
        await storage.set_ai_summary(Path("/tmp/en2.md"), "S2", "sig2", "en")
        await storage.set_ai_summary(Path("/tmp/ko1.md"), "S3", "sig3", "ko")

        assert await storage.count_language_drift("ko") == 2
        assert await storage.count_language_drift("en") == 1
        assert await storage.count_language_drift("fr") == 3

    async def test_list_language_drift_paths_returns_only_drifting(self, components):
        storage = components.storage
        await storage.set_ai_summary(Path("/tmp/en1.md"), "S1", "sig1", "en")
        await storage.set_ai_summary(Path("/tmp/ko1.md"), "S2", "sig2", "ko")

        ko_drift = await storage.list_language_drift_paths("ko")
        assert len(ko_drift) == 1
        assert "en1.md" in str(ko_drift[0])

    async def test_delete_by_source_clears_ai_summary(self, components):
        """Deleting a source's chunks must also drop its cached summary —
        otherwise stale rows would surface in the next
        ``get_all_ai_summaries`` call and the Source tab would reference
        a deleted file."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        path = Path("/tmp/dropme.md")
        await storage.upsert_chunks(
            [
                Chunk(
                    content="body",
                    metadata=ChunkMetadata(
                        source_file=path,
                        heading_hierarchy=("# H",),
                        start_line=1,
                    ),
                    content_hash="hash-drop",
                    embedding=[0.1] * 1024,
                ),
            ]
        )
        await storage.set_ai_summary(path, "AI prose.", "sig-drop", "en")
        assert await storage.get_ai_summary(path) is not None

        deleted = await storage.delete_by_source(path)
        assert deleted == 1
        # Summary must be gone — same record was keyed by norm_path of
        # the source, so delete_by_source is responsible for cleanup.
        assert await storage.get_ai_summary(path) is None

    async def test_delete_by_source_clears_orphan_summary(self, components):
        """Even when a summary exists with no chunks (orphan from a prior
        bug or aborted index), ``delete_by_source`` clears it. Defends
        the "no chunks but stale summary" path that would otherwise leak
        past the zero-chunk early return."""
        storage = components.storage
        path = Path("/tmp/orphan.md")
        await storage.set_ai_summary(path, "leftover", "sig-orphan", "en")

        deleted = await storage.delete_by_source(path)
        assert deleted == 0  # no chunks
        assert await storage.get_ai_summary(path) is None

    async def test_delete_chunks_clears_summary_when_source_emptied(self, components):
        """Per-chunk ``delete_chunks`` (web chunk-delete fallback, dedup,
        decay) must drop the AI summary cache once the *last* chunk for
        a source is gone. Without this, deleting every chunk of a file
        through the chunk endpoint would still leave LLM-generated prose
        about that source on disk and exposed via
        ``get_all_ai_summaries`` — privacy regression and a stale-data
        source for the Source-tab drift count."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        src = Path("/tmp/single.md")
        chunk = Chunk(
            content="only chunk",
            metadata=ChunkMetadata(
                source_file=src,
                heading_hierarchy=("# H",),
                start_line=1,
            ),
            content_hash="hash-single",
            embedding=[0.1] * 1024,
        )
        await storage.upsert_chunks([chunk])
        await storage.set_ai_summary(src, "AI prose.", "sig-single", "en")

        # Delete via chunk-id path — *not* delete_by_source.
        deleted = await storage.delete_chunks([chunk.id])
        assert deleted == 1
        # Summary must be gone — source has zero remaining chunks.
        assert await storage.get_ai_summary(src) is None

    async def test_delete_chunks_preserves_summary_on_partial_delete(self, components):
        """Partial deletion (some chunks remain for the source) leaves
        the summary alone. Rationale: the signature will mismatch on
        the next reindex and ``maybe_update_ai_summary`` regenerates
        cleanly; clearing here would force a regenerate every time
        dedup or decay shaves a single chunk off a multi-section file,
        which is wasted LLM cost."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        src = Path("/tmp/multi.md")
        chunks = [
            Chunk(
                content=f"section {i}",
                metadata=ChunkMetadata(
                    source_file=src,
                    heading_hierarchy=(f"# H{i}",),
                    start_line=i * 10,
                ),
                content_hash=f"hash-multi-{i}",
                embedding=[0.1] * 1024,
            )
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)
        await storage.set_ai_summary(src, "AI prose.", "sig-multi", "en")

        # Delete just the first chunk — two remain.
        deleted = await storage.delete_chunks([chunks[0].id])
        assert deleted == 1
        rec = await storage.get_ai_summary(src)
        assert rec is not None
        assert rec["summary"] == "AI prose."

    async def test_delete_chunks_in_transaction_preserves_summary_for_rewrite(self, components):
        """Reindex (``IndexingEngine._index_file``) wraps a delete +
        upsert pair in a single ``storage.transaction()``. Mid-
        transaction the source is *temporarily* empty, but the upsert
        immediately follows — clearing the AI summary at the delete
        step would mean a stale-but-renderable preview gets blown away
        and never restored if the post-transaction
        ``maybe_update_ai_summary`` no-ops (LLM down,
        ``auto_summarize=False``, transient error). Pin the deferred-
        cleanup contract: in-transaction ``delete_chunks`` leaves the
        cache row alone."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        src = Path("/tmp/rewrite.md")
        old_chunk = Chunk(
            content="old content",
            metadata=ChunkMetadata(source_file=src, heading_hierarchy=("# H",), start_line=1),
            content_hash="hash-old",
            embedding=[0.1] * 1024,
        )
        new_chunk = Chunk(
            content="new content",
            metadata=ChunkMetadata(source_file=src, heading_hierarchy=("# H",), start_line=1),
            content_hash="hash-new",
            embedding=[0.1] * 1024,
        )
        await storage.upsert_chunks([old_chunk])
        await storage.set_ai_summary(src, "AI prose.", "sig-old", "en")

        # Simulate the engine's reindex flow: delete-all + upsert-new
        # in one transaction. Without the deferred-cleanup gate this
        # test fails: ``delete_chunks`` would clear the summary while
        # the source is briefly empty, before ``upsert_chunks`` lands.
        async with storage.transaction():
            await storage.delete_chunks([old_chunk.id])
            await storage.upsert_chunks([new_chunk])

        rec = await storage.get_ai_summary(src)
        assert rec is not None
        assert rec["summary"] == "AI prose."

    async def test_delete_chunks_in_transaction_skips_cleanup(self, components):
        """Deferred-cleanup gate is intentionally permissive: even when
        an in-transaction delete truly empties a source (no follow-up
        upsert), the cleanup is left to the outer scope. In practice
        that's either ``delete_by_source`` (clears cache) or session-
        end ``reset_all`` (also clears). Direct ``delete_chunks``
        callers who wrap in a transaction with no upsert are an
        unsupported pattern; documenting the trade-off here so a
        future test asserting the *opposite* flags the design intent
        first rather than silently inverting."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        src = Path("/tmp/empty-in-tx.md")
        chunk = Chunk(
            content="only",
            metadata=ChunkMetadata(source_file=src, heading_hierarchy=("# H",), start_line=1),
            content_hash="hash-only",
            embedding=[0.1] * 1024,
        )
        await storage.upsert_chunks([chunk])
        await storage.set_ai_summary(src, "old prose", "sig", "en")

        async with storage.transaction():
            await storage.delete_chunks([chunk.id])

        # Stale but present — caller is expected to use
        # delete_by_source / reset_all for final cleanup.
        rec = await storage.get_ai_summary(src)
        assert rec is not None

    async def test_delete_chunks_handles_mixed_sources_atomically(self, components):
        """A single ``delete_chunks`` call spanning multiple sources
        clears summaries for the ones it fully empties and leaves the
        others alone. Pin the per-source check so a future "clear all
        affected summaries" shortcut can't slip through (that would be
        the same bug as before, just inverted)."""
        from memtomem.models import Chunk, ChunkMetadata

        storage = components.storage
        # source A: single chunk → will be fully emptied
        src_a = Path("/tmp/empty-me.md")
        chunk_a = Chunk(
            content="only A",
            metadata=ChunkMetadata(source_file=src_a, heading_hierarchy=("# A",), start_line=1),
            content_hash="hash-A",
            embedding=[0.1] * 1024,
        )
        # source B: two chunks, only one deleted → partial
        src_b = Path("/tmp/keep-me.md")
        chunks_b = [
            Chunk(
                content="B section 0",
                metadata=ChunkMetadata(source_file=src_b, heading_hierarchy=("# B",), start_line=1),
                content_hash="hash-B0",
                embedding=[0.1] * 1024,
            ),
            Chunk(
                content="B section 1",
                metadata=ChunkMetadata(
                    source_file=src_b, heading_hierarchy=("# B",), start_line=20
                ),
                content_hash="hash-B1",
                embedding=[0.1] * 1024,
            ),
        ]
        await storage.upsert_chunks([chunk_a, *chunks_b])
        await storage.set_ai_summary(src_a, "A prose.", "sig-A", "en")
        await storage.set_ai_summary(src_b, "B prose.", "sig-B", "en")

        # Mixed batch: A's only chunk + B's first chunk.
        await storage.delete_chunks([chunk_a.id, chunks_b[0].id])

        assert await storage.get_ai_summary(src_a) is None  # fully emptied
        assert await storage.get_ai_summary(src_b) is not None  # partial


class TestSetNamespaceMetaAtomicity:
    """``set_namespace_meta`` must be a single atomic upsert, not
    check-then-INSERT — two concurrent first-time registrations of the same
    namespace (e.g. ``mem_agent_register`` from two clients) raced across the
    read's await window and the loser died on the PK (#1574 item 4)."""

    @pytest.mark.asyncio
    async def test_lost_race_insert_does_not_raise(self, components, monkeypatch):
        """Simulate the raced loser: the row appears after this call's read
        window. Any read the implementation performs reports "missing" —
        exactly what the loser saw — and the call must still succeed."""
        from unittest.mock import AsyncMock

        storage = components.storage
        await storage.set_namespace_meta("agent-runtime:planner", description="winner")

        monkeypatch.setattr(storage._ns, "get_namespace_meta", AsyncMock(return_value=None))
        await storage.set_namespace_meta("agent-runtime:planner", description="loser")

        monkeypatch.undo()
        meta = await storage.get_namespace_meta("agent-runtime:planner")
        assert meta is not None
        assert meta["description"] == "loser"  # last-writer-wins, no PK error

    @pytest.mark.asyncio
    async def test_fresh_create_defaults_omitted_fields_to_empty(self, components):
        storage = components.storage
        await storage.set_namespace_meta("agent-runtime:coder", description="only desc")
        meta = await storage.get_namespace_meta("agent-runtime:coder")
        assert meta["description"] == "only desc"
        assert meta["color"] == ""

    @pytest.mark.asyncio
    async def test_partial_update_preserves_unset_fields(self, components):
        """None means "leave as is" — the upsert must not clobber an existing
        field the caller did not pass."""
        storage = components.storage
        await storage.set_namespace_meta("shared", description="keep me", color="blue")
        await storage.set_namespace_meta("shared", color="red")
        meta = await storage.get_namespace_meta("shared")
        assert meta["description"] == "keep me"
        assert meta["color"] == "red"


class TestDeleteByNamespaceMetadata:
    """``delete_by_namespace`` must remove the ``namespace_metadata`` row even
    when the namespace holds no chunks, otherwise a metadata-only namespace
    (registered via ``set_namespace_meta`` but never written to) stays listed
    by ``list_namespace_meta`` and is undeletable through this API — an
    undeletable tombstone for scratch-namespace GC callers (#1705)."""

    def _listed(self, metas):
        return {m["namespace"] for m in metas}

    @pytest.mark.asyncio
    async def test_deletes_metadata_only_namespace(self, components):
        """A namespace that exists only as metadata (zero chunks) is removed
        and returns 0 (the chunk count), not left behind."""
        storage = components.storage
        await storage.set_namespace_meta("board-run:abc", description="scratch")
        assert "board-run:abc" in self._listed(await storage.list_namespace_meta())

        deleted = await storage.delete_by_namespace("board-run:abc")

        assert deleted == 0
        assert "board-run:abc" not in self._listed(await storage.list_namespace_meta())
        assert await storage.get_namespace_meta("board-run:abc") is None

    @pytest.mark.asyncio
    async def test_nonexistent_namespace_is_noop(self, components):
        """Deleting a wholly unknown namespace stays a 0 no-op."""
        storage = components.storage
        assert await storage.delete_by_namespace("board-run:never") == 0

    @pytest.mark.asyncio
    async def test_deletes_chunks_and_metadata(self, components):
        """The chunks+metadata path is unchanged: chunks are gone, the
        metadata row is gone, and the return value is the chunk count."""
        storage = components.storage
        await storage.set_namespace_meta("work", description="real")
        await storage.upsert_chunks([make_chunk(content="c1", namespace="work", source="a.md")])

        deleted = await storage.delete_by_namespace("work")

        assert deleted == 1
        assert "work" not in self._listed(await storage.list_namespace_meta())
        assert await storage.get_namespace_meta("work") is None


class TestRecallByChunkIds:
    """``recall_chunks(chunk_ids=…)`` — the id-restricted recall path.

    Exists so a caller with explicit provenance can fetch exactly those
    rows *through* the recall path, keeping the always-on ADR-0011 scope
    fragment and the ``created_at DESC`` ordering that fetching them via
    ``get_chunks_batch`` would bypass.
    """

    @pytest.mark.asyncio
    async def test_returns_only_the_named_ids(self, components):
        storage = components.storage
        chunks = [make_chunk(content=f"c{i}", source=f"{i}.md") for i in range(4)]
        await storage.upsert_chunks(chunks)
        wanted = [chunks[0].id, chunks[2].id]

        got = await storage.recall_chunks(chunk_ids=wanted, limit=100)

        assert {c.id for c in got} == set(wanted)

    @pytest.mark.asyncio
    async def test_orders_newest_first(self, components):
        """The summary-link writer truncates with ``[:cap]`` and depends on
        this ordering, so an id-restricted recall must keep it."""
        storage = components.storage
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        chunks = [
            dataclasses.replace(
                make_chunk(content=f"c{i}", source=f"{i}.md"),
                created_at=base + timedelta(hours=i),
            )
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)

        got = await storage.recall_chunks(chunk_ids=[c.id for c in chunks], limit=100)

        assert [c.content for c in got] == ["c2", "c1", "c0"]

    @pytest.mark.asyncio
    async def test_empty_sequence_matches_nothing(self, components):
        """An empty id list means "these zero ids", NOT "no filter".

        Gating on truthiness here would silently widen a caller's
        filtered-down-to-nothing list into a full unfiltered scan.
        """
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="present")])

        assert await storage.recall_chunks(chunk_ids=[], limit=100) == []

    @pytest.mark.asyncio
    async def test_none_leaves_the_recall_unfiltered(self, components):
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="present")])

        got = await storage.recall_chunks(chunk_ids=None, limit=100)

        assert [c.content for c in got] == ["present"]

    @pytest.mark.asyncio
    async def test_unknown_ids_are_simply_absent(self, components):
        storage = components.storage
        chunk = make_chunk(content="real")
        await storage.upsert_chunks([chunk])

        got = await storage.recall_chunks(chunk_ids=[chunk.id, uuid.uuid4()], limit=100)

        assert [c.id for c in got] == [chunk.id]

    @pytest.mark.asyncio
    async def test_other_filters_still_apply(self, components):
        """The id set intersects with, rather than replaces, the other filters."""
        storage = components.storage
        a = make_chunk(content="in ns", namespace="work", source="a.md")
        b = make_chunk(content="other ns", namespace="play", source="b.md")
        await storage.upsert_chunks([a, b])

        got = await storage.recall_chunks(
            chunk_ids=[a.id, b.id],
            namespace_filter=NamespaceFilter(namespaces=("work",)),
            limit=100,
        )

        assert [c.id for c in got] == [a.id]

    @pytest.mark.asyncio
    async def test_large_id_set_does_not_hit_the_variable_limit(self, components):
        """>999 ids: the reason this binds one JSON array instead of an IN-list.

        An ``IN (?,?,…)`` of this width would blow SQLite's bound-variable
        limit, and ``placeholders(0)`` cannot express the empty case at all.
        """
        storage = components.storage
        chunks = [make_chunk(content=f"c{i}", source=f"{i}.md") for i in range(5)]
        await storage.upsert_chunks(chunks)
        padding = [uuid.uuid4() for _ in range(2000)]

        got = await storage.recall_chunks(chunk_ids=[c.id for c in chunks] + padding, limit=100)

        assert {c.id for c in got} == {c.id for c in chunks}


class TestSumChunkContentChars:
    @pytest.mark.asyncio
    async def test_agrees_with_the_hydrated_rows(self, components):
        storage = components.storage
        chunks = [
            make_chunk(content="a" * 10, source="a.md"),
            make_chunk(content="b" * 25, source="b.md"),
        ]
        await storage.upsert_chunks(chunks)
        ids = [c.id for c in chunks]

        count, chars = await storage.sum_chunk_content_chars(ids)
        hydrated = await storage.recall_chunks(chunk_ids=ids, limit=100)

        assert count == len(hydrated) == 2
        assert chars == sum(len(c.content) for c in hydrated)

    @pytest.mark.asyncio
    async def test_counts_characters_not_utf8_bytes(self, components):
        """The unit must match ``max_input_chars``, a Python character count.

        SQLite ``LENGTH()`` on TEXT counts characters; on a BLOB it would
        count bytes, and multibyte content would then over-report by ~3x.
        """
        storage = components.storage
        chunk = make_chunk(content="한글abc")
        await storage.upsert_chunks([chunk])

        _, chars = await storage.sum_chunk_content_chars([chunk.id])

        assert chars == 5

    @pytest.mark.asyncio
    async def test_empty_id_set_is_zero(self, components):
        storage = components.storage
        await storage.upsert_chunks([make_chunk(content="present")])

        assert await storage.sum_chunk_content_chars([]) == (0, 0)
