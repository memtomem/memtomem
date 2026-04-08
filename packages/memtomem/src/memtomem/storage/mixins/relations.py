"""Cross-reference and tag management storage methods."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID


class RelationMixin:
    """Mixin providing cross-reference and tag methods. Requires self._get_db()."""

    async def add_relation(
        self,
        source_id: UUID,
        target_id: UUID,
        relation_type: str = "related",
    ) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT OR REPLACE INTO chunk_relations (source_id, target_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
            (str(source_id), str(target_id), relation_type, now),
        )
        db.commit()

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
        cursor = db.execute(
            "DELETE FROM chunk_relations WHERE (source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?)",
            (str(source_id), str(target_id), str(target_id), str(source_id)),
        )
        db.commit()
        return cursor.rowcount > 0

    async def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """Rename a tag across all chunks."""
        db = self._get_db()
        rows = db.execute(
            "SELECT rowid, tags FROM chunks WHERE tags LIKE ?",
            (f'%"{old_tag}"%',),
        ).fetchall()
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if old_tag in tags:
                tags = sorted({new_tag if t == old_tag else t for t in tags})
                batch.append((json.dumps(tags), row[0]))
        if batch:
            db.executemany("UPDATE chunks SET tags = ? WHERE rowid = ?", batch)
            db.commit()
        return len(batch)

    async def delete_tag(self, tag: str) -> int:
        """Delete a tag from all chunks."""
        db = self._get_db()
        rows = db.execute(
            "SELECT rowid, tags FROM chunks WHERE tags LIKE ?",
            (f'%"{tag}"%',),
        ).fetchall()
        batch = []
        for row in rows:
            tags = json.loads(row[1]) if row[1] else []
            if tag in tags:
                tags = [t for t in tags if t != tag]
                batch.append((json.dumps(tags), row[0]))
        if batch:
            db.executemany("UPDATE chunks SET tags = ? WHERE rowid = ?", batch)
            db.commit()
        return len(batch)
