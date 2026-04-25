"""Namespace operations for the SQLite backend."""

from __future__ import annotations

import re
import sqlite3
from typing import Callable, Sequence

from memtomem.errors import StorageError
from memtomem.storage.sqlite_helpers import escape_like, now_iso, placeholders

# Namespace names: alphanumeric, hyphens, underscores, dots, colons, @, spaces
# (max 255). Automatic namespace generators use a ``{bucket}-{kind}:`` format
# (``claude-memory:``, ``codex-memory:``, ``agent-runtime:``); the second
# segment is sanitized through :func:`sanitize_namespace_segment` so callers
# never smuggle a stray separator character through.
_NS_NAME_RE = re.compile(r"^[\w\-.:@ ]{1,255}$", re.UNICODE)

# Characters outside the namespace allowlist — substituted to ``_`` by
# :func:`sanitize_namespace_segment`.
_SEGMENT_SAFE_RE = re.compile(r"[^\w\-.:@ ]")


def validate_namespace(name: str) -> bool:
    """Check whether *name* is a valid namespace identifier.

    Valid names contain word characters, hyphens, dots, colons, @, and spaces,
    with a maximum length of 255.
    """
    return bool(_NS_NAME_RE.match(name))


def sanitize_namespace_segment(name: str) -> str:
    """Strip whitespace and replace disallowed characters with ``_``.

    Shared by the ingest pipeline (``cli/ingest_cmd.py``) and the multi-agent
    tool (``server/tools/multi_agent.py``) so both produce namespace segments
    that satisfy :data:`_NS_NAME_RE`. Empty-input handling is the caller's
    responsibility so this helper has no error path.
    """
    return _SEGMENT_SAFE_RE.sub("_", name.strip())


def _ensure_valid_namespace(name: str) -> None:
    """Raise ``StorageError`` if *name* fails :func:`validate_namespace`."""
    if not validate_namespace(name):
        raise StorageError(
            f"Invalid namespace: {name!r} (allowed characters: word, -, ., :, @, space; max 255)"
        )


class NamespaceOps:
    """Namespace CRUD operations delegated from SqliteBackend."""

    def __init__(
        self,
        get_db: Callable[[], sqlite3.Connection],
        has_vec_table: Callable[[], bool],
    ) -> None:
        self._get_db = get_db
        # Live lookup so reset_embedding_meta()'s flag flip is visible here
        # without re-construction. Required (no default) — sole caller is
        # SqliteBackend.initialize(); a default would silently regress the
        # dim=0 guard if a future caller forgets it.
        self._has_vec_table = has_vec_table

    async def list_namespaces(self) -> list[tuple[str, int]]:
        db = self._get_db()
        rows = db.execute(
            "SELECT namespace, COUNT(*) FROM chunks GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    async def count_chunks_by_ns_prefix(self, prefixes: Sequence[str]) -> int:
        """Count chunks whose namespace starts with any of the given prefixes.

        Returns 0 when ``prefixes`` is empty. Each prefix is LIKE-escaped so
        literal ``%`` / ``_`` in a system-namespace prefix does not become a
        wildcard.
        """
        if not prefixes:
            return 0
        db = self._get_db()
        clauses = " OR ".join("namespace LIKE ? ESCAPE '\\'" for _ in prefixes)
        params = [f"{escape_like(p)}%" for p in prefixes]
        row = db.execute(
            f"SELECT COUNT(*) FROM chunks WHERE {clauses}",
            params,
        ).fetchone()
        return int(row[0]) if row else 0

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
            if self._has_vec_table():
                db.execute(
                    f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders(len(rowids))})",
                    rowids,
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
        _ensure_valid_namespace(new)
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
        _ensure_valid_namespace(namespace)
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
            params: list[object] = []
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
        # Source from BOTH ``namespace_metadata`` (registered namespaces,
        # possibly with zero chunks) and ``chunks`` (namespaces that hold
        # data but have no metadata row), unioned. Iterating only one side
        # would hide the other — registering an agent before adding any
        # chunks is a legitimate state (``mm agent register <id>`` followed
        # by ``mm agent list`` should show the agent), and conversely a
        # legacy chunk in a namespace without a metadata row should not
        # disappear from the listing.
        db = self._get_db()
        rows = db.execute("""
            SELECT
                ns.namespace,
                COALESCE(c.chunk_count, 0) AS chunk_count,
                COALESCE(m.description, '') AS description,
                COALESCE(m.color, '') AS color
            FROM (
                SELECT namespace FROM namespace_metadata
                UNION
                SELECT DISTINCT namespace FROM chunks
            ) ns
            LEFT JOIN (
                SELECT namespace, COUNT(*) AS chunk_count
                FROM chunks
                GROUP BY namespace
            ) c ON c.namespace = ns.namespace
            LEFT JOIN namespace_metadata m ON m.namespace = ns.namespace
            ORDER BY ns.namespace
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

    async def assign_namespace(
        self,
        namespace: str,
        source_filter: str | None = None,
        old_namespace: str | None = None,
    ) -> int:
        """Move chunks matching filters to *namespace*. Returns affected row count."""
        _ensure_valid_namespace(namespace)
        db = self._get_db()
        conditions: list[str] = []
        params: list = [namespace]
        if source_filter:
            conditions.append("source_file LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(source_filter)}%")
        if old_namespace:
            conditions.append("namespace = ?")
            params.append(old_namespace)
        if not conditions:
            raise ValueError("At least one filter (source_filter or old_namespace) is required")
        where = " WHERE " + " AND ".join(conditions)
        cursor = db.execute(f"UPDATE chunks SET namespace=?{where}", params)
        db.commit()
        return cursor.rowcount
