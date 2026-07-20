"""Namespace operations for the SQLite backend."""

from __future__ import annotations

import re
import sqlite3
from typing import Callable, Sequence

from memtomem.errors import NamespaceConflictError, StorageError
from memtomem.storage.base import NamespaceRenameResult
from memtomem.storage.sqlite_helpers import escape_like, now_iso, placeholders

# Savepoint name for ``rename_namespace``. A savepoint (rather than a bare
# commit/rollback pair) is what makes the method safe inside an outer
# ``SqliteBackend.transaction()``: it can undo exactly its own writes without
# tearing down a transaction it does not own.
_RENAME_SAVEPOINT = "ns_rename"

# Namespace names: alphanumeric, hyphens, underscores, dots, colons, @, spaces
# (max 255). Automatic namespace generators use a ``{bucket}-{kind}:`` format
# (``claude-memory:``, ``codex-memory:``, ``agent-runtime:``); the second
# segment is sanitized through :func:`sanitize_namespace_segment` so callers
# never smuggle a stray separator character through.
_NS_NAME_RE = re.compile(r"^[\w\-.:@ ]{1,255}$", re.UNICODE)

# Characters outside the namespace allowlist — substituted to ``_`` by
# :func:`sanitize_namespace_segment`.
_SEGMENT_SAFE_RE = re.compile(r"[^\w\-.:@ ]")


def _is_valid_ns_chars(name: str) -> bool:
    """Check whether *name* satisfies the storage-layer namespace charset.

    Valid names contain word characters, hyphens, dots, colons, @, and spaces,
    with a maximum length of 255. This is the legacy SQLite-row charset
    guard — broader than the strict caller-input validator in
    :func:`memtomem.constants.validate_namespace`, which is what every
    public surface (``mem_session_start``, ``mem_agent_share``,
    ``mem_ns_*``, etc.) calls before a value reaches storage. The two are
    deliberately different shapes; this one trips only on values that
    would break the SQLite row contract (e.g. control characters), while
    the constants validator additionally rejects shapes that are storable
    but semantically suspect (``agent-runtime:foo:bar``, comma-joined
    namespace lists, …). Kept private to ``sqlite_namespace`` so callers
    don't accidentally use it as a substitute for the public gate.
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
    """Raise ``StorageError`` if *name* fails :func:`_is_valid_ns_chars`."""
    if not _is_valid_ns_chars(name):
        raise StorageError(
            f"Invalid namespace: {name!r} (allowed characters: word, -, ., :, @, space; max 255)"
        )


class NamespaceOps:
    """Namespace CRUD operations delegated from SqliteBackend."""

    def __init__(
        self,
        get_db: Callable[[], sqlite3.Connection],
        has_vec_table: Callable[[], bool],
        in_transaction: Callable[[], bool],
    ) -> None:
        self._get_db = get_db
        # Live lookup so reset_embedding_meta()'s flag flip is visible here
        # without re-construction. Required (no default) — sole caller is
        # SqliteBackend.initialize(); a default would silently regress the
        # dim=0 guard if a future caller forgets it.
        self._has_vec_table = has_vec_table
        # Same live-lookup shape for the backend's outer-transaction flag:
        # ``rename_namespace`` must not commit or roll back a transaction
        # opened by ``SqliteBackend.transaction()``. Required (no default)
        # so a future caller can't silently regress to "always owns".
        self._in_transaction = in_transaction

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

        # The metadata delete runs unconditionally, even when the namespace has
        # no chunks: a metadata-only namespace (registered via
        # set_namespace_meta but never written to) is still listed by
        # list_namespace_meta, so an early return here would leave it
        # undeletable through this API. Return value stays the chunk count, so
        # deleting a wholly nonexistent namespace remains a 0 no-op.
        ids = [row[0] for row in rows]
        rowids = [row[1] for row in rows]

        try:
            if rows:
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

    async def rename_namespace(
        self, old: str, new: str, *, merge: bool = False
    ) -> NamespaceRenameResult:
        """Rename namespace *old* to *new*, atomically.

        **Existence is decided by ``chunks`` ∪ ``namespace_metadata``.**
        Those two tables are what a namespace *is*; everything else that
        stores a namespace string merely points at it:

        * ``sessions.namespace`` **follows** the rename inside the same
          transaction — a live session's auto-summary filters chunks by
          the namespace recorded on its row, so leaving it behind would
          make the summary find nothing (``server/tools/session.py``).
          It does not make a namespace *exist*, though: a session-only
          namespace is not renameable and is not a rename target.
        * ``chunk_links.namespace_target`` is deliberately **not**
          rewritten. It records what the target namespace was called at
          share time — an immutable historical fact, not a live pointer.

        Conflict policy: if *new* already exists, the rename is refused
        with :class:`NamespaceConflictError` **before any write**.
        ``merge=True`` opts into consolidation; the target's metadata row
        then wins (its description / color survive, only ``updated_at``
        moves) and the source's row is dropped. Renaming a namespace onto
        itself is always refused — under the merge branch it would delete
        the sole metadata row.

        Returns a :class:`NamespaceRenameResult`; ``chunks_moved == 0``
        does not mean nothing changed (see that class).
        """
        _ensure_valid_namespace(new)
        if old == new:
            raise NamespaceConflictError(
                f"Cannot rename namespace {old!r} onto itself (source and target are equal)"
            )

        db = self._get_db()
        # Two independent signals — conflating them reopens a race (the
        # same distinction ``SqliteBackend.reset_all`` documents):
        #   * the write lock is gated on ``db.in_transaction``, because
        #     ``transaction()`` only flips the backend's flag and does NOT
        #     begin a SQLite transaction — a rename that is the first
        #     statement inside that CM still needs its own BEGIN. Python's
        #     lazy transaction start would only promote on the first DML,
        #     leaving the preflight SELECTs below unprotected against a
        #     concurrent writer creating the target between check and UPDATE.
        #   * ownership (``_in_transaction``) decides only whether *we* are
        #     allowed to commit/rollback the whole transaction at the end.
        # The savepoint covers the borrowed case: a caller that catches this
        # method's StorageError inside its own ``transaction()`` block must
        # not end up committing our half-written rows.
        owns_txn = not self._in_transaction()
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        db.execute(f"SAVEPOINT {_RENAME_SAVEPOINT}")

        try:
            if not self._namespace_exists(db, old):
                # Renaming a namespace that holds nothing is a no-op, not an
                # error — and not a conflict either, so this check precedes
                # the target evaluation below. Falls through to the shared
                # finalize path so the lock taken above is always released.
                result = NamespaceRenameResult(
                    chunks_moved=0, metadata_renamed=False, merged=False
                )
            else:
                target_chunks = db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE namespace=?", (new,)
                ).fetchone()[0]
                target_meta = self._has_namespace_meta(db, new)
                merged = bool(target_chunks) or target_meta
                if merged and not merge:
                    raise NamespaceConflictError(
                        f"Cannot rename namespace {old!r} to {new!r}: target already exists "
                        f"({target_chunks} chunk(s), metadata row: "
                        f"{'yes' if target_meta else 'no'}). Pass merge=True to consolidate "
                        f"into it (the target's description/color are kept), or move only the "
                        f"chunks with ns_assign(namespace={new!r}, old_namespace={old!r})."
                    )

                now = now_iso()
                chunks_moved = db.execute(
                    "UPDATE chunks SET namespace=? WHERE namespace=?", (new, old)
                ).rowcount
                # Sessions follow the rename (see docstring) — their
                # namespace is a live filter, not a historical record.
                db.execute("UPDATE sessions SET namespace=? WHERE namespace=?", (new, old))

                if target_meta:
                    # Target wins: keep its description/color, drop the
                    # source row. A plain UPDATE would trip the PK here —
                    # which is exactly the failure this method used to leave
                    # half-applied (#1874).
                    db.execute(
                        "UPDATE namespace_metadata SET updated_at=? WHERE namespace=?",
                        (now, new),
                    )
                    db.execute("DELETE FROM namespace_metadata WHERE namespace=?", (old,))
                    metadata_renamed = False
                else:
                    metadata_renamed = bool(
                        db.execute(
                            "UPDATE namespace_metadata SET namespace=?, updated_at=? "
                            "WHERE namespace=?",
                            (new, now, old),
                        ).rowcount
                    )
                result = NamespaceRenameResult(
                    chunks_moved=chunks_moved,
                    metadata_renamed=metadata_renamed,
                    merged=merged,
                )

            db.execute(f"RELEASE {_RENAME_SAVEPOINT}")
            if owns_txn:
                db.commit()
            return result
        except NamespaceConflictError:
            # Typed passthrough — the conflict is caller-resolvable and each
            # surface translates it (web → 409); wrapping it in a generic
            # StorageError would erase that.
            self._undo_rename(db, owns_txn)
            raise
        except Exception as exc:
            self._undo_rename(db, owns_txn)
            raise StorageError(f"rename_namespace failed, transaction rolled back: {exc}") from exc

    @staticmethod
    def _undo_rename(db: sqlite3.Connection, owns_txn: bool) -> None:
        """Discard this rename's writes, whether we own the transaction or not."""
        db.execute(f"ROLLBACK TO {_RENAME_SAVEPOINT}")
        db.execute(f"RELEASE {_RENAME_SAVEPOINT}")
        if owns_txn:
            # Also ends the transaction opened above, releasing the RESERVED
            # lock instead of leaving it for the next unrelated commit.
            db.rollback()

    @staticmethod
    def _has_namespace_meta(db: sqlite3.Connection, namespace: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM namespace_metadata WHERE namespace=?", (namespace,)
            ).fetchone()
            is not None
        )

    def _namespace_exists(self, db: sqlite3.Connection, namespace: str) -> bool:
        """True when *namespace* holds chunks or a metadata row.

        Sessions are excluded on purpose — see ``rename_namespace``.
        """
        row = db.execute(
            "SELECT 1 FROM chunks WHERE namespace=? LIMIT 1", (namespace,)
        ).fetchone()
        return row is not None or self._has_namespace_meta(db, namespace)

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
        now = now_iso()

        # INSERT OR IGNORE instead of read-then-branch: two concurrent
        # first-time registrations (e.g. ``mem_agent_register`` racing itself
        # across the ``await`` of a read) would both see "missing" and both
        # INSERT — the loser died on the PK (#1574 item 4). The atomic upsert
        # pair below keeps the old semantics: a fresh row gets ``""`` for
        # omitted fields, an existing row is only touched for the fields the
        # caller actually passed (None means "leave as is").
        db.execute(
            "INSERT OR IGNORE INTO namespace_metadata "
            "(namespace, description, color, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (namespace, description or "", color or "", now, now),
        )
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
                SELECT namespace FROM chunks
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
