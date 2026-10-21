"""Cross-reference and tag management storage methods."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from uuid import UUID

from memtomem.errors import StorageError
from memtomem.models import Chunk
from memtomem.storage.sqlite_helpers import utc_stamp
from memtomem.storage.sqlite_scope import scope_context_sql
from memtomem.storage.sqlite_visibility import NOT_WITHHELD_CHUNKS, VISIBLE_SOURCE_CHUNKS


class RelationMixin:
    """Mixin providing cross-reference and tag methods. Requires self._get_db()
    and self._in_transaction."""

    async def add_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        relation_type: str = "related",
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._rolls_back_if_standalone(db):
            db.execute(
                "INSERT OR REPLACE INTO chunk_relations (source_id, target_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                (str(source_id), str(target_id), relation_type, now),
            )
            # Transaction-aware: ``apply_consolidation`` writes the
            # ``consolidated_into`` edges in the same transaction that creates the
            # summary they point at, so committing here would end the caller's
            # transaction early and strand a partial write (#2158).
            self._commit_if_standalone(db)

    async def get_related(self, chunk_id: UUID) -> list[tuple[UUID, str]]:
        db = self._get_txn_read_db()
        cid = str(chunk_id)
        rows = db.execute(
            "SELECT target_id, relation_type FROM chunk_relations WHERE source_id = ? "
            "UNION SELECT source_id, relation_type FROM chunk_relations WHERE target_id = ?",
            (cid, cid),
        ).fetchall()
        return [(UUID(row[0]), row[1]) for row in rows]

    async def get_consolidation_targets(self, chunk_ids: Sequence[UUID]) -> dict[UUID, list[UUID]]:
        """Map each of *chunk_ids* that a summary covers to those summaries.

        Only outgoing ``consolidated_into`` edges count: an original points at
        its summary, so the summary itself is not reported as consolidated —
        unlike :meth:`get_related`, which reads both directions. The edge is the
        marker by design; ``mem_link`` can replace or add one, which is the
        caller editing its own graph. Reads through the caller's transaction, so
        a check inside ``transaction()`` sees what the writer sees.
        """
        from memtomem.storage.sqlite_backend import _param_batches

        db = self._get_txn_read_db()
        targets: dict[UUID, list[UUID]] = {}
        for batch in _param_batches([str(cid) for cid in chunk_ids]):
            marks = ",".join("?" * len(batch))
            rows = db.execute(
                "SELECT source_id, target_id FROM chunk_relations "  # nosec B608
                f"WHERE relation_type = 'consolidated_into' AND source_id IN ({marks}) "
                "ORDER BY source_id, target_id",
                batch,
            ).fetchall()
            for source_id, target_id in rows:
                targets.setdefault(UUID(source_id), []).append(UUID(target_id))
        return targets

    async def get_consolidation_summaries(self, chunk_ids: Sequence[UUID]) -> set[UUID]:
        """Return those of *chunk_ids* that a ``consolidated_into`` edge points at.

        The reverse of :meth:`get_consolidation_targets`: these chunks are
        summaries, whatever else they cover themselves. Reads through the
        caller's transaction, like that method.
        """
        from memtomem.storage.sqlite_backend import _param_batches

        db = self._get_txn_read_db()
        summaries: set[UUID] = set()
        for batch in _param_batches([str(cid) for cid in chunk_ids]):
            marks = ",".join("?" * len(batch))
            rows = db.execute(
                "SELECT DISTINCT target_id FROM chunk_relations "  # nosec B608
                f"WHERE relation_type = 'consolidated_into' AND target_id IN ({marks})",
                batch,
            ).fetchall()
            summaries.update(UUID(row[0]) for row in rows)
        return summaries

    async def get_consolidation_sources(self, chunk_ids: Sequence[UUID]) -> list[UUID]:
        """Return the originals whose ``consolidated_into`` edge points at any of *chunk_ids*.

        Incoming edges only, deduplicated, in id order. Reads through the
        caller's transaction, like :meth:`get_consolidation_targets`.
        """
        from memtomem.storage.sqlite_backend import _param_batches

        db = self._get_txn_read_db()
        sources: set[str] = set()
        for batch in _param_batches([str(cid) for cid in chunk_ids]):
            marks = ",".join("?" * len(batch))
            rows = db.execute(
                "SELECT DISTINCT source_id FROM chunk_relations "  # nosec B608
                f"WHERE relation_type = 'consolidated_into' AND target_id IN ({marks})",
                batch,
            ).fetchall()
            sources.update(row[0] for row in rows)
        return [UUID(sid) for sid in sorted(sources)]

    async def carry_consolidation_edges(self, source_ids: Sequence[UUID], target_id: UUID) -> int:
        """Add a ``consolidated_into`` edge from each of *source_ids* to *target_id*.

        For a re-index that re-mints a summary chunk, inside its transaction.
        ``INSERT OR IGNORE``: the primary key is the pair without the type, so
        a pair that already holds an edge — a user's ``related`` link, say —
        keeps it rather than having it replaced. Returns the edges inserted.
        """
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._rolls_back_if_standalone(db):
            cursor = db.executemany(
                "INSERT OR IGNORE INTO chunk_relations "
                "(source_id, target_id, relation_type, created_at) "
                "VALUES (?, ?, 'consolidated_into', ?)",
                [(str(sid), str(target_id), now) for sid in source_ids],
            )
            self._commit_if_standalone(db)
        return cursor.rowcount

    async def get_relations_among(self, chunk_ids: Sequence[UUID]) -> list[tuple[UUID, UUID, str]]:
        """Return the edges whose both ends are in *chunk_ids*, of every type.

        For export: an edge with one end outside the set would dangle in the
        bundle. Ordered by ``(source_id, target_id)`` so the same store yields
        the same bundle, and with it the same provenance signature.
        """
        from memtomem.storage.sqlite_backend import _param_batches

        db = self._get_txn_read_db()
        members = {str(cid) for cid in chunk_ids}
        edges: list[tuple[str, str, str]] = []
        for batch in _param_batches(sorted(members)):
            marks = ",".join("?" * len(batch))
            rows = db.execute(
                "SELECT source_id, target_id, relation_type FROM chunk_relations "  # nosec B608
                f"WHERE source_id IN ({marks})",
                batch,
            ).fetchall()
            edges.extend(row for row in rows if row[1] in members)
        edges.sort(key=lambda row: (row[0], row[1]))
        return [(UUID(sid), UUID(tid), rel) for sid, tid, rel in edges]

    async def insert_relations(self, edges: Sequence[tuple[UUID, UUID, str]]) -> int:
        """Insert *edges* whose both ends exist; return the edges inserted.

        For import, inside its transaction. ``INSERT OR IGNORE`` for the same
        reason as :meth:`carry_consolidation_edges`: a pair that already holds an
        edge keeps it. The existence guard is in the statement because an end
        can be an id the chunk upsert ignored, and with foreign keys on, a
        dangling insert would abort the whole import.
        """
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._rolls_back_if_standalone(db):
            cursor = db.executemany(
                "INSERT OR IGNORE INTO chunk_relations "
                "(source_id, target_id, relation_type, created_at) "
                "SELECT ?, ?, ?, ? "
                "WHERE EXISTS (SELECT 1 FROM chunks WHERE id = ?) "
                "AND EXISTS (SELECT 1 FROM chunks WHERE id = ?)",
                [(str(sid), str(tid), rel, now, str(sid), str(tid)) for sid, tid, rel in edges],
            )
            self._commit_if_standalone(db)
        return cursor.rowcount

    async def delete_relation(self, source_id: UUID, target_id: UUID) -> bool:
        db = self._get_db()
        with self._rolls_back_if_standalone(db):
            cursor = db.execute(
                "DELETE FROM chunk_relations WHERE (source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?)",
                (str(source_id), str(target_id), str(target_id), str(source_id)),
            )
            self._commit_if_standalone(db)
        return cursor.rowcount > 0

    def _write_tag_batch(self, db, batch: list[tuple[str, str, int]]) -> int:
        """Apply ``(tags, updated_at, rowid)`` rewrites; return the rows written.

        The candidate reads already leave held rows out, so a held row's tags
        are never decoded and a malformed one cannot stop the visible rows
        from being rewritten. The write judges the hold again: the tag lock
        is process-local, so another process can hold a source between the
        two statements, and the write must then leave that row alone.
        """
        if not batch:
            return 0
        with self._rolls_back_if_standalone(db):
            cursor = db.executemany(
                "UPDATE chunks SET tags = ?, updated_at = ? "
                f"WHERE rowid = ? AND {VISIBLE_SOURCE_CHUNKS}",  # nosec B608
                batch,
            )
            self._commit_if_standalone(db)
        return cursor.rowcount

    async def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """Rename a tag across all visible chunks.

        Bumps ``updated_at`` on every mutated row so downstream consumers
        (search result TTL cache, decay scoring) see the rewrite as a
        write event. ``decay.py`` reads ``updated_at`` for age, so a
        rename does reset the decay timer for affected chunks — that is
        an intentional v1 trade-off (see #688 confirm thread).

        Chunks of a held source keep their tags, like the sample and count
        the service shows for the same call leave them out. If the hold is
        released with the file unchanged, they come back carrying the old tag.
        """
        db = self._get_db()
        rows = db.execute(
            f"SELECT rowid, tags FROM chunks WHERE tags LIKE ? AND {VISIBLE_SOURCE_CHUNKS}",  # nosec B608
            (f'%"{old_tag}"%',),
        ).fetchall()
        now = utc_stamp(datetime.now(timezone.utc))
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if old_tag in tags:
                tags = sorted({new_tag if t == old_tag else t for t in tags})
                batch.append((json.dumps(tags), now, row[0]))
        return self._write_tag_batch(db, batch)

    async def delete_tag(self, tag: str) -> int:
        """Delete a tag from all visible chunks. Bumps ``updated_at`` and
        leaves held chunks alone per :meth:`rename_tag`."""
        db = self._get_db()
        rows = db.execute(
            f"SELECT rowid, tags FROM chunks WHERE tags LIKE ? AND {VISIBLE_SOURCE_CHUNKS}",  # nosec B608
            (f'%"{tag}"%',),
        ).fetchall()
        now = utc_stamp(datetime.now(timezone.utc))
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if tag in tags:
                tags = [t for t in tags if t != tag]
                batch.append((json.dumps(tags), now, row[0]))
        return self._write_tag_batch(db, batch)

    async def merge_tags(self, sources: Sequence[str], target: str) -> int:
        """Replace any tag in ``sources`` with ``target`` across all visible chunks.

        Result tag list is deduplicated and sorted. Chunks that carry both
        a source tag and ``target`` collapse to a single ``target``. Returns
        the number of chunks actually mutated. ``sources`` containing
        ``target`` is allowed; the target is treated as a no-op source.
        Bumps ``updated_at`` and leaves held chunks alone per :meth:`rename_tag`.
        """
        if not sources:
            return 0
        source_set = {s for s in sources if s != target}
        if not source_set:
            return 0
        # ``LIKE`` prefilter narrows the candidate set; final membership
        # check happens in Python so substring false-positives (e.g. tag
        # spans like ``"foo","foobar"``) cannot mis-fire.
        like_clauses = " OR ".join(["tags LIKE ?"] * len(source_set))
        params = [f'%"{s}"%' for s in source_set]
        db = self._get_db()
        rows = db.execute(
            f"SELECT rowid, tags FROM chunks WHERE ({like_clauses}) "  # nosec B608
            f"AND {VISIBLE_SOURCE_CHUNKS}",
            params,
        ).fetchall()
        now = utc_stamp(datetime.now(timezone.utc))
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if not any(s in tags for s in source_set):
                continue
            new_tags = sorted({target if t in source_set else t for t in tags})
            if new_tags != tags:
                batch.append((json.dumps(new_tags), now, row[0]))
        return self._write_tag_batch(db, batch)

    async def set_chunk_tags(self, chunk_id: UUID, tags: Sequence[str]) -> bool:
        """Overwrite one chunk's tags whole-store, unless its source is hidden.

        For bulk taggers that run outside any project boundary. Only ``tags``
        and ``updated_at`` are written, so nothing read earlier can overwrite
        newer metadata, and a chunk deleted meanwhile is not recreated. The
        hold is judged in this statement. ``False`` means no row was written.
        """
        db = self._get_db()
        with self._rolls_back_if_standalone(db):
            cursor = db.execute(
                "UPDATE chunks SET tags = ?, updated_at = ? "
                f"WHERE id = ? AND {VISIBLE_SOURCE_CHUNKS}",  # nosec B608
                (json.dumps(list(tags)), utc_stamp(datetime.now(timezone.utc)), str(chunk_id)),
            )
            self._commit_if_standalone(db)
        return cursor.rowcount > 0

    async def replace_chunk_tags(
        self,
        chunk_id: UUID,
        tags: Sequence[str],
        *,
        project_context_root: Path | None,
    ) -> Chunk | None:
        """Replace one visible chunk's tags without rewriting stale metadata.

        The boundary predicate is part of the ``UPDATE`` rather than a prior
        authorization read. SQLite therefore judges the row after any writer
        it waited for has committed: a same-project migration still permits
        the tag change, while a move outside the caller's ADR-0011 boundary
        makes the statement match nothing. A redaction hold (#2777) is in the
        same predicate, so one committed after the service's read refuses the
        write too. Only ``tags`` and ``updated_at`` are written, so a
        migration's source/scope metadata cannot be restored.

        The service owns ``_tag_write_lock`` and performs the exact no-op
        check. This raw primitive must not acquire that lock again.
        """
        db = self._get_db()
        scope_fragment, scope_params = scope_context_sql(None, project_context_root)
        now = datetime.now(timezone.utc)
        updated: Chunk | None = None
        with self._rolls_back_if_standalone(db):
            cursor = db.execute(
                "UPDATE chunks SET tags=?, updated_at=? WHERE id=? "
                f"AND {scope_fragment} AND {NOT_WITHHELD_CHUNKS}",  # nosec B608
                [
                    json.dumps(list(tags)),
                    utc_stamp(now),
                    str(chunk_id),
                    *scope_params,
                ],
            )
            if cursor.rowcount:
                # Read through the writer connection before commit.
                # ``get_chunk`` uses the read pool, which cannot see an
                # enclosing transaction's uncommitted tag update.
                row = db.execute("SELECT * FROM chunks WHERE id=?", (str(chunk_id),)).fetchone()
                if row is None:
                    raise StorageError(
                        "replace_chunk_tags updated a row that could not be read back"
                    )
                updated = self._row_to_chunk(row)
            self._commit_if_standalone(db)
        return updated
