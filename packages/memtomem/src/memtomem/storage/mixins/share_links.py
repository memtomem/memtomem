"""Share-lineage storage methods — writer + reader API for ``chunk_links``.

PR-2 of the ``mem_agent_share`` chunk_links series (see
``planning/mem-agent-share-chunk-links-rfc.md``). The schema and the
one-shot back-fill ship in ``sqlite_schema.py``; this mixin hangs the
Python surface off ``SqliteBackend``.

The table is a superset of ``chunk_relations`` and is deliberately kept
separate — that table is symmetric and used for user-authored cross
references, consolidation, reflection. A share link is directed
(source → target) with a denormalised destination namespace so
"list everything I've shared out" is one index lookup, not a join.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from memtomem.models import ChunkLink
from memtomem.storage.sqlite_schema import _VALID_LINK_TYPES


def _parse_created_at(value: str) -> datetime:
    """Rehydrate an ISO-8601 timestamp written by the writer / back-fill.

    ``datetime.fromisoformat`` on 3.12 accepts both the ``Z`` and the
    ``+00:00`` suffix, but back-fill rows are written with
    ``timespec='seconds'`` and no timezone offset when the source was
    already a naive string. Treat missing offsets as UTC — every writer
    in this codebase emits UTC.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_link(row: tuple) -> ChunkLink:
    source_id, target_id, link_type, namespace_target, created_at = row
    return ChunkLink(
        source_id=UUID(source_id) if source_id else None,
        target_id=UUID(target_id),
        link_type=link_type,
        namespace_target=namespace_target,
        created_at=_parse_created_at(created_at),
    )


class ShareLinkMixin:
    """Mixin providing ``chunk_links`` writer + reader. Requires ``self._get_db()``."""

    async def add_chunk_link(
        self,
        source_id: UUID | None,
        target_id: UUID,
        link_type: str,
        namespace_target: str,
    ) -> None:
        """Record a provenance link from ``source_id`` to ``target_id``.

        Idempotent on ``(target_id, link_type)``: re-calling for the same
        destination overwrites the row (``INSERT OR REPLACE``). Intended
        to be called by ``mem_agent_share`` after its internal
        ``mem_add`` succeeds with the newly-minted destination UUID.

        ``source_id=None`` is a legal state (matches the post-delete row
        shape produced by ``ON DELETE SET NULL`` and the back-fill when
        the source UUID is unresolvable); callers normally pass the
        actual source UUID.
        """
        if link_type not in _VALID_LINK_TYPES:
            raise ValueError(
                f"link_type={link_type!r} not in {sorted(_VALID_LINK_TYPES)!r}. "
                "Add to _VALID_LINK_TYPES in storage/sqlite_schema.py to extend."
            )
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT OR REPLACE INTO chunk_links "
            "(source_id, target_id, link_type, namespace_target, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(source_id) if source_id is not None else None,
                str(target_id),
                link_type,
                namespace_target,
                now,
            ),
        )
        db.commit()

    async def get_chunk_link(
        self,
        target_id: UUID,
        link_type: str = "shared",
    ) -> ChunkLink | None:
        """Return the link row for a destination chunk, or ``None``.

        Exact lookup on the primary key ``(target_id, link_type)``.
        """
        db = self._get_db()
        row = db.execute(
            "SELECT source_id, target_id, link_type, namespace_target, created_at "
            "FROM chunk_links WHERE target_id = ? AND link_type = ?",
            (str(target_id), link_type),
        ).fetchone()
        return _row_to_link(row) if row is not None else None

    async def get_chunks_shared_from(
        self,
        source_id: UUID,
        link_type: str | None = None,
    ) -> list[ChunkLink]:
        """Return every destination that was shared *from* ``source_id``.

        Indexed by ``idx_chunk_links_source (source_id, link_type)`` so
        fanout is ``O(fanout)``, not a table scan. Pass ``link_type``
        to narrow to a specific type (default: all types).
        """
        db = self._get_db()
        if link_type is None:
            rows = db.execute(
                "SELECT source_id, target_id, link_type, namespace_target, created_at "
                "FROM chunk_links WHERE source_id = ? ORDER BY created_at, target_id",
                (str(source_id),),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT source_id, target_id, link_type, namespace_target, created_at "
                "FROM chunk_links WHERE source_id = ? AND link_type = ? "
                "ORDER BY created_at, target_id",
                (str(source_id), link_type),
            ).fetchall()
        return [_row_to_link(r) for r in rows]

    async def walk_share_chain(
        self,
        target_id: UUID,
        *,
        link_type: str = "shared",
        max_depth: int = 100,
    ) -> list[ChunkLink]:
        """Walk link rows backwards from ``target_id`` toward the root source.

        Returns links in walk order (closest to ``target_id`` first).
        Stops when:

        * there is no link row for the current target (walk_up complete);
        * the link row has ``source_id IS NULL`` (source was deleted or
          back-filled from an unresolvable tag) — the terminal row is
          still included in the result;
        * ``max_depth`` is reached — cycle defence for hand-crafted
          loops (``A→B→A`` from raw SQL) plus bounded worst-case work.
        """
        if max_depth <= 0:
            return []
        db = self._get_db()
        chain: list[ChunkLink] = []
        visited: set[UUID] = set()
        current: UUID | None = target_id
        while current is not None and len(chain) < max_depth:
            if current in visited:
                break
            visited.add(current)
            row = db.execute(
                "SELECT source_id, target_id, link_type, namespace_target, created_at "
                "FROM chunk_links WHERE target_id = ? AND link_type = ?",
                (str(current), link_type),
            ).fetchone()
            if row is None:
                break
            link = _row_to_link(row)
            chain.append(link)
            current = link.source_id
        return chain
