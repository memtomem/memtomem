"""One-shot DB repair of non-canonical chunk timestamps (#2203).

``created_at`` / ``updated_at`` are compared lexically, so stored rows must be
UTC in the exact shape ``utc_stamp`` renders. Rows written before the
write-boundary fix could carry an imported bundle's offset, no offset at all
(a naive value), or SQLite's space-separated ``CURRENT_TIMESTAMP`` shape from
the old scope-move UPDATE. ``_repair_non_utc_chunk_timestamps`` in
``create_tables`` rewrites them once per database.

These mirror the tags-repair tests: seed AFTER the first ``create_tables``
pass, clear the idempotency marker, then re-init so the migration runs against
the seeded rows.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
import sqlite_vec

from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import (
    _TS_REPAIR_BATCH,
    _repair_non_utc_chunk_timestamps,
    create_tables,
)

_MARKER_KEY = "chunk_timestamps_utc_repair_v1"

_CLEAN = "2026-01-01T12:00:00.000000+00:00"


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _initialize(db: sqlite3.Connection) -> None:
    meta = MetaManager(lambda: db)
    create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")


def _insert_chunk(
    db: sqlite3.Connection, chunk_id: str, *, created_at: str, updated_at: str | None = None
) -> None:
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
        "tags, created_at, updated_at) VALUES (?, '', ?, '', 'default', '[]', ?, ?)",
        (chunk_id, chunk_id, created_at, updated_at or created_at),
    )


def _rerun_migration(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM _memtomem_meta WHERE key=?", (_MARKER_KEY,))
    db.commit()
    _initialize(db)


def _stored(db: sqlite3.Connection, chunk_id: str) -> tuple[str, str]:
    row = db.execute("SELECT created_at, updated_at FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    return row[0], row[1]


class FailOnUpdate:
    """Proxy that lets the repair's Nth ``UPDATE`` through, then raises.

    Failing on the *second* update is what makes the rollback observable: the
    first one has already been applied inside the transaction, so a missing
    rollback leaves that row rewritten.
    """

    def __init__(self, db: sqlite3.Connection, *, fail_on: int = 2) -> None:
        self._db = db
        self._fail_on = fail_on
        self.updates = 0

    def execute(self, sql: str, *args):
        if sql.startswith("UPDATE chunks SET created_at"):
            self.updates += 1
            if self.updates >= self._fail_on:
                raise sqlite3.OperationalError("boom")
        return self._db.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._db, name)


class TestRepairNonUtcChunkTimestamps:
    def test_offset_row_rewritten_with_instant_preserved(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "offset-1", created_at="2026-01-01T00:00:00+09:00")
            _rerun_migration(db)
            created, updated = _stored(db, "offset-1")
            assert created == "2025-12-31T15:00:00.000000+00:00"
            assert updated == "2025-12-31T15:00:00.000000+00:00"
            # Same instant, different rendering.
            assert datetime.fromisoformat(created) == datetime(
                2025, 12, 31, 15, 0, tzinfo=timezone.utc
            )
        finally:
            db.close()

    def test_naive_row_read_as_utc(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "naive-1", created_at="2026-01-01T00:00:00")
            _rerun_migration(db)
            created, _ = _stored(db, "naive-1")
            assert created == "2026-01-01T00:00:00.000000+00:00"
        finally:
            db.close()

    def test_z_suffix_row_converted(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "zulu-1", created_at="2026-01-01T00:00:00Z")
            _rerun_migration(db)
            created, _ = _stored(db, "zulu-1")
            assert created == "2026-01-01T00:00:00.000000+00:00"
        finally:
            db.close()

    def test_current_timestamp_shape_row_converted(self) -> None:
        """The shape the old scope-move UPDATE actually wrote: SQLite's
        ``CURRENT_TIMESTAMP`` is space-separated, offset-less, second
        precision. It ends in neither an offset nor ``T``-separated digits, so
        only a full parse-and-rerender scan catches it."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "sqlite-1", created_at=_CLEAN, updated_at="2026-02-03 04:05:06")
            _rerun_migration(db)
            created, updated = _stored(db, "sqlite-1")
            assert created == _CLEAN  # clean column untouched
            assert updated == "2026-02-03T04:05:06.000000+00:00"
        finally:
            db.close()

    def test_noncanonical_utc_suffix_row_converted(self) -> None:
        """Ends in ``+00:00`` yet is not canonical — the case a suffix
        pre-filter would have skipped forever once the marker was set."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "space-1", created_at="2026-01-01 00:00:00+00:00")
            _rerun_migration(db)
            created, _ = _stored(db, "space-1")
            assert created == "2026-01-01T00:00:00.000000+00:00"
        finally:
            db.close()

    def test_clean_row_byte_identical(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "clean-1", created_at=_CLEAN)
            _rerun_migration(db)
            assert _stored(db, "clean-1") == (_CLEAN, _CLEAN)
        finally:
            db.close()

    def test_updated_at_only_bad_row_repaired(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "half-1", created_at=_CLEAN, updated_at="2026-01-02T00:00:00+09:00")
            _rerun_migration(db)
            created, updated = _stored(db, "half-1")
            assert created == _CLEAN
            assert updated == "2026-01-01T15:00:00.000000+00:00"
        finally:
            db.close()

    def test_garbage_row_skipped_and_migration_completes(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "bad-1", created_at="not a timestamp")
            _insert_chunk(db, "offset-2", created_at="2026-01-01T00:00:00+09:00")
            _rerun_migration(db)  # must not raise
            assert _stored(db, "bad-1") == ("not a timestamp", "not a timestamp")
            # The sibling row is still repaired and the marker is set.
            created, _ = _stored(db, "offset-2")
            assert created == "2025-12-31T15:00:00.000000+00:00"
            marker = db.execute(
                "SELECT value FROM _memtomem_meta WHERE key=?", (_MARKER_KEY,)
            ).fetchone()
            assert marker is not None and marker[0] == "done"
        finally:
            db.close()

    def test_a_corrupt_sibling_column_does_not_deny_the_other_its_repair(self) -> None:
        """Sharing one parse between the two columns meant a corrupt
        ``updated_at`` left a fixable ``created_at`` lexically wrong — and the
        marker is set either way, so that row would never be revisited."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(
                db, "mixed-1", created_at="2026-01-01T00:00:00+09:00", updated_at="garbage"
            )
            _rerun_migration(db)
            created, updated = _stored(db, "mixed-1")
            assert created == "2025-12-31T15:00:00.000000+00:00"
            assert updated == "garbage"
        finally:
            db.close()

    def test_repairs_beyond_one_batch(self) -> None:
        """The walk is batched by rowid, so a store larger than one batch must
        still be repaired end to end — an off-by-one in the cursor would
        silently leave the tail behind."""
        db = _connect()
        try:
            _initialize(db)
            total = _TS_REPAIR_BATCH + 5
            for i in range(total):
                _insert_chunk(db, f"batch-{i}", created_at="2026-01-01T00:00:00+09:00")
            _rerun_migration(db)
            remaining = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE created_at != ?",
                ("2025-12-31T15:00:00.000000+00:00",),
            ).fetchone()[0]
            assert remaining == 0
        finally:
            db.close()

    def test_an_out_of_range_value_is_left_untouched(self) -> None:
        """``0001-01-01T00:00:00+01:00`` parses and then overflows shifting to
        UTC — it moves before ``datetime.min``. Uncaught, one such row would
        fail every startup, not just skip itself."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "edge-1", created_at="0001-01-01T00:00:00+01:00")
            _rerun_migration(db)  # must not raise
            assert _stored(db, "edge-1")[0] == "0001-01-01T00:00:00+01:00"
        finally:
            db.close()

    def test_idempotent_marker_short_circuits(self) -> None:
        """Rows added after the run are not re-scanned — later writes go
        through the fixed writer, not a re-scan."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "early-1", created_at="2026-01-01T00:00:00+09:00")
            _rerun_migration(db)

            _insert_chunk(db, "late-1", created_at="2026-06-01T00:00:00+09:00")
            db.commit()
            _initialize(db)  # marker still "done" → no-op
            assert _stored(db, "late-1")[0] == "2026-06-01T00:00:00+09:00"
        finally:
            db.close()

    def test_a_failure_mid_repair_rolls_back_rows_and_marker(self) -> None:
        """The marker is written inline rather than through ``set_meta``
        (which commits), so it lands in the same transaction as the rewrites.
        A crash between the two would otherwise record the repair as done with
        rows still unrepaired — and the marker denies them a second chance."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "rollback-1", created_at="2026-01-01T00:00:00+09:00")
            _insert_chunk(db, "rollback-2", created_at="2026-01-01T00:00:00+09:00")
            db.execute("DELETE FROM _memtomem_meta WHERE key=?", (_MARKER_KEY,))
            db.commit()

            # A proxy, not a monkeypatch: ``sqlite3.Connection.execute`` is
            # read-only, and every statement other than the failing one still
            # has to reach the real connection for the rollback to mean
            # anything.
            failed = FailOnUpdate(db)
            meta = MetaManager(lambda: failed)
            with pytest.raises(sqlite3.OperationalError):
                _repair_non_utc_chunk_timestamps(failed, meta)

            assert failed.updates == 2
            # Neither the already-applied row nor the marker survived.
            assert _stored(db, "rollback-1")[0] == "2026-01-01T00:00:00+09:00"
            assert _stored(db, "rollback-2")[0] == "2026-01-01T00:00:00+09:00"
            assert (
                db.execute(
                    "SELECT value FROM _memtomem_meta WHERE key=?", (_MARKER_KEY,)
                ).fetchone()
                is None
            )

            # And the next startup still repairs it.
            _initialize(db)
            assert _stored(db, "rollback-1")[0] == "2025-12-31T15:00:00.000000+00:00"
            assert _stored(db, "rollback-2")[0] == "2025-12-31T15:00:00.000000+00:00"
        finally:
            db.close()

    def test_running_twice_is_stable(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "twice-1", created_at="2026-01-01T00:00:00+09:00")
            _rerun_migration(db)
            first = _stored(db, "twice-1")
            _rerun_migration(db)
            assert _stored(db, "twice-1") == first
        finally:
            db.close()
