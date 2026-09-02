"""Entity storage mixin — CRUD for chunk_entities table."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem.errors import StorageError
from memtomem.storage.sqlite_scope import scope_context_sql

if TYPE_CHECKING:
    from memtomem.models import Chunk, ScopeFilter

# ``_memtomem_meta`` key for the one-time entity backfill (#2133). Values:
# a decimal ``chunks.rowid`` cursor while the walk is in progress, ``done``
# once every pre-existing chunk has had an extraction attempt, or ``stale``
# when content was written while ``indexing.extract_entities`` was off *after*
# ``done`` — a machine-readable record that coverage has a gap the write path
# was told not to fill. The backfill itself never re-runs on ``stale`` (the
# gap is the user's explicit opt-out; the documented remediation is
# ``mem_entity_scan``); consumers that need full coverage gate on ``done``.
_ENTITY_BACKFILL_KEY = "entity_backfill_v1"
ENTITY_BACKFILL_DONE = "done"
ENTITY_BACKFILL_STALE = "stale"


class EntityMixin:
    """Mixin providing entity extraction storage methods.

    Requires ``self._get_db()`` and ``self._in_transaction`` from the backend:
    the write methods gate their commit/rollback on ``_in_transaction`` so they
    compose under the backend's ``transaction()`` context manager.
    """

    async def upsert_entities(self, chunk_id: str, entities: list[dict]) -> int:
        """Insert entities for a chunk. Replaces existing entities if any."""
        if not entities:
            return 0
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Build params before touching the DB: a malformed entity dict (missing a
        # required key) must raise BEFORE the DELETE, never mid-transaction (#1572).
        rows = [
            (
                chunk_id,
                e["entity_type"],
                e["entity_value"],
                e.get("confidence", 1.0),
                e.get("position", 0),
                now,
            )
            for e in entities
        ]

        try:
            # The rollback itself lives in the context manager (#2167), which
            # also covers a ``BaseException`` this handler would miss; the
            # ``except`` below stays for the ``StorageError`` translation that
            # callers of this method match on.
            with self._rolls_back_if_standalone(db):
                # Overwrite mode: replace this chunk's entities atomically.
                db.execute("DELETE FROM chunk_entities WHERE chunk_id = ?", (chunk_id,))
                # Targeted conflict clause, not ``INSERT OR IGNORE`` (#2145): two
                # rows differing only in the case of ``entity_value`` are one
                # mention as far as ``idx_entities_unique`` — and the Stage-7b
                # boost, which matches ``COLLATE NOCASE`` — are concerned, so the
                # second is dropped rather than raising mid-transaction. Naming the
                # conflict target keeps *only* that collision silent: ``OR IGNORE``
                # would swallow a ``NOT NULL`` violation too, and since the DELETE
                # above has already run, a malformed row would then leave the chunk
                # with fewer entities than it had and no error to say so.
                cur = db.executemany(
                    "INSERT INTO chunk_entities (chunk_id, entity_type, entity_value, "
                    "confidence, position, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(chunk_id, entity_type, entity_value COLLATE NOCASE) DO NOTHING",
                    rows,
                )
                inserted = cur.rowcount
                self._commit_if_standalone(db)
        except Exception as exc:
            raise StorageError(f"upsert_entities failed, transaction rolled back: {exc}") from exc
        # Rows actually stored, not rows offered: a caller counting entities
        # would otherwise over-report the ones the conflict clause collapsed.
        return inserted

    async def filter_persisted_chunk_ids(self, chunk_ids: list[str]) -> set[str]:
        """Return which of ``chunk_ids`` actually have a ``chunks`` row.

        ``chunk_entities`` is a child of ``chunks`` under an enforced foreign
        key, so a writer that extracts entities alongside a chunk write has to
        know which of the chunks it handed over were really stored. Not all of
        them necessarily were: ``upsert_chunks`` inserts ``OR IGNORE`` against
        the #691 uniqueness index, so when two processes index the same file
        concurrently the loser's freshly-generated id is dropped on the floor —
        its content lives on under the winner's id, and the winner's own pass
        writes the entities for it. Inserting for the dropped id would raise
        ``FOREIGN KEY constraint failed`` and roll back the whole write.

        Reads through the writer connection deliberately: the caller is
        typically *inside* the chunk-write transaction, and the read pool's
        separate connections cannot see rows that transaction has not committed
        yet — from there every id would look absent.
        """
        if not chunk_ids:
            return set()
        db = self._get_db()
        found: set[str] = set()
        # Chunked to stay under SQLite's 999-host-parameter limit on older
        # builds, matching the batching in the schema migrations.
        batch_size = 500
        for i in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[i : i + batch_size]
            marks = ",".join("?" * len(batch))
            found.update(
                row[0]
                for row in db.execute(
                    f"SELECT id FROM chunks WHERE id IN ({marks})", batch
                ).fetchall()
            )
        return found

    async def delete_entities_for_chunk(self, chunk_id: str) -> int:
        db = self._get_db()
        try:
            # Rollback in the context manager (#2167); the ``except`` is the
            # ``StorageError`` translation callers match on.
            with self._rolls_back_if_standalone(db):
                cur = db.execute("DELETE FROM chunk_entities WHERE chunk_id = ?", (chunk_id,))
                self._commit_if_standalone(db)
        except Exception as exc:
            raise StorageError(
                f"delete_entities_for_chunk failed, transaction rolled back: {exc}"
            ) from exc
        return cur.rowcount

    async def list_chunks_missing_entities(
        self, *, after_rowid: int = 0, limit: int = 500
    ) -> list[tuple[int, Chunk]]:
        """One page of chunks that have no ``chunk_entities`` rows at all.

        The one-time backfill's read primitive (#2133). Keyed on ``rowid``
        rather than OFFSET so the walk stays stable while the backfill's own
        writes shrink the "missing" set between pages — an OFFSET would slide
        over rows as earlier ones gain entities. "Missing" is row-presence,
        deliberately: there is no per-chunk "extraction attempted" marker, so a
        chunk whose content yields no entities is indistinguishable here from
        one never visited. The caller's completion flag, not this predicate, is
        what stops entity-less chunks being re-walked forever.

        Reads the committed state through the read pool; the backfill re-checks
        each page on the writer connection inside its transaction before
        writing (``filter_unextracted_chunk_ids``).
        """
        db = self._get_read_db()
        rows = db.execute(
            "SELECT chunks.rowid, chunks.* FROM chunks "
            "WHERE chunks.rowid > ? AND NOT EXISTS "
            "(SELECT 1 FROM chunk_entities e WHERE e.chunk_id = chunks.id) "
            "ORDER BY chunks.rowid LIMIT ?",
            (after_rowid, limit),
        ).fetchall()
        return [(row[0], self._row_to_chunk(row[1:])) for row in rows]

    async def filter_unextracted_chunk_ids(self, chunk_ids: list[str]) -> set[str]:
        """Return which of ``chunk_ids`` still have zero ``chunk_entities`` rows.

        The backfill's in-transaction re-check. Selection happens on the read
        pool before the write lock is taken, so a concurrent ``mem_entity_scan``
        (possibly an LLM pass, strictly richer than the regex extractor) can
        populate a selected chunk in between — and ``upsert_entities`` deletes
        before inserting, so writing that chunk anyway would replace the richer
        rows. Reading through the writer connection under the transaction sees
        everything committed before the write lock, and nothing else can commit
        while it is held.
        """
        if not chunk_ids:
            return set()
        db = self._get_db()
        extracted: set[str] = set()
        # Chunked under SQLite's 999-host-parameter floor, like
        # ``filter_persisted_chunk_ids`` above.
        batch_size = 500
        for i in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[i : i + batch_size]
            marks = ",".join("?" * len(batch))
            extracted.update(
                row[0]
                for row in db.execute(
                    f"SELECT DISTINCT chunk_id FROM chunk_entities WHERE chunk_id IN ({marks})",
                    batch,
                ).fetchall()
            )
        return {cid for cid in chunk_ids if cid not in extracted}

    async def entity_backfill_get_state(self) -> str | None:
        """Read the ``entity_backfill_v1`` marker (cursor, ``done``, ``stale``)."""
        db = self._get_read_db()
        row = db.execute(
            "SELECT value FROM _memtomem_meta WHERE key = ?", (_ENTITY_BACKFILL_KEY,)
        ).fetchone()
        return row[0] if row else None

    async def entity_backfill_set_state(self, value: str) -> None:
        """Write the ``entity_backfill_v1`` marker.

        Direct SQL rather than ``MetaManager.set_meta``: the backend constructs
        its manager with the standalone commit, so a ``set_meta`` inside an
        owned ``transaction()`` would end the owner's transaction and trip the
        #2168 ownership guard. The ``stale`` arming runs inside the indexer's
        chunk-write transaction, so this write has to compose the same way the
        other mixin writers do.
        """
        db = self._get_db()
        try:
            with self._rolls_back_if_standalone(db):
                db.execute(
                    "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
                    (_ENTITY_BACKFILL_KEY, value),
                )
                self._commit_if_standalone(db)
        except Exception as exc:
            raise StorageError(
                f"entity_backfill_set_state failed, transaction rolled back: {exc}"
            ) from exc

    async def search_entities(
        self,
        entity_type: str | None = None,
        value: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
        *,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
    ) -> list[dict]:
        """Search entities, optionally filtered by type, value substring, and namespace.

        The ADR-0011 scope-context fragment is appended unconditionally, the
        same way the search and recall paths append it: this query joins
        ``chunks``, so without it an entity search inside one project returns
        every other project's people, decisions and action items (#2194).
        ``scope_filter`` narrows further, or opts into a deliberate
        cross-project read; ``project_context_root`` is what the default merge
        resolves "this project" against.
        """
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

        scope_frag, scope_params = scope_context_sql(
            scope_filter, project_context_root, column_alias="c."
        )
        query += f"AND ({scope_frag}) "
        params.extend(scope_params)

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

    async def get_extracted_chunk_ids(self, chunk_ids: list[str]) -> set[str]:
        """Return the subset of chunk_ids that already have extracted entities."""
        if not chunk_ids:
            return set()
        from memtomem.storage.sqlite_helpers import placeholders

        db = self._get_read_db()
        ph = placeholders(len(chunk_ids))
        rows = db.execute(
            f"SELECT DISTINCT chunk_id FROM chunk_entities WHERE chunk_id IN ({ph})",
            chunk_ids,
        ).fetchall()
        return {r[0] for r in rows}

    async def get_matching_entities(
        self,
        chunk_ids: list[str],
        entity_keys: list[tuple[str, str]],
        min_confidence: float = 0.0,
    ) -> dict[str, set[tuple[str, str]]]:
        """Return, per chunk, which of ``entity_keys`` that chunk carries.

        Powers the Stage-7b entity-match boost. Inverted on purpose: only rows
        matching a query entity come back, so the transfer is bounded by
        ``len(entity_keys) * len(chunk_ids)`` tiny tuples rather than every
        entity of every candidate (``decision``/``action_item`` values run to
        200 chars).

        Args:
            chunk_ids: Candidate chunk ids (already namespace/scope-filtered by
                the caller — this query does not re-join ``chunks``).
            entity_keys: ``(entity_type, lowercased value)`` pairs to look for.
            min_confidence: Ignore stored rows below this confidence.

        Returns:
            ``chunk_id`` → set of matched ``(entity_type, lowercased value)``.
            Chunks with no match are absent from the mapping.

        Matching is exact and case-insensitive (``COLLATE NOCASE``, ASCII-only —
        adequate for this entity vocabulary), never substring: a substring match
        would let query entity "git" hit stored "github" and every value
        containing it. ``DISTINCT`` collapses matches across candidate chunks
        into one ``(type, value)`` per query entity, so the caller counts
        distinct keys, never rows. Per-chunk duplicates no longer reach here at
        all — ``idx_entities_unique`` folds them at write time (#2145).
        """
        if not chunk_ids or not entity_keys:
            return {}
        from memtomem.storage.sqlite_helpers import placeholders

        db = self._get_read_db()
        ph = placeholders(len(chunk_ids))
        key_clause = " OR ".join(
            ["(entity_type = ? AND entity_value = ? COLLATE NOCASE)"] * len(entity_keys)
        )
        params: list[object] = [*chunk_ids, min_confidence]
        for entity_type, value in entity_keys:
            params.extend((entity_type, value))

        rows = db.execute(
            f"SELECT DISTINCT chunk_id, entity_type, lower(entity_value) "
            f"FROM chunk_entities "
            f"WHERE chunk_id IN ({ph}) AND confidence >= ? AND ({key_clause})",
            params,
        ).fetchall()

        matches: dict[str, set[tuple[str, str]]] = {}
        for chunk_id, entity_type, value in rows:
            matches.setdefault(chunk_id, set()).add((entity_type, value))
        return matches

    async def get_entity_type_counts(self) -> dict[str, int]:
        """Return count of entities per type."""
        db = self._get_read_db()
        rows = db.execute(
            "SELECT entity_type, COUNT(*) FROM chunk_entities GROUP BY entity_type ORDER BY COUNT(*) DESC"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
