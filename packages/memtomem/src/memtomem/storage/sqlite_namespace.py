"""Namespace operations for the SQLite backend."""

from __future__ import annotations

import re
import sqlite3
from typing import Callable

from memtomem.errors import StorageError
from memtomem.storage.sqlite_helpers import now_iso, placeholders

# Namespace names: alphanumeric, hyphens, underscores, dots, colons, @, spaces (max 255)
_NS_NAME_RE = re.compile(r"^[\w\-.:@ ]{1,255}$", re.UNICODE)


def validate_namespace(name: str) -> bool:
    """Check whether *name* is a valid namespace identifier.

    Valid names contain word characters, hyphens, dots, colons, @, and spaces,
    with a maximum length of 255.
    """
    return bool(_NS_NAME_RE.match(name))


class NamespaceOps:
    """Namespace CRUD operations delegated from SqliteBackend."""

    def __init__(self, get_db: Callable[[], sqlite3.Connection]) -> None:
        self._get_db = get_db

    async def list_namespaces(self) -> list[tuple[str, int]]:
        db = self._get_db()
        rows = db.execute(
            "SELECT namespace, COUNT(*) FROM chunks GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    async def delete_by_namespace(self, namespace: str) -> int:
        db = self._get_db()
        rows = db.execute(
            "SELECT id, rowid FROM chunks WHERE namespace=?",
            (namespace,),
        ).fetchall()

        if not rows:
            return 0

        ids = [row[0] for row in rows]
        rowids = [row[1] for row in rows]

        try:
            db.execute(f"DELETE FROM chunks WHERE id IN ({placeholders(len(ids))})", ids)
            db.execute(
                f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders(len(rowids))})", rowids
            )
            db.execute(
                f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders(len(rowids))})", rowids
            )
            db.execute("DELETE FROM namespace_metadata WHERE namespace=?", (namespace,))
            db.commit()
        except Exception as exc:
            db.rollback()
            raise StorageError(
                f"delete_by_namespace failed, transaction rolled back: {exc}"
            ) from exc
        return len(rows)

    async def rename_namespace(self, old: str, new: str) -> int:
        db = self._get_db()
        cursor = db.execute("UPDATE chunks SET namespace=? WHERE namespace=?", (new, old))
        db.execute(
            "UPDATE namespace_metadata SET namespace=?, updated_at=? WHERE namespace=?",
            (new, now_iso(), old),
        )
        db.commit()
        return cursor.rowcount

    async def get_namespace_meta(self, namespace: str) -> dict | None:
        db = self._get_db()
        row = db.execute(
            "SELECT namespace, description, color, created_at, updated_at "
            "FROM namespace_metadata WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if not row:
            return None
        return {
            "namespace": row[0],
            "description": row[1],
            "color": row[2],
            "created_at": row[3],
            "updated_at": row[4],
        }

    async def set_namespace_meta(
        self,
        namespace: str,
        description: str | None = None,
        color: str | None = None,
    ) -> None:
        db = self._get_db()
        existing = await self.get_namespace_meta(namespace)
        now = now_iso()

        if existing is None:
            db.execute(
                "INSERT INTO namespace_metadata (namespace, description, color, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (namespace, description or "", color or "", now, now),
            )
        else:
            updates = []
            params: list = []
            if description is not None:
                updates.append("description=?")
                params.append(description)
            if color is not None:
                updates.append("color=?")
                params.append(color)
            if updates:
                updates.append("updated_at=?")
                params.append(now)
                params.append(namespace)
                db.execute(
                    f"UPDATE namespace_metadata SET {', '.join(updates)} WHERE namespace=?",
                    params,
                )
        db.commit()

    async def list_namespace_meta(self) -> list[dict]:
        db = self._get_db()
        rows = db.execute("""
            SELECT c.namespace, COUNT(*) as chunk_count,
                   COALESCE(m.description, '') as description,
                   COALESCE(m.color, '') as color
            FROM chunks c
            LEFT JOIN namespace_metadata m ON c.namespace = m.namespace
            GROUP BY c.namespace
            ORDER BY c.namespace
        """).fetchall()
        return [
            {
                "namespace": row[0],
                "chunk_count": row[1],
                "description": row[2],
                "color": row[3],
            }
            for row in rows
        ]
