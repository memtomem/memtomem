"""Tag-management service: rename / delete / merge tags across chunks.

Single source of truth for tag mutations. Web routes (PR1), MCP tools
(PR1 if migration is approved), and the ``mm tags`` CLI (PR3) all funnel
here so the surface stays symmetric.

Concurrency: each entry point acquires ``storage._tag_write_lock`` for
the read-modify-write window so it can't interleave with
``auto_tag_storage`` running in the same process. Cross-process
serialization still falls back to SQLite's WAL file lock.

Open policy questions (resolved in #688 confirm thread before this
service is wired into Web/MCP routes):

- ``updated_at`` policy on tag-only mutations. ``decay.py`` and
  ``expire_chunks`` both read ``chunk.updated_at``; bumping it would
  reset decay scoring + expiration timers for every affected chunk.
  The service exposes ``bump_updated_at`` as an explicit parameter so
  the policy choice lives at the route layer, not buried here.
- Whether the existing MCP ``tag_management`` tools migrate to this
  service in PR1 or as a follow-up (PR1.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

if TYPE_CHECKING:
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.base import StorageBackend


_DRY_RUN_SAMPLE_CAP = 10


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


async def _collect_samples(
    storage: StorageBackend,
    tag: str,
    cap: int = _DRY_RUN_SAMPLE_CAP,
) -> tuple[TagOpSample, ...]:
    chunks = await storage.list_chunks_by_tag(tag, limit=cap)
    return tuple(
        TagOpSample(
            chunk_id=c.id,
            source_file=str(c.metadata.source_file),
            content_preview=_preview(c.content),
            current_tags=tuple(c.metadata.tags),
        )
        for c in chunks
    )


async def rename_tag(
    storage: StorageBackend,
    old: str,
    new: str,
    *,
    dry_run: bool = False,
    bump_updated_at: bool = True,
    search_pipeline: SearchPipeline | None = None,
) -> TagOpResult:
    """Rename ``old`` to ``new`` across every chunk that carries it.

    Idempotent: if a chunk already has ``new`` alongside ``old``, the result
    is a single ``new`` tag. ``dry_run=True`` returns counts + sample chunks
    without writing.
    """
    raise NotImplementedError(
        "tag_management.rename_tag is a PR1 placeholder; wiring blocked on "
        "#688 confirm (updated_at policy + MCP migration scope)."
    )


async def delete_tag(
    storage: StorageBackend,
    tag: str,
    *,
    dry_run: bool = False,
    bump_updated_at: bool = True,
    search_pipeline: SearchPipeline | None = None,
) -> TagOpResult:
    """Drop ``tag`` from every chunk that carries it.

    Chunks that end up tag-less stay indexed; we don't synthesize new tags
    to fill the gap. ``dry_run=True`` returns counts + sample chunks
    without writing.
    """
    raise NotImplementedError(
        "tag_management.delete_tag is a PR1 placeholder; wiring blocked on "
        "#688 confirm (updated_at policy + MCP migration scope)."
    )


async def merge_tags(
    storage: StorageBackend,
    sources: Sequence[str],
    target: str,
    *,
    dry_run: bool = False,
    bump_updated_at: bool = True,
    search_pipeline: SearchPipeline | None = None,
) -> TagOpResult:
    """Replace every tag in ``sources`` with ``target`` across all chunks.

    The resulting per-chunk tag list is deduplicated. ``dry_run=True``
    returns counts + sample chunks without writing.
    """
    raise NotImplementedError(
        "tag_management.merge_tags is a PR1 placeholder; wiring blocked on "
        "#688 confirm (updated_at policy + MCP migration scope)."
    )
