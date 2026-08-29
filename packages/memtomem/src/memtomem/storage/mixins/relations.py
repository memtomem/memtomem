"""Cross-reference and tag management storage methods."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence
from uuid import UUID

from memtomem.storage.sqlite_helpers import utc_stamp


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
        db = self._get_db()
        cid = str(chunk_id)
        rows = db.execute(
            "SELECT target_id, relation_type FROM chunk_relations WHERE source_id = ? "
            "UNION SELECT source_id, relation_type FROM chunk_relations WHERE target_id = ?",
            (cid, cid),
        ).fetchall()
        return [(UUID(row[0]), row[1]) for row in rows]

    async def delete_relation(self, source_id: UUID, target_id: UUID) -> bool:
        db = self._get_db()
        with self._rolls_back_if_standalone(db):
            cursor = db.execute(
                "DELETE FROM chunk_relations WHERE (source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?)",
                (str(source_id), str(target_id), str(target_id), str(source_id)),
            )
            self._commit_if_standalone(db)
        return cursor.rowcount > 0

    async def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """Rename a tag across all chunks.

        Bumps ``updated_at`` on every mutated row so downstream consumers
        (search result TTL cache, decay scoring) see the rewrite as a
        write event. ``decay.py`` reads ``updated_at`` for age, so a
        rename does reset the decay timer for affected chunks — that is
        an intentional v1 trade-off (see #688 confirm thread).
        """
        db = self._get_db()
        rows = db.execute(
            "SELECT rowid, tags FROM chunks WHERE tags LIKE ?",
            (f'%"{old_tag}"%',),
        ).fetchall()
        now = utc_stamp(datetime.now(timezone.utc))
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if old_tag in tags:
                tags = sorted({new_tag if t == old_tag else t for t in tags})
                batch.append((json.dumps(tags), now, row[0]))
        if batch:
            with self._rolls_back_if_standalone(db):
                db.executemany("UPDATE chunks SET tags = ?, updated_at = ? WHERE rowid = ?", batch)
                self._commit_if_standalone(db)
        return len(batch)

    async def delete_tag(self, tag: str) -> int:
        """Delete a tag from all chunks. Bumps ``updated_at`` per
        :meth:`rename_tag`."""
        db = self._get_db()
        rows = db.execute(
            "SELECT rowid, tags FROM chunks WHERE tags LIKE ?",
            (f'%"{tag}"%',),
        ).fetchall()
        now = utc_stamp(datetime.now(timezone.utc))
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if tag in tags:
                tags = [t for t in tags if t != tag]
                batch.append((json.dumps(tags), now, row[0]))
        if batch:
            with self._rolls_back_if_standalone(db):
                db.executemany("UPDATE chunks SET tags = ?, updated_at = ? WHERE rowid = ?", batch)
                self._commit_if_standalone(db)
        return len(batch)

    async def merge_tags(self, sources: Sequence[str], target: str) -> int:
        """Replace any tag in ``sources`` with ``target`` across all chunks.

        Result tag list is deduplicated and sorted. Chunks that carry both
        a source tag and ``target`` collapse to a single ``target``. Returns
        the number of chunks actually mutated. ``sources`` containing
        ``target`` is allowed; the target is treated as a no-op source.
        Bumps ``updated_at`` per :meth:`rename_tag`.
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
            f"SELECT rowid, tags FROM chunks WHERE {like_clauses}",
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
        if batch:
            with self._rolls_back_if_standalone(db):
                db.executemany("UPDATE chunks SET tags = ?, updated_at = ? WHERE rowid = ?", batch)
                self._commit_if_standalone(db)
        return len(batch)
