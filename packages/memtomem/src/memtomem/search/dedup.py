"""Deduplication scanner: exact (content_hash) and near (dense vector) duplicate detection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.models import Chunk

if TYPE_CHECKING:
    from memtomem.storage.base import StorageBackend


@dataclass(frozen=True)
class DedupCandidate:
    chunk_a: Chunk  # older / keep-preferred chunk
    chunk_b: Chunk  # duplicate candidate
    # 1.0 for exact duplicates; otherwise the storage dense score 1/(1+L2),
    # which is monotonic in closeness but not a cosine similarity.
    score: float
    exact: bool  # True when content_hash matches


@dataclass(frozen=True)
class DedupCoverage:
    """What one scan actually looked at. Describes the selected pool only.

    A scan reads at most ``max_scan`` chunks, so an empty candidate list means
    "nothing in this pool", never "nothing in the store"; near-phase neighbours
    can still come from outside the pool.
    """

    pool: int  # chunks selected for the scan (the exact phase's input)
    near_search_enabled: bool  # False for a BM25-only store
    probed: int  # pool chunks searched with their stored vector
    # Pool chunks the vector lookup returned nothing for: not vectorised yet, or
    # a stored vector that could not be decoded. They are not probed.
    without_vector: int


class DedupScanner:
    def __init__(self, storage: StorageBackend) -> None:
        # Storage only. The near phase searches with vectors already stored for
        # each chunk, so a scan never reaches an embedder and has no model
        # generation to pin across an embedding swap (#2180/#2199 no longer
        # apply); the storage object itself is shared across such swaps.
        self._storage = storage

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

        See :meth:`scan_with_coverage`; this drops the coverage report.
        """
        candidates, _ = await self.scan_with_coverage(threshold, limit, max_scan)
        return candidates

    async def scan_with_coverage(
        self,
        threshold: float = 0.92,
        limit: int = 100,
        max_scan: int = 500,
    ) -> tuple[list[DedupCandidate], DedupCoverage]:
        """Return duplicate candidate pairs and what the scan covered.

        Phase 1 groups up to *max_scan* chunks by content_hash.
        Phase 2 searches each of those chunks with its stored vector. Chunks
        without one are counted in the coverage, not probed: re-embedding them
        here would need an embedder, and re-embedding bare ``content`` did not
        reproduce the stored ``retrieval_content`` vectors (it found candidates
        for 2 of 62 chunks that stored vectors found on a real E5 store).
        Results are sorted: exact duplicates first, then by score descending.
        """
        if max_scan < 1:
            raise ValueError(f"max_scan must be at least 1, got {max_scan}")
        all_chunks = await self._get_all_chunks(max_scan)

        seen: set[frozenset] = set()
        candidates: list[DedupCandidate] = []

        # Phase 1: exact duplicates
        candidates.extend(self._find_exact_duplicates(all_chunks, seen))

        # Phase 2: near duplicates over the same pool
        near, coverage = await self._find_near_duplicates(all_chunks, threshold, seen)
        candidates.extend(near)

        # Exact first, then by score descending
        candidates.sort(key=lambda c: (not c.exact, -c.score))

        return candidates[:limit], coverage

    async def merge(self, keep_id: UUID, delete_ids: list[UUID], dry_run: bool = False) -> int:
        """Merge duplicate chunks: keep *keep_id*, delete *delete_ids*.

        The tags of all deleted chunks are unioned into the kept chunk.
        Returns the number of chunks that were (or would be) deleted.

        When ``dry_run`` is True, no writes are performed: neither the
        tag-merge upsert on ``keep_id`` nor the deletion of ``delete_ids``.
        The return value is the count of ``delete_ids`` that resolve to
        existing chunks — the same value the non-dry path's
        ``storage.delete_chunks`` would return.
        """
        if not delete_ids:
            return 0

        # Batch-fetch all chunks in a single query
        all_ids = [keep_id, *delete_ids]
        chunks_map = await self._storage.get_chunks_batch(all_ids)

        keep_chunk = chunks_map.get(keep_id)
        if keep_chunk is None:
            return 0
        # A held row is retained for recovery, not a candidate to keep or
        # merge away. Recheck at apply time because a scan result may be stale.
        for chunk in chunks_map.values():
            if await self._storage.is_source_held(chunk.metadata.source_file):
                raise ValueError("Cannot merge chunks from a held source")

        # Collect tags from chunks being deleted
        merged_tags: set[str] = set(keep_chunk.metadata.tags)
        for del_id in delete_ids:
            del_chunk = chunks_map.get(del_id)
            if del_chunk is not None:
                merged_tags.update(del_chunk.metadata.tags)

        if dry_run:
            return sum(1 for d in delete_ids if d in chunks_map)

        # Update keep chunk if tags changed
        if merged_tags != set(keep_chunk.metadata.tags):
            # ``replace`` rather than a field-by-field rebuild: this merge only
            # changes tags, and enumerating the fields silently dropped every
            # one added since it was written (overlap, validity window, scope,
            # project_root — and ``origin``, whose loss would leave a
            # consolidation summary unowned and fail its next regeneration
            # closed, #2161). A new metadata field must not need an edit here.
            new_meta = replace(keep_chunk.metadata, tags=tuple(sorted(merged_tags)))
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
            if await self._storage.is_source_held(source):
                continue
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
    ) -> tuple[list[DedupCandidate], DedupCoverage]:
        candidates: list[DedupCandidate] = []
        enabled = getattr(self._storage, "dense_enabled", True) is not False
        if not chunks or not enabled:
            return candidates, DedupCoverage(
                pool=len(chunks), near_search_enabled=enabled, probed=0, without_vector=0
            )

        # The vectors the chunks are stored with. Ingress embeds
        # ``retrieval_content``, so a fresh embedding of ``content`` would land
        # somewhere else and miss most real neighbours.
        stored = await self._storage.get_embeddings_for_chunks([str(c.id) for c in chunks])
        probed = without_vector = 0

        for chunk in chunks:
            embedding = stored.get(str(chunk.id))
            if embedding is None:
                without_vector += 1
                continue
            probed += 1
            # ADR-0011 PR-D round 11: per-chunk project context. Each
            # chunk only finds duplicates in its OWN project tier —
            # passing ``chunk.metadata.project_root`` (None for
            # user-tier rows) honours the always-on storage scope
            # filter without cross-project leakage. Without this,
            # the dedup pass would silently drop project-tier
            # candidates whenever the scanner is invoked outside a
            # cwd-resolved project context (e.g. from the web admin
            # endpoint).
            results = await self._storage.dense_search(
                embedding,
                top_k=6,
                project_context_root=chunk.metadata.project_root,
            )
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
        return candidates, DedupCoverage(
            pool=len(chunks),
            near_search_enabled=True,
            probed=probed,
            without_vector=without_vector,
        )
