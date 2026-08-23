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
    # (tags, validity window). They keep their vector and their id; only the
    # metadata columns are rewritten. Empty unless the caller supplies that
    # metadata — see ``compute_diff``. Never overlaps ``unchanged``/``to_upsert``.
    metadata_only: list[Chunk] = field(default_factory=list)


# One chunk's stored state, in any of the shapes a caller can supply:
#
#   ``hash``                                       — hash only
#   ``(hash, hierarchy)``                          — + heading identity
#   ``(hash, hierarchy, tags)``                    — + tags
#   ``(hash, hierarchy, tags, valid_from, valid_to)`` — + validity window
#
# Each step adds a field the differ may compare. A shape that stops short is not
# claiming the omitted fields are empty; it is saying nothing about them, and the
# differ must not read silence as drift. Any other width is rejected rather than
# truncated — see :func:`_split_state`.
ChunkState = (
    str
    | tuple[str, tuple[str, ...]]
    | tuple[str, tuple[str, ...], tuple[str, ...]]
    | tuple[str, tuple[str, ...], tuple[str, ...], int | None, int | None]
)


@dataclass(frozen=True)
class _RetrievalMetadata:
    """The columns a search reads that a chunk's own text cannot speak for.

    Both are stamped onto the chunk from outside its text — tags from a section
    blockquote that is stripped after promotion, the validity window from
    file-level frontmatter — so a content hash says nothing about either, and
    editing one used to be invisible to the diff.

    ``None`` means "the caller did not say", which is never drift; a field it
    did supply is compared. Tags compare as a set: ``tag_filter`` is set
    membership (ADR-0002), so a row stored in another order is the same filing
    and must not be rewritten on every re-index.
    """

    tags: tuple[str, ...] | None = None
    validity: tuple[int | None, int | None] | None = None

    def differs_from(self, chunk: Chunk) -> bool:
        if self.tags is not None and set(self.tags) != set(chunk.metadata.tags):
            return True
        if self.validity is not None and self.validity != (
            chunk.metadata.valid_from_unix,
            chunk.metadata.valid_to_unix,
        ):
            return True
        return False

    def matches(self, chunk: Chunk) -> bool:
        """Whether every *supplied* field agrees — the strictest reuse test."""
        return self.tags is not None and not self.differs_from(chunk)


def _split_state(state: ChunkState) -> tuple[str, tuple[str, ...] | None, _RetrievalMetadata]:
    """Unpack one stored state into (hash, hierarchy, retrieval metadata).

    Widths are matched exactly. Truncating an unrecognised one would silently
    drop whatever a caller had just started supplying — the failure mode this
    whole family of bugs is made of (#2124, #2140): a field a search reads,
    invisible to the diff, so the row keeps the stale value. A width nobody
    knows how to read is a programming error, not a state to guess at.
    """
    if isinstance(state, str):
        # Backward-compatible input for pure differ callers that only
        # know content hashes. No hierarchy means hash equality is enough.
        return state, None, _RetrievalMetadata()
    if not isinstance(state, tuple):
        # A non-string scalar would be compared against real hashes, match
        # nothing, and take every existing id down the delete path — a silent
        # data loss dressed up as a diff. Reject the type instead.
        raise ValueError(
            f"unsupported chunk state type {type(state).__name__}; expected a hash "
            "string or a 2-, 3- or 5-element tuple (see ChunkState)"
        )
    if len(state) == 5:
        chash, hierarchy, tags, valid_from, valid_to = state
        return chash, hierarchy, _RetrievalMetadata(tags=tags, validity=(valid_from, valid_to))
    if len(state) == 3:
        chash, hierarchy, tags = state
        return chash, hierarchy, _RetrievalMetadata(tags=tags)
    if len(state) == 2:
        chash, hierarchy = state
        return chash, hierarchy, _RetrievalMetadata()
    raise ValueError(
        f"unsupported chunk state width {len(state)}; expected a bare hash or a "
        "2-, 3- or 5-element tuple (see ChunkState)"
    )


def compute_diff(
    existing_hashes: Mapping[str, ChunkState],
    new_chunks: list[Chunk],
) -> DiffResult:
    """Compare existing chunk hashes against newly computed chunks.

    Matching is done by content_hash (not ID), so re-ordering sections
    is correctly recognized as unchanged content.

    - New chunk hash NOT in existing hashes → upsert (needs embedding)
    - Hash match with a changed heading hierarchy → upsert with reused ID
    - Existing ID that no new chunk reused → delete
    - Hash, hierarchy and retrieval metadata all match → unchanged, reuse ID
    - Hash and hierarchy match, retrieval metadata differs → metadata_only

    A caller that supplies retrieval metadata (see :data:`ChunkState`) opts into
    the ``metadata_only`` bucket. Both fields it covers are stamped onto a chunk
    from outside its own text, so neither moves the content hash: a section's
    ``> tags: [...]`` blockquote is promoted to ``metadata.tags`` and stripped
    from the text (#2124), and a file's frontmatter validity window is applied
    to every chunk the file produces (#2140). Editing either used to leave every
    hash-matched row filed under the old value that ``mem_search`` still read.
    Shorter states leave the bucket empty and behave exactly as before.

    Duplicate content_hash values are handled safely: each existing ID is
    reused at most once, preventing ID collisions when multiple chunks share
    identical content.
    """
    # Build hash → [id, ...] mapping to handle duplicate hashes safely
    existing_ids_by_hash: dict[str, list[tuple[str, tuple[str, ...] | None]]] = {}
    existing_meta: dict[str, _RetrievalMetadata] = {}
    for cid, state in existing_hashes.items():
        chash, hierarchy, meta = _split_state(state)
        existing_ids_by_hash.setdefault(chash, []).append((cid, hierarchy))
        existing_meta[cid] = meta

    to_upsert: list[Chunk] = []
    unchanged: list[Chunk] = []
    metadata_only: list[Chunk] = []
    used_ids: set[str] = set()

    # Reserve exact matches first across the whole file. This avoids an earlier
    # renamed duplicate body consuming an ID that a later unchanged duplicate
    # should keep. Passes run most specific first: hierarchy *and* retrieval
    # metadata, then hierarchy alone. Without the metadata pass, two
    # byte-identical sections under the same heading differing only by tags
    # could swap ids when reordered — taking each other's access counts, links
    # and line positions with them, even though an exact assignment existed.
    assignments: list[tuple[str, tuple[str, ...] | None] | None] = [None] * len(new_chunks)

    def _reserve(*, require_meta_match: bool, allow_wildcard: bool) -> None:
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
                    and (not require_meta_match or existing_meta[candidate[0]].matches(chunk))
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
    _reserve(require_meta_match=True, allow_wildcard=False)
    _reserve(require_meta_match=False, allow_wildcard=False)
    _reserve(require_meta_match=False, allow_wildcard=True)

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
                if existing_meta[reuse_id].differs_from(chunk):
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
