"""Entity storage mixin — CRUD for chunk_entities table."""

from __future__ import annotations

from datetime import datetime, timezone


class EntityMixin:
    """Mixin providing entity extraction storage methods. Requires self._get_db()."""

    async def upsert_entities(self, chunk_id: str, entities: list[dict]) -> int:
        """Insert entities for a chunk. Replaces existing entities if any."""
        if not entities:
            return 0
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Delete existing entities for this chunk (overwrite mode)
        db.execute("DELETE FROM chunk_entities WHERE chunk_id = ?", (chunk_id,))

        db.executemany(
            "INSERT INTO chunk_entities (chunk_id, entity_type, entity_value, confidence, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    chunk_id,
                    e["entity_type"],
                    e["entity_value"],
                    e.get("confidence", 1.0),
                    e.get("position", 0),
                    now,
                )
                for e in entities
            ],
        )
        db.commit()
        return len(entities)

    async def delete_entities_for_chunk(self, chunk_id: str) -> int:
        db = self._get_db()
        cur = db.execute("DELETE FROM chunk_entities WHERE chunk_id = ?", (chunk_id,))
        db.commit()
        return cur.rowcount

    async def search_entities(
        self,
        entity_type: str | None = None,
        value: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search entities, optionally filtered by type, value substring, and namespace."""
        db = self._get_read_db()
        query = (
            "SELECT e.entity_type, e.entity_value, e.confidence, e.chunk_id, "
            "c.content, c.source_file, c.namespace "
            "FROM chunk_entities e "
            "JOIN chunks c ON e.chunk_id = c.id "
            "WHERE 1=1 "
        )
        params: list = []

        if entity_type:
            query += "AND e.entity_type = ? "
            params.append(entity_type)
        if value:
            query += "AND e.entity_value LIKE ? "
            params.append(f"%{value}%")
        if namespace:
            query += "AND c.namespace = ? "
            params.append(namespace)

        query += "ORDER BY e.confidence DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()
        return [
            {
                "entity_type": r[0],
                "entity_value": r[1],
                "confidence": r[2],
                "chunk_id": r[3],
                "content_preview": r[4][:120] if r[4] else "",
                "source_file": r[5],
                "namespace": r[6],
            }
            for r in rows
        ]

    async def get_entities_for_chunk(self, chunk_id: str) -> list[dict]:
        db = self._get_read_db()
        rows = db.execute(
            "SELECT entity_type, entity_value, confidence, position "
            "FROM chunk_entities WHERE chunk_id = ? ORDER BY position",
            (chunk_id,),
        ).fetchall()
        return [
            {"entity_type": r[0], "entity_value": r[1], "confidence": r[2], "position": r[3]}
            for r in rows
        ]

    async def get_entity_type_counts(self) -> dict[str, int]:
        """Return count of entities per type."""
        db = self._get_read_db()
        rows = db.execute(
            "SELECT entity_type, COUNT(*) FROM chunk_entities GROUP BY entity_type ORDER BY COUNT(*) DESC"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
