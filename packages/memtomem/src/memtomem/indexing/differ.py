"""Diff-based incremental indexing: compare old vs new chunks at chunk level."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from memtomem.models import Chunk


@dataclass
class DiffResult:
    to_upsert: list[Chunk]  # new or changed chunks (need embedding)
    to_delete: list[UUID]  # stale chunk IDs to remove
    unchanged: list[Chunk]  # unchanged chunks (skip embedding)


def compute_diff(
    existing_hashes: dict[str, str],  # chunk_id -> content_hash
    new_chunks: list[Chunk],
) -> DiffResult:
    """Compare existing chunk hashes against newly computed chunks.

    Matching is done by content_hash (not ID), so re-ordering sections
    is correctly recognized as unchanged content.

    - New chunk hash NOT in existing hashes → upsert (needs embedding)
    - Existing ID whose hash doesn't appear in new chunks → delete
    - New chunk hash already in existing → unchanged, reuse existing ID

    Duplicate content_hash values are handled safely: each existing ID is
    reused at most once, preventing ID collisions when multiple chunks share
    identical content.
    """
    # Build hash → [id, ...] mapping to handle duplicate hashes safely
    existing_ids_by_hash: dict[str, list[str]] = {}
    for cid, chash in existing_hashes.items():
        existing_ids_by_hash.setdefault(chash, []).append(cid)

    to_upsert: list[Chunk] = []
    unchanged: list[Chunk] = []
    new_hash_set: set[str] = set()
    used_ids: set[str] = set()

    for chunk in new_chunks:
        new_hash_set.add(chunk.content_hash)
        ids_for_hash = existing_ids_by_hash.get(chunk.content_hash, [])
        # Find the first unused existing ID for this hash
        reuse_id = next((i for i in ids_for_hash if i not in used_ids), None)
        if reuse_id is not None:
            used_ids.add(reuse_id)
            chunk.id = UUID(reuse_id)
            unchanged.append(chunk)
        else:
            to_upsert.append(chunk)

    # Existing chunks whose hashes are no longer present in any new chunk → stale
    to_delete = [UUID(cid) for cid, chash in existing_hashes.items() if chash not in new_hash_set]

    return DiffResult(to_upsert=to_upsert, to_delete=to_delete, unchanged=unchanged)
