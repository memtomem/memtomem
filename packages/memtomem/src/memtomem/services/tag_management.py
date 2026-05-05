"""Tag-management service: rename / delete / merge tags across chunks.

Single source of truth for tag mutations. Web routes, MCP tools, and the
``mm tags`` CLI (PR3) all funnel here so the surface stays symmetric.

Concurrency: each entry point acquires ``storage._tag_write_lock`` for
the read-modify-write window so it can't interleave with
``auto_tag_storage`` running in the same process. Cross-process
serialization still falls back to SQLite's WAL file lock — concurrent
``mm web`` and ``mm`` CLI processes can still race on the same chunks
(known limitation, see #688 confirm thread).

``updated_at`` is bumped on every mutated row by the storage helpers
themselves; ``decay.py`` reads ``chunk.updated_at`` for age, so a tag
mutation does reset the decay timer for affected chunks. This is the
intentional v1 trade-off (option (a) in the #688 confirm thread): tag
fix-ups are treated as curation events.

Cache invalidation: when a ``SearchPipeline`` is passed in, this service
calls ``invalidate_cache()`` after a successful apply so the result TTL
cache (``search/pipeline.py``) cannot serve stale tag-filter responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

if TYPE_CHECKING:
    from memtomem.models import Chunk
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.base import StorageBackend


_DRY_RUN_SAMPLE_CAP = 10
_MERGE_CANDIDATE_SCAN_LIMIT = 10_000


@dataclass(frozen=True)
class TagOpSample:
    """One chunk preview returned in a dry-run response."""

    chunk_id: UUID
    source_file: str
    content_preview: str
    current_tags: tuple[str, ...]


@dataclass(frozen=True)
class TagOpResult:
    """Outcome of a rename / delete / merge call.

    ``affected_chunks`` is the count that *would* change for a dry run, or
    the count that *did* change for a real apply. ``samples`` is populated
    only when ``dry_run`` is true.
    """

    tag: str
    affected_chunks: int
    dry_run: bool
    samples: tuple[TagOpSample, ...] = field(default_factory=tuple)


def _preview(content: str, max_chars: int = 200) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "…"


def _chunk_to_sample(chunk: Chunk) -> TagOpSample:
    return TagOpSample(
        chunk_id=chunk.id,
        source_file=str(chunk.metadata.source_file),
        content_preview=_preview(chunk.content),
        current_tags=tuple(chunk.metadata.tags),
    )


async def _samples_for_tag(
    storage: StorageBackend,
    tag: str,
    cap: int = _DRY_RUN_SAMPLE_CAP,
) -> tuple[TagOpSample, ...]:
    chunks = await storage.list_chunks_by_tag(tag, limit=cap)
    return tuple(_chunk_to_sample(c) for c in chunks)


def _invalidate(search_pipeline: SearchPipeline | None) -> None:
    if search_pipeline is not None:
        search_pipeline.invalidate_cache()


async def rename_tag(
    storage: StorageBackend,
    old: str,
    new: str,
    *,
    dry_run: bool = False,
    search_pipeline: SearchPipeline | None = None,
) -> TagOpResult:
    """Rename ``old`` to ``new`` across every chunk that carries it.

    Idempotent: if a chunk already has ``new`` alongside ``old``, the result
    is a single ``new`` tag. ``dry_run=True`` returns counts + sample chunks
    without writing.
    """
    if not old or not new:
        raise ValueError("rename_tag requires non-empty old and new tag names")

    async with storage._tag_write_lock:
        if dry_run:
            count = await storage.count_chunks_by_tag(old)
            samples = await _samples_for_tag(storage, old) if count else ()
            return TagOpResult(tag=new, affected_chunks=count, dry_run=True, samples=samples)

        affected = await storage.rename_tag(old, new)
        if affected:
            _invalidate(search_pipeline)
        return TagOpResult(tag=new, affected_chunks=affected, dry_run=False)


async def delete_tag(
    storage: StorageBackend,
    tag: str,
    *,
    dry_run: bool = False,
    search_pipeline: SearchPipeline | None = None,
) -> TagOpResult:
    """Drop ``tag`` from every chunk that carries it.

    Chunks that end up tag-less stay indexed; we don't synthesize new tags
    to fill the gap. ``dry_run=True`` returns counts + sample chunks
    without writing.
    """
    if not tag:
        raise ValueError("delete_tag requires a non-empty tag name")

    async with storage._tag_write_lock:
        if dry_run:
            count = await storage.count_chunks_by_tag(tag)
            samples = await _samples_for_tag(storage, tag) if count else ()
            return TagOpResult(tag=tag, affected_chunks=count, dry_run=True, samples=samples)

        affected = await storage.delete_tag(tag)
        if affected:
            _invalidate(search_pipeline)
        return TagOpResult(tag=tag, affected_chunks=affected, dry_run=False)


async def merge_tags(
    storage: StorageBackend,
    sources: Sequence[str],
    target: str,
    *,
    dry_run: bool = False,
    search_pipeline: SearchPipeline | None = None,
) -> TagOpResult:
    """Replace every tag in ``sources`` with ``target`` across all chunks.

    The resulting per-chunk tag list is deduplicated. ``dry_run=True``
    returns counts + sample chunks without writing.
    """
    if not target:
        raise ValueError("merge_tags requires a non-empty target tag name")
    source_set = {s for s in sources if s and s != target}
    if not source_set:
        # Nothing to do — empty sources, or sources collapsed to just the target.
        return TagOpResult(tag=target, affected_chunks=0, dry_run=dry_run)

    async with storage._tag_write_lock:
        if dry_run:
            # Candidate count = chunks that hold *any* source tag (excluding
            # target). The actual ``merge_tags`` storage helper will mutate
            # exactly this set since every such chunk's tag list changes
            # when a source is rewritten to target (target is not in
            # source_set, so the rewrite always produces a new tags tuple).
            candidates: dict[UUID, Chunk] = {}
            for s in source_set:
                chunks = await storage.list_chunks_by_tag(s, limit=_MERGE_CANDIDATE_SCAN_LIMIT)
                for c in chunks:
                    candidates.setdefault(c.id, c)
            samples = tuple(
                _chunk_to_sample(c) for c in list(candidates.values())[:_DRY_RUN_SAMPLE_CAP]
            )
            return TagOpResult(
                tag=target,
                affected_chunks=len(candidates),
                dry_run=True,
                samples=samples,
            )

        affected = await storage.merge_tags(list(source_set), target)
        if affected:
            _invalidate(search_pipeline)
        return TagOpResult(tag=target, affected_chunks=affected, dry_run=False)
