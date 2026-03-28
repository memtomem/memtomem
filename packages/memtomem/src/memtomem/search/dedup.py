"""Deduplication scanner: exact (content_hash) and near (dense vector) duplicate detection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.models import Chunk, ChunkMetadata

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.base import StorageBackend


@dataclass(frozen=True)
class DedupCandidate:
    chunk_a: Chunk  # older / keep-preferred chunk
    chunk_b: Chunk  # duplicate candidate
    score: float  # cosine similarity (1.0 = identical content)
    exact: bool  # True when content_hash matches


class DedupScanner:
    def __init__(self, storage: StorageBackend, embedder: EmbeddingProvider) -> None:
        self._storage = storage
        self._embedder = embedder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        threshold: float = 0.92,
        limit: int = 100,
        max_scan: int = 500,
    ) -> list[DedupCandidate]:
        """Return duplicate candidate pairs (dry-run, no mutations).

        Phase 1 scans all chunks for exact content_hash matches.
        Phase 2 re-embeds up to *max_scan* chunks and queries dense_search.
        Results are sorted: exact duplicates first, then by score descending.
        """
        all_chunks = await self._get_all_chunks(max_scan)

        seen: set[frozenset] = set()
        candidates: list[DedupCandidate] = []

        # Phase 1: exact duplicates
        candidates.extend(self._find_exact_duplicates(all_chunks, seen))

        # Phase 2: near duplicates (limited to max_scan chunks)
        candidates.extend(await self._find_near_duplicates(all_chunks[:max_scan], threshold, seen))

        # Exact first, then by score descending
        candidates.sort(key=lambda c: (not c.exact, -c.score))

        return candidates[:limit]

    async def merge(self, keep_id: UUID, delete_ids: list[UUID]) -> int:
        """Merge duplicate chunks: keep *keep_id*, delete *delete_ids*.

        The tags of all deleted chunks are unioned into the kept chunk.
        Returns the number of chunks actually deleted.
        """
        if not delete_ids:
            return 0

        # Batch-fetch all chunks in a single query
        all_ids = [keep_id, *delete_ids]
        chunks_map = await self._storage.get_chunks_batch(all_ids)

        keep_chunk = chunks_map.get(keep_id)
        if keep_chunk is None:
            return 0

        # Collect tags from chunks being deleted
        merged_tags: set[str] = set(keep_chunk.metadata.tags)
        for del_id in delete_ids:
            del_chunk = chunks_map.get(del_id)
            if del_chunk is not None:
                merged_tags.update(del_chunk.metadata.tags)

        # Update keep chunk if tags changed
        if merged_tags != set(keep_chunk.metadata.tags):
            new_meta = ChunkMetadata(
                source_file=keep_chunk.metadata.source_file,
                heading_hierarchy=keep_chunk.metadata.heading_hierarchy,
                chunk_type=keep_chunk.metadata.chunk_type,
                start_line=keep_chunk.metadata.start_line,
                end_line=keep_chunk.metadata.end_line,
                language=keep_chunk.metadata.language,
                tags=tuple(sorted(merged_tags)),
                namespace=keep_chunk.metadata.namespace,
            )
            updated = Chunk(
                content=keep_chunk.content,
                metadata=new_meta,
                id=keep_chunk.id,
                content_hash=keep_chunk.content_hash,
                embedding=keep_chunk.embedding,
                created_at=keep_chunk.created_at,
                updated_at=datetime.now(timezone.utc),
            )
            await self._storage.upsert_chunks([updated])

        return await self._storage.delete_chunks(delete_ids)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_all_chunks(self, max_count: int) -> list[Chunk]:
        """Fetch up to *max_count* chunks from all source files."""
        source_files = await self._storage.get_all_source_files()
        chunks: list[Chunk] = []
        for source in source_files:
            file_chunks = await self._storage.list_chunks_by_source(source, limit=max_count)
            chunks.extend(file_chunks)
            if len(chunks) >= max_count:
                break
        return chunks[:max_count]

    def _find_exact_duplicates(
        self, chunks: list[Chunk], seen: set[frozenset]
    ) -> list[DedupCandidate]:
        hash_groups: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            hash_groups[chunk.content_hash].append(chunk)

        candidates: list[DedupCandidate] = []
        for group in hash_groups.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda c: c.created_at)  # oldest = keep
            keep = group[0]
            for dup in group[1:]:
                pair: frozenset = frozenset([keep.id, dup.id])
                if pair in seen:
                    continue
                seen.add(pair)
                candidates.append(DedupCandidate(chunk_a=keep, chunk_b=dup, score=1.0, exact=True))
        return candidates

    async def _find_near_duplicates(
        self,
        chunks: list[Chunk],
        threshold: float,
        seen: set[frozenset],
    ) -> list[DedupCandidate]:
        candidates: list[DedupCandidate] = []
        if not chunks:
            return candidates

        # Batch embed all chunks at once (N calls -> 1 call)
        texts = [c.content for c in chunks]
        embeddings = await self._embedder.embed_texts(texts)

        for chunk, embedding in zip(chunks, embeddings):
            results = await self._storage.dense_search(embedding, top_k=6)
            for r in results:
                if r.chunk.id == chunk.id:
                    continue
                if r.score < threshold:
                    continue
                pair: frozenset = frozenset([chunk.id, r.chunk.id])
                if pair in seen:
                    continue
                seen.add(pair)
                # Older chunk becomes chunk_a (keep candidate)
                if chunk.created_at <= r.chunk.created_at:
                    chunk_a, chunk_b = chunk, r.chunk
                else:
                    chunk_a, chunk_b = r.chunk, chunk
                candidates.append(
                    DedupCandidate(chunk_a=chunk_a, chunk_b=chunk_b, score=r.score, exact=False)
                )
        return candidates
