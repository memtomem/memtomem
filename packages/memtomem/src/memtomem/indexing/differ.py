"""Diff-based incremental indexing: compare old vs new chunks at chunk level."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from memtomem.models import Chunk


@dataclass
class DiffResult:
    to_upsert: list[Chunk]  # new or changed chunks (need embedding)
    to_delete: list[UUID]  # stale chunk IDs to remove
    unchanged: list[Chunk]  # unchanged chunks (skip embedding)


def compute_diff(
    existing_hashes: Mapping[
        str, str | tuple[str, tuple[str, ...]]
    ],  # chunk_id -> hash or (hash, hierarchy)
    new_chunks: list[Chunk],
) -> DiffResult:
    """Compare existing chunk hashes against newly computed chunks.

    Matching is done by content_hash (not ID), so re-ordering sections
    is correctly recognized as unchanged content.

    - New chunk hash NOT in existing hashes → upsert (needs embedding)
    - Hash match with a changed heading hierarchy → upsert with reused ID
    - Existing ID that no new chunk reused → delete
    - Hash and heading hierarchy match → unchanged, reuse existing ID

    Duplicate content_hash values are handled safely: each existing ID is
    reused at most once, preventing ID collisions when multiple chunks share
    identical content.
    """
    # Build hash → [id, ...] mapping to handle duplicate hashes safely
    existing_ids_by_hash: dict[str, list[tuple[str, tuple[str, ...] | None]]] = {}
    for cid, state in existing_hashes.items():
        if isinstance(state, tuple):
            chash, hierarchy = state
        else:
            # Backward-compatible input for pure differ callers that only
            # know content hashes. No hierarchy means hash equality is enough.
            chash, hierarchy = state, None
        existing_ids_by_hash.setdefault(chash, []).append((cid, hierarchy))

    to_upsert: list[Chunk] = []
    unchanged: list[Chunk] = []
    used_ids: set[str] = set()

    # Reserve exact hierarchy matches first across the whole file. This avoids
    # an earlier renamed duplicate body consuming an ID that a later unchanged
    # duplicate should keep.
    assignments: list[tuple[str, tuple[str, ...] | None] | None] = [None] * len(new_chunks)
    for index, chunk in enumerate(new_chunks):
        candidates = existing_ids_by_hash.get(chunk.content_hash, [])
        new_hierarchy = chunk.metadata.heading_hierarchy
        exact = next(
            (
                candidate
                for candidate in candidates
                if candidate[0] not in used_ids
                and (candidate[1] is None or candidate[1] == new_hierarchy)
            ),
            None,
        )
        if exact is not None:
            assignments[index] = exact
            used_ids.add(exact[0])

    for index, chunk in enumerate(new_chunks):
        new_hierarchy = chunk.metadata.heading_hierarchy
        reuse = assignments[index]
        if reuse is None:
            candidates = existing_ids_by_hash.get(chunk.content_hash, [])
            reuse = next(
                (candidate for candidate in candidates if candidate[0] not in used_ids), None
            )
        if reuse is not None:
            reuse_id, existing_hierarchy = reuse
            used_ids.add(reuse_id)
            chunk.id = UUID(reuse_id)
            if existing_hierarchy is not None and existing_hierarchy != new_hierarchy:
                to_upsert.append(chunk)
            else:
                unchanged.append(chunk)
        else:
            to_upsert.append(chunk)

    # Existing chunks that no new chunk reused → stale. Keyed on the id, not on
    # whether the hash survives somewhere: when a file collapses N byte-identical
    # chunks into fewer, the hash is still present but the surplus ids are not
    # reused and nothing upserts over them, so a hash-keyed test left them behind
    # as orphan rows that stayed searchable under a heading the file no longer
    # has — and that ``--force`` could not clear either, since force only promotes
    # ``unchanged`` into ``to_upsert`` and reuses this same list (#2123).
    # ``used_ids`` holds exactly the ids handed to ``unchanged`` or to a reusing
    # ``to_upsert`` chunk, so the deletions stay disjoint from both.
    to_delete = [UUID(cid) for cid in existing_hashes if cid not in used_ids]

    return DiffResult(to_upsert=to_upsert, to_delete=to_delete, unchanged=unchanged)
