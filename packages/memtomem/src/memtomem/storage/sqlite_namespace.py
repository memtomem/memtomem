"""Namespace operations for the SQLite backend."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Sequence

from memtomem.errors import NamespaceConflictError, StorageError
from memtomem.storage.base import NamespaceAssignResult, NamespaceRenameResult
from memtomem.storage.sqlite_helpers import escape_like, now_iso, placeholders, quote_ident

logger = logging.getLogger(__name__)

# Dedicated savepoints for namespace writers. A savepoint (rather than a bare
# commit/rollback pair) makes each method safe inside an outer
# ``SqliteBackend.transaction()``: it can undo exactly its own writes without
# tearing down a transaction it does not own.
_RENAME_SAVEPOINT = "ns_rename"
_ASSIGN_SAVEPOINT = "ns_assign"
_DELETE_SAVEPOINT = "ns_delete"
_SET_META_SAVEPOINT = "ns_set_meta"

# Per-operation TEMP table used to collapse duplicate-to-survivor mappings
# into set-based edge deletes. A linear mapping avoids materializing every
# pair in a large equivalence group (O(k^2)) while the write lock is held.
_DUPLICATE_MAP_TABLE = "_ns_duplicate_map"

# Row cap per ``… IN (?, ?, …)`` delete during a merge. SQLite's
# host-parameter limit is 999 on builds older than 3.32, and a namespace-wide
# merge can carry more duplicates than that.
_DELETE_BATCH = 500

# Namespace names: alphanumeric, hyphens, underscores, dots, colons, @, spaces
# (max 255). Automatic namespace generators use a ``{bucket}-{kind}:`` format
# (``claude-memory:``, ``codex-memory:``, ``agent-runtime:``); the second
# segment is sanitized through :func:`sanitize_namespace_segment` so callers
# never smuggle a stray separator character through.
_NS_NAME_RE = re.compile(r"^[\w\-.:@ ]{1,255}$", re.UNICODE)

# Characters outside the namespace allowlist — substituted to ``_`` by
# :func:`sanitize_namespace_segment`.
_SEGMENT_SAFE_RE = re.compile(r"[^\w\-.:@ ]")


@dataclass(frozen=True, slots=True)
class _DuplicatePlan:
    """One survivor and the selected duplicate rows that must be removed."""

    survivor_id: str
    losers: tuple[tuple[str, int], ...]  # (chunk id, rowid)


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
        # namespace writers must not commit or roll back a transaction opened
        # by ``SqliteBackend.transaction()``. Required (no default) so a future
        # caller can't silently regress to "always owns".
        self._in_transaction = in_transaction

    async def list_namespaces(self) -> list[tuple[str, int]]:
        db = self._get_db()
        rows = db.execute(
            "SELECT namespace, COUNT(*) FROM chunks GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    async def count_chunks_by_namespace(self, namespace: str) -> int:
        """Count chunks in one namespace.

        The rename receipt needs the target's resulting total; going through
        ``list_namespaces`` for it aggregates the whole ``chunks`` table to
        read a single bucket.
        """
        db = self._get_db()
        row = db.execute("SELECT COUNT(*) FROM chunks WHERE namespace=?", (namespace,)).fetchone()
        return int(row[0]) if row else 0

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
        owns_txn = self._begin_namespace_write(
            db,
            operation="delete_by_namespace",
            action="deleting a namespace",
        )

        try:
            db.execute(f"SAVEPOINT {_DELETE_SAVEPOINT}")
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
            db.execute(f"RELEASE {_DELETE_SAVEPOINT}")
            if owns_txn:
                db.commit()
        except Exception as exc:
            self._undo_namespace_write(
                db,
                savepoint=_DELETE_SAVEPOINT,
                owns_txn=owns_txn,
                operation="delete_by_namespace",
            )
            raise StorageError(f"delete_by_namespace failed, operation rolled back: {exc}") from exc
        return len(rows)

    def _begin_namespace_write(
        self,
        db: sqlite3.Connection,
        *,
        operation: str,
        action: str,
    ) -> bool:
        """Take the write lock and return whether this method owns it.

        An owning ``SqliteBackend.transaction()`` has already taken
        ``BEGIN IMMEDIATE``. Standalone calls take the same lock here, while
        commit/rollback ownership comes from the injected task-affine flag. A
        pending transaction with no backend owner belongs to an unrelated
        writer and is refused.
        """
        borrowed = self._in_transaction()
        if not db.in_transaction:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageError(f"{operation} could not take the write lock: {exc}") from exc
            return not borrowed
        if not borrowed:
            raise StorageError(
                f"{operation} refused: the connection already has an open transaction "
                "that SqliteBackend.transaction() does not own. Commit or roll it back "
                f"before {action} — this method cannot report a result it cannot commit."
            )
        return False

    @staticmethod
    def _undo_namespace_write(
        db: sqlite3.Connection,
        *,
        savepoint: str,
        owns_txn: bool,
        operation: str,
    ) -> None:
        """Undo one namespace operation without masking its original error."""
        for statement in (f"ROLLBACK TO {savepoint}", f"RELEASE {savepoint}"):
            try:
                db.execute(statement)
            except sqlite3.Error as exc:
                logger.debug("%s undo: %s failed (%s)", operation, statement, exc)
        if owns_txn:
            try:
                db.rollback()
            except sqlite3.Error as exc:
                logger.warning("%s undo: rollback failed (%s)", operation, exc)

    async def rename_namespace(
        self, old: str, new: str, *, merge: bool = False
    ) -> NamespaceRenameResult:
        """Rename namespace *old* to *new*, atomically.

        **Existence is decided by ``chunks`` ∪ ``namespace_metadata``.**
        Those two tables are what a namespace *is*; everything else that
        stores a namespace string merely points at it:

        * ``sessions.namespace`` **follows** the rename inside the same
          transaction — unmarked or provenance-incomplete sessions retain
          the legacy auto-summary fallback that filters chunks by the
          namespace recorded on the row, so leaving it behind would make
          that fallback find nothing (``server/tools/session.py``). It does
          not make a namespace *exist*, though: a session-only namespace is
          not renameable and is not a rename target.
        * ``chunk_links.namespace_target`` is deliberately **not**
          rewritten. It records what the target namespace was called at
          share time — an immutable historical fact, not a live pointer.

        Conflict policy: if *new* already exists, the rename is refused
        with :class:`NamespaceConflictError` **before any write**.
        ``merge=True`` opts into consolidation; the target's metadata row
        then wins (its description / color survive, only ``updated_at``
        moves) and the source's row is dropped. Chunks the target already
        holds are dropped the same way — see
        :meth:`_drop_duplicate_chunks` — and counted separately from the
        moved ones. Renaming a namespace onto itself is always refused —
        under the merge branch it would delete the sole metadata row.

        Returns a :class:`NamespaceRenameResult`; ``chunks_moved == 0``
        does not mean nothing changed (see that class).
        """
        _ensure_valid_namespace(new)
        if old == new:
            raise NamespaceConflictError(
                f"Cannot rename namespace {old!r} onto itself (source and target are equal)",
                reason_code="same_name",
            )

        db = self._get_db()
        owns_txn = self._begin_namespace_write(
            db,
            operation="rename_namespace",
            action="renaming",
        )

        try:
            db.execute(f"SAVEPOINT {_RENAME_SAVEPOINT}")
            if not self._namespace_exists(db, old):
                # Renaming a namespace that holds nothing is a no-op, not an
                # error — and not a conflict either, so this check precedes
                # the target evaluation below. Falls through to the shared
                # finalize path so the lock taken above is always released.
                result = NamespaceRenameResult(chunks_moved=0, metadata_renamed=False, merged=False)
            else:
                target_chunks = db.execute(
                    "SELECT COUNT(*) FROM chunks WHERE namespace=?", (new,)
                ).fetchone()[0]
                target_meta = self._has_namespace_meta(db, new)
                merged = bool(target_chunks) or target_meta
                if merged and not merge:
                    # Condition only — no "pass merge=True", no tool names. The
                    # web user reading this 409 has no merge affordance, and a
                    # remedy they cannot act on is worse than none (#1870).
                    # Surfaces phrase their own off ``reason_code``.
                    raise NamespaceConflictError(
                        f"Cannot rename namespace {old!r} to {new!r}: target already exists "
                        f"({target_chunks} chunk(s), metadata row: "
                        f"{'yes' if target_meta else 'no'})",
                        reason_code="target_exists",
                    )

                now = now_iso()
                duplicates_dropped = self._drop_duplicate_chunks(db, old, new) if merged else 0
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
                    duplicates_dropped=duplicates_dropped,
                )

            db.execute(f"RELEASE {_RENAME_SAVEPOINT}")
            if owns_txn:
                db.commit()
            return result
        except NamespaceConflictError:
            # Typed passthrough — the conflict is caller-resolvable and each
            # surface translates it (web → 409); wrapping it in a generic
            # StorageError would erase that.
            self._undo_namespace_write(
                db,
                savepoint=_RENAME_SAVEPOINT,
                owns_txn=owns_txn,
                operation="rename_namespace",
            )
            raise
        except Exception as exc:
            self._undo_namespace_write(
                db,
                savepoint=_RENAME_SAVEPOINT,
                owns_txn=owns_txn,
                operation="rename_namespace",
            )
            raise StorageError(f"rename_namespace failed, transaction rolled back: {exc}") from exc

    def _drop_duplicate_chunks(self, db: sqlite3.Connection, old: str, new: str) -> int:
        """Delete source chunks the target already holds. Returns how many.

        ``chunks`` carries a UNIQUE index on
        ``(namespace, source_file, content_hash, start_line)`` (#691), so a
        merge whose two namespaces indexed the *same* file — the common case
        for ``mm agent migrate``, where a legacy and a canonical namespace
        cover one agent — would otherwise trip that index and turn an
        explicitly requested consolidation into a failure.

        The target's copy wins, matching the metadata rule: it keeps its
        accumulated access/use counters, and the source duplicate is removed
        with its FTS / vector sidecar rows (same shape as
        ``delete_by_namespace``). Everything that pointed *at* the dropped
        row is first re-pointed at the surviving twin
        (:meth:`_remap_chunk_references`), so relations, entity mentions,
        share lineage and assertions survive a merge instead of being
        cascaded away. The count is reported back so the caller can say
        that rows were dropped rather than moved.
        """
        rows = db.execute(
            """
            SELECT c.id, c.rowid, MIN(t.id) AS survivor
            FROM chunks c
            JOIN chunks t
              ON t.namespace = ?
             AND t.source_file = c.source_file
             AND t.content_hash = c.content_hash
             -- ``IS`` matches the UNIQUE index's key exactly only because
             -- ``start_line`` is NOT NULL. Were the column ever made
             -- nullable, the index would treat two NULLs as distinct (no
             -- collision) while ``IS`` would call them equal — deleting rows
             -- that never collided.
             AND t.start_line IS c.start_line
            WHERE c.namespace = ?
            GROUP BY c.id, c.rowid
            """,
            (new, old),
        ).fetchall()
        plans = [
            _DuplicatePlan(survivor_id=row[2], losers=((row[0], int(row[1])),)) for row in rows
        ]
        return self._drop_duplicate_plans(db, plans)

    def _drop_duplicate_plans(
        self,
        db: sqlite3.Connection,
        plans: Sequence[_DuplicatePlan],
    ) -> int:
        """Remap and delete duplicate rows described by *plans*."""
        losers = [loser for plan in plans for loser in plan.losers]
        if not losers:
            return 0
        pairs = [(plan.survivor_id, loser_id) for plan in plans for loser_id, _rowid in plan.losers]
        self._remap_chunk_references(db, pairs)
        ids = [loser_id for loser_id, _rowid in losers]
        rowids = [rowid for _loser_id, rowid in losers]
        # Batched: a namespace-wide merge can carry more duplicates than
        # SQLite's host-parameter limit (999 on older builds), and blowing
        # that limit would fail the whole migration.
        for start in range(0, len(ids), _DELETE_BATCH):
            batch_ids = ids[start : start + _DELETE_BATCH]
            batch_rowids = rowids[start : start + _DELETE_BATCH]
            db.execute(
                f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders(len(batch_rowids))})",
                batch_rowids,
            )
            if self._has_vec_table():
                db.execute(
                    f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders(len(batch_rowids))})",
                    batch_rowids,
                )
            db.execute(
                f"DELETE FROM chunks WHERE id IN ({placeholders(len(batch_ids))})", batch_ids
            )
        return len(losers)

    @staticmethod
    def _chunk_reference_columns(db: sqlite3.Connection) -> list[tuple[str, str]]:
        """Return ``(table, column)`` for every FK pointing at ``chunks(id)``.

        Enumerated from the live schema rather than hardcoded: the tables
        that reference a chunk have grown over time (relations, chunk_links,
        entity mentions, the access log, ``memory_assertions``), and a
        hardcoded list would go stale silently — the next table would simply
        lose its rows to ``ON DELETE CASCADE`` with no test failing. Same
        reasoning as ``reset_all``'s ``sqlite_master`` enumeration (#1832).
        """
        out: list[tuple[str, str]] = []
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            if table == "chunks":
                continue
            try:
                fks = db.execute(f"PRAGMA foreign_key_list({quote_ident(table)})").fetchall()
            except sqlite3.DatabaseError:
                # Virtual-table shadows and modules that don't implement the
                # pragma: nothing there references chunks(id) anyway.
                continue
            for fk in fks:
                # (id, seq, table, from, to, on_update, on_delete, match)
                if fk[2] == "chunks" and (fk[4] is None or fk[4] == "id"):
                    out.append((table, fk[3]))
        return out

    def _remap_chunk_references(
        self,
        db: sqlite3.Connection,
        pairs: Sequence[tuple[str, str]],
    ) -> None:
        """Re-point references from each dropped chunk to its surviving twin.

        *pairs* is ``[(survivor_id, dropped_id), …]``. The two rows in a pair
        are the same content indexed twice, so a relation or an entity
        mention recorded against one is equally true of the other — letting
        the cascade delete them instead would quietly lose provenance that
        has no other copy. ``UPDATE OR IGNORE`` handles the case where the
        survivor already carries the same row (a relation to the same target,
        say): the source's copy is left to be cascaded away, which is the
        target-wins rule again.

        ``OR IGNORE`` only collapses a row the survivor already carries
        where a uniqueness constraint says the two are the same row. Tables
        without one — ``chunk_entities`` (three non-unique indexes),
        ``memory_assertions`` (lookup index only) — therefore end up holding
        *both* copies for a merged chunk: the same entity mention twice, and
        a `GROUP BY entity_type` count that reads one too many. No
        well-defined key exists to dedupe them on here (``created_at``
        differs between the two indexing runs, so "identical row" is not
        identity), and the state self-heals the next time that chunk is
        re-indexed, since ``set_chunk_entities`` deletes before inserting.
        Stated rather than silently inherited.

        One asymmetry is deliberate: ``access_log`` rows move to the survivor
        while ``chunks.access_count`` / ``last_accessed`` stay as the target
        had them (target-wins, like the metadata). The log keeps the fuller
        history — those accesses did happen, to content the survivor now
        represents — while the denormalized counters keep the survivor's own
        ranking signal rather than inheriting a merged one. So the two can
        disagree for a merged chunk; the counters are not derived from the
        log and no surface reconciles them.

        Schema discovery runs once for the whole merge — a per-duplicate
        ``PRAGMA`` sweep would put ``O(duplicates × tables)`` metadata
        queries inside the write lock, which a large ``mm agent migrate``
        would feel. The updates stay **pair-major** (all of one duplicate's
        references, then the next) rather than column-major: when two
        remapped rows collapse onto the same key, ``OR IGNORE`` keeps
        whichever landed first, and pair order is the one a reader can
        reason about — column order is an artifact of the FK declaration.
        """
        columns = self._chunk_reference_columns(db)
        self._drop_edges_between_duplicates(db, columns, pairs)
        for pair in pairs:
            for table, column in columns:
                db.execute(
                    f"UPDATE OR IGNORE {quote_ident(table)} SET {quote_ident(column)}=? "
                    f"WHERE {quote_ident(column)}=?",
                    pair,
                )

    @staticmethod
    def _drop_edges_between_duplicates(
        db: sqlite3.Connection,
        columns: Sequence[tuple[str, str]],
        pairs: Sequence[tuple[str, str]],
    ) -> None:
        """Delete edges whose endpoints collapse into one surviving chunk.

        A table with two FKs to ``chunks`` (``chunk_relations``,
        ``chunk_links``) can hold an edge saying "this chunk relates to /
        was shared from that one" where the endpoints turn out to be the same
        content indexed in multiple namespaces. Remapping such a row would
        point it at itself. A TEMP mapping table records each member's final
        survivor once, so loser-to-loser edges in groups larger than two are
        removed without expanding the group into every possible pair.

        Run *before* the remap and matched on the exact endpoint pair, so a
        self-edge the surviving chunk already carried — someone else's row,
        with its own meaning — is left untouched.
        """
        mapping_table = quote_ident(_DUPLICATE_MAP_TABLE)
        # A previous hard SQLite abort may have bypassed normal cleanup. The
        # table is connection-local and contains only operation-scoped ids, so
        # always recreate it inside this method's savepoint.
        db.execute(f"DROP TABLE IF EXISTS temp.{mapping_table}")
        db.execute(
            f"CREATE TEMP TABLE {mapping_table} ("
            "chunk_id TEXT PRIMARY KEY, survivor_id TEXT NOT NULL"
            ") WITHOUT ROWID"
        )
        # Survivors repeat when one group has several losers; losers must be
        # unique and map directly to their final survivor (no chains).
        db.executemany(
            f"INSERT OR IGNORE INTO {mapping_table} (chunk_id, survivor_id) VALUES (?, ?)",
            ((survivor, survivor) for survivor, _dropped in pairs),
        )
        db.executemany(
            f"INSERT INTO {mapping_table} (chunk_id, survivor_id) VALUES (?, ?)",
            ((dropped, survivor) for survivor, dropped in pairs),
        )

        by_table: dict[str, list[str]] = {}
        for table, column in columns:
            by_table.setdefault(table, []).append(column)
        for table, cols in by_table.items():
            if len(cols) < 2:
                continue
            for i, left in enumerate(cols):
                for right in cols[i + 1 :]:
                    q_table = quote_ident(table)
                    q_left = quote_ident(left)
                    q_right = quote_ident(right)
                    # One statement per FK-column pair, independent of how
                    # many equivalent chunks the assignment selected. The IN
                    # predicates let SQLite use the existing FK indexes; the
                    # scalar lookups then prove both endpoints collapse to the
                    # same survivor. Existing self-edges stay untouched.
                    db.execute(
                        f"DELETE FROM {q_table} "
                        f"WHERE {q_left} IN (SELECT chunk_id FROM temp.{mapping_table}) "
                        f"AND {q_right} IN (SELECT chunk_id FROM temp.{mapping_table}) "
                        f"AND {q_left} <> {q_right} "
                        f"AND (SELECT survivor_id FROM temp.{mapping_table} "
                        f"     WHERE chunk_id={q_table}.{q_left}) = "
                        f"    (SELECT survivor_id FROM temp.{mapping_table} "
                        f"     WHERE chunk_id={q_table}.{q_right})"
                    )
        db.execute(f"DROP TABLE temp.{mapping_table}")

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
        row = db.execute("SELECT 1 FROM chunks WHERE namespace=? LIMIT 1", (namespace,)).fetchone()
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

        owns_txn = self._begin_namespace_write(
            db,
            operation="set_namespace_meta",
            action="updating namespace metadata",
        )
        try:
            db.execute(f"SAVEPOINT {_SET_META_SAVEPOINT}")
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
            if updates:
                db.execute(
                    f"UPDATE namespace_metadata SET {', '.join(updates)} WHERE namespace=?",
                    params,
                )
            db.execute(f"RELEASE {_SET_META_SAVEPOINT}")
            if owns_txn:
                db.commit()
        except Exception as exc:
            self._undo_namespace_write(
                db,
                savepoint=_SET_META_SAVEPOINT,
                owns_txn=owns_txn,
                operation="set_namespace_meta",
            )
            raise StorageError(f"set_namespace_meta failed, operation rolled back: {exc}") from exc

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

    @staticmethod
    def _find_assign_duplicate_plans(
        db: sqlite3.Connection,
        namespace: str,
        conditions: Sequence[str],
        filter_params: Sequence[object],
    ) -> list[_DuplicatePlan]:
        """Plan the rows that would collide after a filtered assignment.

        Existing target rows always survive. When a source-only filter selects
        the same chunk from several namespaces, the #691 dedup ordering picks
        the most-used, then oldest, then lexically smallest-id source row.
        """
        selected_where = " AND ".join((*conditions, "namespace <> ?"))
        rows = db.execute(
            f"""
            WITH selected AS (
                SELECT id, rowid, namespace, source_file, content_hash, start_line,
                       access_count, use_count, created_at
                FROM chunks
                WHERE {selected_where}
            ),
            selected_keys AS (
                SELECT DISTINCT source_file, content_hash, start_line
                FROM selected
            ),
            pool AS (
                SELECT target.id, target.rowid, target.namespace,
                       target.source_file, target.content_hash, target.start_line,
                       target.access_count, target.use_count, target.created_at,
                       1 AS is_target, 0 AS is_selected
                FROM selected_keys AS selected_key
                CROSS JOIN chunks AS target
                WHERE target.namespace = ?
                  AND target.source_file = selected_key.source_file
                  AND target.content_hash = selected_key.content_hash
                  AND target.start_line IS selected_key.start_line
                UNION ALL
                SELECT id, rowid, namespace, source_file, content_hash, start_line,
                       access_count, use_count, created_at,
                       0 AS is_target, 1 AS is_selected
                FROM selected
            ),
            ranked AS (
                SELECT *,
                       FIRST_VALUE(id) OVER (
                           PARTITION BY source_file, content_hash, start_line
                           ORDER BY is_target DESC,
                                    (access_count + use_count) DESC,
                                    created_at ASC,
                                    id ASC
                       ) AS survivor_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY source_file, content_hash, start_line
                           ORDER BY is_target DESC,
                                    (access_count + use_count) DESC,
                                    created_at ASC,
                                    id ASC
                       ) AS duplicate_rank
                FROM pool
            )
            SELECT source_file, content_hash, start_line,
                   survivor_id, id, rowid
            FROM ranked
            WHERE is_selected = 1 AND duplicate_rank > 1
            ORDER BY source_file, content_hash, start_line, duplicate_rank
            """,
            (*filter_params, namespace, namespace),
        ).fetchall()

        grouped: dict[tuple[object, object, object], tuple[str, list[tuple[str, int]]]] = {}
        for source_file, content_hash, start_line, survivor_id, loser_id, loser_rowid in rows:
            key = (source_file, content_hash, start_line)
            if key not in grouped:
                grouped[key] = (survivor_id, [])
            grouped[key][1].append((loser_id, int(loser_rowid)))
        return [
            _DuplicatePlan(survivor_id=survivor_id, losers=tuple(losers))
            for survivor_id, losers in grouped.values()
        ]

    async def assign_namespace(
        self,
        namespace: str,
        source_filter: str | None = None,
        old_namespace: str | None = None,
        *,
        merge: bool = False,
    ) -> NamespaceAssignResult:
        """Move filtered chunks to *namespace* without changing namespace identity.

        Assignment is chunks-only: metadata and sessions do not follow. A
        collision is refused before writes unless ``merge=True`` explicitly
        opts into keeping one copy and dropping the selected duplicates.
        """
        _ensure_valid_namespace(namespace)
        conditions: list[str] = []
        filter_params: list[object] = []
        if source_filter:
            conditions.append("source_file LIKE ? ESCAPE '\\'")
            filter_params.append(f"%{escape_like(source_filter)}%")
        if old_namespace:
            conditions.append("namespace = ?")
            filter_params.append(old_namespace)
        if not conditions:
            raise ValueError("At least one filter (source_filter or old_namespace) is required")

        db = self._get_db()
        owns_txn = self._begin_namespace_write(
            db,
            operation="assign_namespace",
            action="assigning chunks",
        )
        try:
            db.execute(f"SAVEPOINT {_ASSIGN_SAVEPOINT}")
            duplicate_plans = self._find_assign_duplicate_plans(
                db,
                namespace,
                conditions,
                filter_params,
            )
            overlap_count = sum(len(plan.losers) for plan in duplicate_plans)
            if overlap_count and not merge:
                raise NamespaceConflictError(
                    f"Cannot assign selected chunks to namespace {namespace!r}: "
                    f"{overlap_count} chunk(s) overlap",
                    reason_code="chunk_overlap",
                )

            duplicates_dropped = (
                self._drop_duplicate_plans(db, duplicate_plans) if overlap_count else 0
            )
            candidate_where = " AND ".join((*conditions, "namespace <> ?"))
            chunks_moved = db.execute(
                f"UPDATE chunks SET namespace=? WHERE {candidate_where}",
                (namespace, *filter_params, namespace),
            ).rowcount
            result = NamespaceAssignResult(
                chunks_moved=chunks_moved,
                duplicates_dropped=duplicates_dropped,
            )
            db.execute(f"RELEASE {_ASSIGN_SAVEPOINT}")
            if owns_txn:
                db.commit()
            return result
        except NamespaceConflictError:
            self._undo_namespace_write(
                db,
                savepoint=_ASSIGN_SAVEPOINT,
                owns_txn=owns_txn,
                operation="assign_namespace",
            )
            raise
        except Exception as exc:
            self._undo_namespace_write(
                db,
                savepoint=_ASSIGN_SAVEPOINT,
                owns_txn=owns_txn,
                operation="assign_namespace",
            )
            raise StorageError(f"assign_namespace failed, transaction rolled back: {exc}") from exc
