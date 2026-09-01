"""Every ``IN (...)`` sized by a file's or a namespace's own row count (#2265).

``delete_by_source`` bound every row id of a source into one statement, so a
source with more chunks than SQLite's host-parameter ceiling
(``SQLITE_LIMIT_VARIABLE_NUMBER``: 32766 today, 999 on builds older than 3.32)
raised ``too many SQL variables`` instead of deleting. The rollback was clean,
which is what made it quiet: the file simply could not be deleted through
``mm purge``, the web source tab, the health sweep, or re-index replacement.
``_SQL_MAX_PARAMS`` existed for exactly this and the write paths had not
adopted it.

Two kinds of pin, because each is false-green without the other:

* a **real-limit** pin lowers the connection's own ceiling to 999 and drives a
  larger input through — it proves the operation *works*, and fails on the
  unbatched code on every build, but says nothing about the split size;
* a **statement-shape** pin shrinks ``_SQL_MAX_PARAMS`` and counts the
  parameters bound per statement — it proves the split happens where the
  constant says, on inputs small enough to stay affordable.

Same reasoning as ``test_count_chunks_by_sources.py::TestBatching`` and
``test_get_chunks_batch_splits_into_statements_of_the_configured_size``.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from memtomem.errors import StorageError
from memtomem.storage import sqlite_backend, sqlite_namespace
from memtomem.storage.sqlite_helpers import norm_path, serialize_f32

from helpers import make_chunk

pytestmark = pytest.mark.asyncio

# Comfortably past the 999-variable ceiling the pins force, small enough that
# seeding stays a fraction of a second.
_OVER_THE_LIMIT = 1200


def _seed(storage, source: Path, count: int, *, namespace: str = "default") -> list[str]:
    """Insert ``count`` complete rows (chunk + FTS + vector) for one source.

    Raw SQL rather than ``upsert_chunks``: the delete paths under test read
    only these three tables, and going through the writer would make the
    seeding of a delete pin depend on the very batching the *upsert* pins
    exist to check. Sidecar rows are seeded too, so "the sidecars went with
    the chunks" is a real assertion rather than a vacuous one.
    """
    db = storage._get_db()
    ids = [str(uuid4()) for _ in range(count)]
    db.executemany(
        "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
        "start_line, end_line, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
        [
            (
                cid,
                f"row {i}",
                f"h-{uuid4().hex[:12]}",
                norm_path(source),
                namespace,
                i * 10,
                i * 10 + 9,
            )
            for i, cid in enumerate(ids)
        ],
    )
    rowids = [
        row[0]
        for row in db.execute(
            "SELECT rowid FROM chunks WHERE source_file=? ORDER BY rowid",
            (norm_path(source),),
        ).fetchall()
    ]
    db.executemany(
        "INSERT OR REPLACE INTO chunks_fts(rowid, content, source_file) VALUES (?,?,?)",
        [(rowid, f"row {rowid}", norm_path(source)) for rowid in rowids],
    )
    if storage._has_vec_table:
        db.executemany(
            "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?,?)",
            [(rowid, serialize_f32([0.0] * storage._dimension)) for rowid in rowids],
        )
    db.commit()
    return ids


def _force_historic_limit(storage) -> None:
    """Lower every connection to the pre-3.32 host-parameter ceiling.

    Without this the pins are false-green on a modern build, where an
    unbatched ``IN`` of 1200 values simply succeeds (``test_storage.py``
    does the same for ``source_exact``).
    """
    for conn in [storage._get_db(), *storage._read_pool]:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)


def _remaining(storage, source: Path) -> tuple[int, int]:
    """``(chunk rows, FTS rows)`` still stored for *source*."""
    db = storage._get_db()
    chunks = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE source_file=?", (norm_path(source),)
    ).fetchone()[0]
    fts = db.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE source_file=?", (norm_path(source),)
    ).fetchone()[0]
    return chunks, fts


def _orphan_sidecars(storage) -> tuple[int, int]:
    """Sidecar rows whose ``chunks`` row is gone — the batching's silent failure."""
    db = storage._get_db()
    fts = db.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE rowid NOT IN (SELECT rowid FROM chunks)"
    ).fetchone()[0]
    vec = 0
    if storage._has_vec_table:
        vec = db.execute(
            "SELECT COUNT(*) FROM chunks_vec WHERE rowid NOT IN (SELECT rowid FROM chunks)"
        ).fetchone()[0]
    return fts, vec


class _Counting:
    """Connection proxy recording the parameter count of matching statements.

    The ``_Counting`` shape from ``test_storage_extended.py``, extended with an
    optional fault injector so a mid-batch failure can be pinned too.
    """

    def __init__(self, real, match: str, *, fail_on: int | None = None):
        self._real = real
        self._match = match
        self._fail_on = fail_on
        self.seen: list[int] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, params=()):
        if self._match in " ".join(sql.split()):
            self.seen.append(len(params))
            if self._fail_on is not None and len(self.seen) == self._fail_on:
                raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, params)


class TestDeleteBySource:
    """The issue: a source too large to delete."""

    async def test_deletes_a_source_past_the_bound_variable_ceiling(self, storage):
        source = Path("/tmp/huge.md")
        _seed(storage, source, _OVER_THE_LIMIT)
        _force_historic_limit(storage)

        deleted = await storage.delete_by_source(source)

        assert deleted == _OVER_THE_LIMIT
        assert _remaining(storage, source) == (0, 0)
        assert _orphan_sidecars(storage) == (0, 0)

    async def test_splits_each_statement_at_the_configured_size(self, storage, monkeypatch):
        source = Path("/tmp/split.md")
        _seed(storage, source, 7)
        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 3)
        counting = _Counting(storage._get_db(), "DELETE FROM chunks WHERE id IN")
        monkeypatch.setattr(storage, "_get_db", lambda: counting)

        deleted = await storage.delete_by_source(source)

        assert deleted == 7
        assert counting.seen == [3, 3, 1]

    async def test_a_batch_failure_rolls_back_the_batches_before_it(self, storage, monkeypatch):
        """The whole delete is still one transaction (the issue's constraint).

        Batching a statement is only safe while the batches share the caller's
        transaction; a per-batch commit would turn a mid-delete failure into a
        half-deleted source, which is worse than the raise it replaced.
        """
        source = Path("/tmp/partial.md")
        _seed(storage, source, 7)
        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 3)
        real_db = storage._get_db()
        counting = _Counting(real_db, "DELETE FROM chunks WHERE id IN", fail_on=2)
        monkeypatch.setattr(storage, "_get_db", lambda: counting)

        with pytest.raises(StorageError, match="delete_by_source failed"):
            await storage.delete_by_source(source)

        assert _remaining(storage, source) == (7, 7)
        assert not real_db.in_transaction


class TestDeleteChunks:
    """Same shape, reached by id list: an index run's delete bucket, decay, dedup."""

    async def test_deletes_an_id_list_past_the_bound_variable_ceiling(self, storage):
        source = Path("/tmp/many-ids.md")
        ids = _seed(storage, source, _OVER_THE_LIMIT)
        _force_historic_limit(storage)

        deleted = await storage.delete_chunks([UUID(cid) for cid in ids])

        assert deleted == _OVER_THE_LIMIT
        assert _remaining(storage, source) == (0, 0)
        assert _orphan_sidecars(storage) == (0, 0)

    async def test_the_lookup_and_the_delete_are_both_split(self, storage, monkeypatch):
        """The pre-flight ``SELECT ... WHERE id IN`` blew the same ceiling."""
        source = Path("/tmp/split-ids.md")
        ids = _seed(storage, source, 7)
        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 3)
        selects = _Counting(storage._get_db(), "SELECT id, rowid, source_file FROM chunks WHERE")
        monkeypatch.setattr(storage, "_get_db", lambda: selects)
        await storage.delete_chunks([UUID(cid) for cid in ids])
        assert selects.seen == [3, 3, 1]

        ids = _seed(storage, source, 7)
        deletes = _Counting(storage._get_db(), "DELETE FROM chunks_fts WHERE rowid IN")
        monkeypatch.setattr(storage, "_get_db", lambda: deletes)
        await storage.delete_chunks([UUID(cid) for cid in ids])
        assert deletes.seen == [3, 3, 1]

    async def test_a_repeated_id_across_batches_is_counted_once(self, storage, monkeypatch):
        """Splitting the lookup must not turn a repeat into a second row.

        A single ``IN (...)`` collapsed duplicates on its own; batching hands
        the same id to two statements, and the row comes back from each. The
        count is what ``mm purge`` and the web routes report as "deleted N",
        so an inflated one is a wrong number shown to the user.
        """
        source = Path("/tmp/repeats.md")
        ids = _seed(storage, source, 4)
        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 3)

        # ``ids[0]`` twice, far enough apart to straddle the batch boundary.
        requested = [UUID(ids[0]), *[UUID(cid) for cid in ids[1:]], UUID(ids[0])]
        deleted = await storage.delete_chunks(requested)

        assert deleted == 4
        assert _remaining(storage, source) == (0, 0)


class TestUpsertChunks:
    """A file's whole chunk set travels through four ``IN`` clauses here."""

    async def test_inserts_then_updates_a_file_past_the_bound_variable_ceiling(self, storage):
        chunks = [make_chunk(f"body {i}", source="wide.md") for i in range(_OVER_THE_LIMIT)]
        _force_historic_limit(storage)

        inserted = await storage.upsert_chunks(chunks)
        # Second pass with the same ids: the update leg binds its own
        # ``IN`` clauses (existing-rowid lookup, vector refresh).
        for chunk in chunks:
            chunk.content = chunk.content + " revised"
        updated = await storage.upsert_chunks(chunks)

        assert inserted == updated == _OVER_THE_LIMIT
        found = await storage.get_chunks_batch([c.id for c in chunks])
        assert len(found) == _OVER_THE_LIMIT
        assert all(c.content.endswith("revised") for c in found.values())
        assert _orphan_sidecars(storage) == (0, 0)


class TestHashMatchedUpdates:
    """``update_chunk_line_ranges`` / ``update_chunk_metadata``: one file's rows."""

    async def test_line_ranges_and_metadata_past_the_bound_variable_ceiling(self, storage):
        chunks = [make_chunk(f"body {i}", source="shift.md") for i in range(_OVER_THE_LIMIT)]
        await storage.upsert_chunks(chunks)
        _force_historic_limit(storage)

        moved = [
            dataclasses.replace(
                chunk,
                metadata=dataclasses.replace(
                    chunk.metadata,
                    start_line=chunk.metadata.start_line + 5,
                    end_line=chunk.metadata.end_line + 5,
                    tags=("moved",),
                ),
            )
            for chunk in chunks
        ]

        assert await storage.update_chunk_line_ranges(moved) == _OVER_THE_LIMIT
        assert await storage.update_chunk_metadata(moved) == _OVER_THE_LIMIT


class TestScopeForSource:
    """``update_chunks_scope_for_source`` binds ``new_norm`` alongside the rowids."""

    async def test_moves_a_source_past_the_bound_variable_ceiling(self, storage):
        old, new = Path("/tmp/old-scope.md"), Path("/tmp/new-scope.md")
        _seed(storage, old, _OVER_THE_LIMIT)
        _force_historic_limit(storage)

        moved = await storage.update_chunks_scope_for_source(old, new, "user", None)

        assert moved == _OVER_THE_LIMIT
        assert _remaining(storage, old) == (0, 0)
        assert _remaining(storage, new) == (_OVER_THE_LIMIT, _OVER_THE_LIMIT)

    async def test_each_batch_carries_one_extra_parameter(self, storage, monkeypatch):
        """``[new_norm, *rowids]`` — the bound count is the batch size plus one,
        which is why ``_SQL_MAX_PARAMS`` has to stay strictly under the ceiling."""
        old, new = Path("/tmp/old-split.md"), Path("/tmp/new-split.md")
        _seed(storage, old, 7)
        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 3)
        counting = _Counting(
            storage._get_db(), "UPDATE chunks_fts SET source_file=? WHERE rowid IN"
        )
        monkeypatch.setattr(storage, "_get_db", lambda: counting)

        assert await storage.update_chunks_scope_for_source(old, new, "user", None) == 7
        assert counting.seen == [4, 4, 2]


class TestDeleteByNamespace:
    """A namespace's row count is data-sized in exactly the same way."""

    async def test_deletes_a_namespace_past_the_bound_variable_ceiling(self, storage):
        source = Path("/tmp/ns.md")
        _seed(storage, source, _OVER_THE_LIMIT, namespace="bulk")
        _force_historic_limit(storage)

        deleted = await storage.delete_by_namespace("bulk")

        assert deleted == _OVER_THE_LIMIT
        assert _remaining(storage, source) == (0, 0)
        assert _orphan_sidecars(storage) == (0, 0)

    async def test_splits_at_the_namespace_delete_batch(self, storage, monkeypatch):
        source = Path("/tmp/ns-split.md")
        _seed(storage, source, 7, namespace="bulk")
        monkeypatch.setattr(sqlite_namespace, "_DELETE_BATCH", 3)
        counting = _Counting(storage._get_db(), "DELETE FROM chunks WHERE id IN")
        # ``NamespaceOps`` captured the backend's ``_get_db`` at construction,
        # so the proxy has to be injected there rather than on the backend.
        monkeypatch.setattr(storage._ns, "_get_db", lambda: counting)

        assert await storage.delete_by_namespace("bulk") == 7
        assert counting.seen == [3, 3, 1]
