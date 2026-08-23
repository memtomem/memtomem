"""Diff-based incremental indexing: compare old vs new chunks at chunk level."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from memtomem.models import Chunk


@dataclass
class DiffResult:
    to_upsert: list[Chunk]  # new or changed chunks (need embedding)
    to_delete: list[UUID]  # stale chunk IDs to remove
    unchanged: list[Chunk]  # unchanged chunks (skip embedding)
    # Chunks whose text is byte-identical but whose retrieval metadata moved
    # (tags). They keep their vector and their id; only the metadata columns
    # are rewritten. Empty unless the caller supplies stored tags — see
    # ``compute_diff``. Never overlaps ``unchanged`` or ``to_upsert``.
    metadata_only: list[Chunk] = field(default_factory=list)


def compute_diff(
    existing_hashes: Mapping[
        str,
        str | tuple[str, tuple[str, ...]] | tuple[str, tuple[str, ...], tuple[str, ...]],
    ],  # chunk_id -> hash, (hash, hierarchy), or (hash, hierarchy, tags)
    new_chunks: list[Chunk],
) -> DiffResult:
    """Compare existing chunk hashes against newly computed chunks.

    Matching is done by content_hash (not ID), so re-ordering sections
    is correctly recognized as unchanged content.

    - New chunk hash NOT in existing hashes → upsert (needs embedding)
    - Hash match with a changed heading hierarchy → upsert with reused ID
    - Existing ID that no new chunk reused → delete
    - Hash, hierarchy and tags all match → unchanged, reuse existing ID
    - Hash and hierarchy match but the stored tags differ → metadata_only

    A caller that supplies the stored tags (the three-element state) opts into
    the ``metadata_only`` bucket: a section's ``> tags: [...]`` blockquote is
    stripped from the chunk text and promoted to ``metadata.tags``
    (``chunking/markdown.py``), so editing it moves neither the content hash nor
    the heading hierarchy, and the row kept tags the file no longer carried
    while ``mem_search(tag_filter=...)`` read them (#2124). Two- and
    one-element states leave the bucket empty and behave exactly as before.

    Duplicate content_hash values are handled safely: each existing ID is
    reused at most once, preventing ID collisions when multiple chunks share
    identical content.
    """
    # Build hash → [id, ...] mapping to handle duplicate hashes safely
    existing_ids_by_hash: dict[str, list[tuple[str, tuple[str, ...] | None]]] = {}
    # ``None`` marks "this caller did not tell us what the row's tags are", which
    # is not the same as "the row has no tags" — the former must never be read as
    # tag drift.
    existing_tags: dict[str, tuple[str, ...] | None] = {}
    for cid, state in existing_hashes.items():
        if isinstance(state, tuple):
            if len(state) == 3:
                chash, hierarchy, stored_tags = state
            else:
                (chash, hierarchy), stored_tags = state, None
        else:
            # Backward-compatible input for pure differ callers that only
            # know content hashes. No hierarchy means hash equality is enough.
            chash, hierarchy, stored_tags = state, None, None
        existing_ids_by_hash.setdefault(chash, []).append((cid, hierarchy))
        existing_tags[cid] = stored_tags

    to_upsert: list[Chunk] = []
    unchanged: list[Chunk] = []
    metadata_only: list[Chunk] = []
    used_ids: set[str] = set()

    # Reserve exact matches first across the whole file. This avoids an earlier
    # renamed duplicate body consuming an ID that a later unchanged duplicate
    # should keep. Two passes, most specific first: hierarchy *and* tags, then
    # hierarchy alone. Without the tag pass, two byte-identical sections under
    # the same heading that differ only by tags could swap ids when reordered —
    # taking each other's access counts, links and line positions with them,
    # even though an exactly-matching assignment existed.
    assignments: list[tuple[str, tuple[str, ...] | None] | None] = [None] * len(new_chunks)

    def _reserve(*, require_tag_match: bool, allow_wildcard: bool) -> None:
        for index, chunk in enumerate(new_chunks):
            if assignments[index] is not None:
                continue
            candidates = existing_ids_by_hash.get(chunk.content_hash, [])
            new_hierarchy = chunk.metadata.heading_hierarchy
            exact = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate[0] not in used_ids
                    and (
                        candidate[1] == new_hierarchy
                        if not allow_wildcard
                        else (candidate[1] is None or candidate[1] == new_hierarchy)
                    )
                    and (
                        not require_tag_match
                        or (
                            existing_tags[candidate[0]] is not None
                            and set(existing_tags[candidate[0]] or ()) == set(chunk.metadata.tags)
                        )
                    )
                ),
                None,
            )
            if exact is not None:
                assignments[index] = exact
                used_ids.add(exact[0])

    # Wildcards (``hierarchy is None`` — the bare-hash input a pure differ caller
    # supplies) match anything, so they go last: spending one on a chunk that had
    # an exact-hierarchy id available would strand that id and force a needless
    # re-embed. Only a state map that mixes input shapes can hit this, which no
    # shipped caller does today — both build one shape for the whole file — but
    # the ordering is what makes that a property rather than a coincidence.
    _reserve(require_tag_match=True, allow_wildcard=False)
    _reserve(require_tag_match=False, allow_wildcard=False)
    _reserve(require_tag_match=False, allow_wildcard=True)

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
                stored_tags = existing_tags.get(reuse_id)
                # Set comparison, not tuple: ``tag_filter`` is set membership
                # (ADR-0002), and a row stored in a different order than the
                # chunker's sorted tuple is the same filing. Comparing order
                # would rewrite such rows on every single re-index.
                if stored_tags is not None and set(stored_tags) != set(chunk.metadata.tags):
                    # Same bytes, same headings, different retrieval metadata:
                    # the row needs a metadata write, not an embedding.
                    metadata_only.append(chunk)
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

    return DiffResult(
        to_upsert=to_upsert,
        to_delete=to_delete,
        unchanged=unchanged,
        metadata_only=metadata_only,
    )
