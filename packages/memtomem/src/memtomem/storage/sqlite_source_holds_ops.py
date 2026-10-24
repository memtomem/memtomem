"""Source visibility and hold operations for the SQLite backend."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Callable, ContextManager

from memtomem.errors import TransactionOwnedError
from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY
from memtomem.storage.source_holds import ABSENCE_HOLD_REASONS, REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_helpers import fold_path_form, norm_path, utc_stamp
from memtomem.storage.sqlite_meta import _standalone_commit, _standalone_write_guard


class SourceHoldOps:
    """Manages ``held_sources``, ``pending_source_checks``, and visibility generations.

    ``commit`` and ``write_guard`` let the owning backend route this manager's
    writes through its transaction-ownership machinery
    (``_commit_if_standalone`` / ``_rolls_back_if_standalone``), so a hold
    mutation inside an owned ``transaction()`` neither ends the owner's
    transaction early (#2158) nor strands a failed one (#2167).
    """

    def __init__(
        self,
        get_db: Callable[[], sqlite3.Connection],
        *,
        commit: Callable[[sqlite3.Connection], None] = _standalone_commit,
        write_guard: Callable[[sqlite3.Connection], ContextManager[None]] = _standalone_write_guard,
        in_transaction: Callable[[], bool] = lambda: False,
        lease_read_db: Callable[[], AsyncContextManager[sqlite3.Connection]] | None = None,
        lease_txn_read_db: Callable[[], AsyncContextManager[sqlite3.Connection]] | None = None,
        check_schema_current: Callable[..., None] | None = None,
        sqlite_path_getter: Callable[[], str | Path] | None = None,
    ) -> None:
        self._get_db = get_db
        self._commit = commit
        self._write_guard = write_guard
        self._in_transaction_fn = in_transaction
        self._lease_read_db_fn = lease_read_db
        self._lease_txn_read_db_fn = lease_txn_read_db
        self._check_schema_current_fn = check_schema_current
        self._sqlite_path_getter = sqlite_path_getter

    @property
    def _in_transaction(self) -> bool:
        return self._in_transaction_fn()

    @asynccontextmanager
    async def _lease_read_db(self) -> AsyncIterator[sqlite3.Connection]:
        if self._lease_read_db_fn is not None:
            async with self._lease_read_db_fn() as db:
                yield db
        else:
            yield self._get_db()

    @asynccontextmanager
    async def _lease_txn_read_db(self) -> AsyncIterator[sqlite3.Connection]:
        if self._in_transaction:
            yield self._get_db()
            return
        if self._lease_txn_read_db_fn is not None:
            async with self._lease_txn_read_db_fn() as db:
                yield db
        elif self._lease_read_db_fn is not None:
            async with self._lease_read_db_fn() as db:
                yield db
        else:
            yield self._get_db()

    @staticmethod
    def _bump_source_visibility(db: sqlite3.Connection, source: str) -> None:
        db.execute(
            "INSERT INTO _memtomem_meta(key, value) VALUES ('source_visibility_epoch', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
        )
        db.execute(
            "INSERT INTO source_visibility_generations(source_file, generation) VALUES (?, 1) "
            "ON CONFLICT(source_file) DO UPDATE SET generation=generation+1",
            (source,),
        )

    @classmethod
    def _clear_source_visibility(cls, db: sqlite3.Connection, source_file: Path | str) -> bool:
        # Strings from chunks.source_file have already been normalized on write.
        source = source_file if isinstance(source_file, str) else norm_path(source_file)
        held = db.execute("DELETE FROM held_sources WHERE source_file=?", (source,)).rowcount
        pending = db.execute(
            "DELETE FROM pending_source_checks WHERE source_file=?", (source,)
        ).rowcount
        changed = bool(held or pending)
        if changed:
            cls._bump_source_visibility(db, source)
        return changed

    async def hold_source(self, source_file: Path, reason: str) -> bool:
        """Hide existing source chunks without destroying them (#2498).

        A ``redaction_blocked`` hold keeps its reason when an availability
        reason arrives later (#2629): the file being missing or outside the
        roots says nothing about whether its content may be served again, and
        the out-of-root recheck lifts availability holds on presence alone.
        Moving a hold *to* that reason advances the source's visibility
        generation, so a release prepared before the block cannot lift it.
        """
        # A stale network mount can block Path.resolve(); keep that syscall off
        # the event loop for scanner and scheduler callers that reach here.
        source = await asyncio.to_thread(norm_path, source_file)
        db = self._get_db()
        if db.in_transaction and not self._in_transaction:
            raise TransactionOwnedError("hold_source refused an unowned transaction")
        now = utc_stamp(datetime.now(timezone.utc))
        existing = db.execute(
            "SELECT reason FROM held_sources WHERE source_file=?", (source,)
        ).fetchone()
        if existing is not None and (
            existing[0] == reason or existing[0] == REDACTION_BLOCKED_HOLD
        ):
            pending = db.execute(
                "SELECT 1 FROM pending_source_checks WHERE source_file=?", (source,)
            ).fetchone()
            if pending is None:
                return False
        with self._write_guard(db):
            indexed = db.execute(
                "SELECT 1 FROM chunks WHERE source_file=? LIMIT 1", (source,)
            ).fetchone()
            if not indexed:
                changed = self._clear_source_visibility(db, source)
            else:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO held_sources "
                    "(source_file, first_seen_at, last_checked_at, reason) VALUES (?, ?, ?, ?)",
                    (source, now, now, reason),
                ).rowcount
                blocked_now = False
                if not inserted:
                    current = db.execute(
                        "SELECT reason FROM held_sources WHERE source_file=?", (source,)
                    ).fetchone()
                    if (
                        current is not None
                        and current[0] != reason
                        and current[0] != REDACTION_BLOCKED_HOLD
                    ):
                        db.execute(
                            "UPDATE held_sources SET last_checked_at=?, reason=? WHERE source_file=?",
                            (now, reason, source),
                        )
                        blocked_now = reason == REDACTION_BLOCKED_HOLD
                pending = db.execute(
                    "DELETE FROM pending_source_checks WHERE source_file=?", (source,)
                ).rowcount
                changed = bool(inserted or pending or blocked_now)
                if changed:
                    self._bump_source_visibility(db, source)
            self._commit(db)
        return changed

    async def release_source_hold(
        self,
        source_file: Path,
        *,
        expected_generation: int | None = None,
        validated: bool = True,
    ) -> bool:
        """Lift a source's hold; ``validated=False`` for a presence-only release.

        ``validated`` means the caller has just indexed the current content
        cleanly. A caller that only saw the file exist passes ``False``, and a
        ``redaction_blocked`` hold then stays: its file is present, and the
        reason it is held is what the file contains (#2629). The reason is
        read under the writer lock, in the transaction that clears it.
        """
        db = self._get_db()
        source = norm_path(source_file)
        changed = False
        # Refuse before the rollback guard: this implicit transaction belongs
        # to neither this method nor a tracked owner, so rolling it back here
        # would discard someone else's uncommitted work.
        if db.in_transaction and not self._in_transaction:
            raise TransactionOwnedError("release_source_hold refused an unowned transaction")
        with self._write_guard(db):
            # The observer uses a separate SQLite connection. Hold the writer
            # lock while checking this source's generation and clearing rows, so a delete
            # journaled after indexing began cannot be erased here.
            if (expected_generation is not None or not validated) and not self._in_transaction:
                db.execute("BEGIN IMMEDIATE")
            generation_matches = True
            if expected_generation is not None:
                row = db.execute(
                    "SELECT generation FROM source_visibility_generations WHERE source_file=?",
                    (source,),
                ).fetchone()
                generation_matches = (int(row[0]) if row else 0) == expected_generation
            content_blocked = False
            if not validated:
                reason = db.execute(
                    "SELECT reason FROM held_sources WHERE source_file=?", (source,)
                ).fetchone()
                content_blocked = reason is not None and reason[0] == REDACTION_BLOCKED_HOLD
            # An ordinary receipt hit has no visibility row. Skip DELETE so
            # the common path stays read-only.
            if (
                generation_matches
                and not content_blocked
                and db.execute(
                    "SELECT 1 FROM held_sources WHERE source_file=? UNION "
                    "SELECT 1 FROM pending_source_checks WHERE source_file=? LIMIT 1",
                    (source, source),
                ).fetchone()
                is not None
            ):
                changed = self._clear_source_visibility(db, source_file)
            self._commit(db)
        return changed

    async def release_absence_hold(self, source_key: str) -> bool:
        """Show a retained source again while it is hidden only for being gone (#2833).

        ``source_key`` is the stored spelling (a ``held_sources`` or
        ``pending_source_checks`` row, or ``norm_path`` of the file for the
        engine, which writes holds under that key). It is matched as is: a
        stored key re-resolved after a parent became a symlink would name a
        different row and leave this one hidden.

        The held row goes only while its reason is in
        :data:`ABSENCE_HOLD_REASONS`, checked under the writer lock, so a hold
        that has since become ``scan_unavailable`` or ``redaction_blocked``
        stays. The pending row (a journaled delete) goes only when no other
        hold remains, because for a retained source a delete is what the
        archive is for. ``hold_source`` still overwrites one availability
        reason with another, so a concurrent absence hold can turn an
        unavailable hold into a releasable one; that exposes only an archived
        source the user chose to keep, never a redaction hold.
        """
        db = self._get_db()
        if db.in_transaction and not self._in_transaction:
            raise TransactionOwnedError("release_absence_hold refused an unowned transaction")
        placeholders = ",".join("?" for _ in ABSENCE_HOLD_REASONS)
        with self._write_guard(db):
            if not self._in_transaction:
                db.execute("BEGIN IMMEDIATE")
            held = db.execute(
                f"DELETE FROM held_sources WHERE source_file=? AND reason IN ({placeholders})",
                (source_key, *sorted(ABSENCE_HOLD_REASONS)),
            ).rowcount
            pending = 0
            if (
                db.execute(
                    "SELECT 1 FROM held_sources WHERE source_file=?", (source_key,)
                ).fetchone()
                is None
            ):
                pending = db.execute(
                    "DELETE FROM pending_source_checks WHERE source_file=?", (source_key,)
                ).rowcount
            changed = bool(held or pending)
            if changed:
                self._bump_source_visibility(db, source_key)
            self._commit(db)
        return changed

    async def get_held_sources(self) -> list[Path]:
        async with self._lease_read_db() as db:
            return [
                Path(row[0])
                for row in db.execute(
                    "SELECT h.source_file FROM held_sources h "
                    "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.source_file=h.source_file) "
                    "ORDER BY h.first_seen_at, h.source_file"
                ).fetchall()
            ]

    async def get_hidden_source_files(self) -> set[Path]:
        """Direct holds plus policy summaries derived from hidden sources."""
        async with self._lease_read_db() as db:
            rows = db.execute(
                "SELECT source_file FROM held_sources "
                "UNION SELECT source_file FROM pending_source_checks "
                "UNION SELECT DISTINCT c.source_file FROM chunks c "
                "WHERE c.origin=? AND ("
                "EXISTS (SELECT 1 FROM held_sources h WHERE h.source_file="
                "substr(c.source_file, 1, length(c.source_file) - length(?))) "
                "OR EXISTS (SELECT 1 FROM pending_source_checks p WHERE p.source_file="
                "substr(c.source_file, 1, length(c.source_file) - length(?))))",
                (ORIGIN_CONSOLIDATION_POLICY, CONSOLIDATED_SUFFIX, CONSOLIDATED_SUFFIX),
            ).fetchall()
            return {Path(row[0]) for row in rows}

    async def is_source_hidden(self, source_file: Path) -> bool:
        """Include a policy summary's parent in path-level visibility.

        ``source_file`` is compared as stored, like :meth:`is_source_held`, and
        read the same way.
        """
        async with self._lease_txn_read_db() as db:
            source = str(source_file)
            if (
                db.execute(
                    "SELECT 1 FROM held_sources WHERE source_file=? UNION "
                    "SELECT 1 FROM pending_source_checks WHERE source_file=? LIMIT 1",
                    (source, source),
                ).fetchone()
                is not None
            ):
                return True
            if not source.endswith(CONSOLIDATED_SUFFIX):
                return False
            parent = source[: -len(CONSOLIDATED_SUFFIX)]
            return (
                db.execute(
                    "SELECT 1 FROM chunks c WHERE c.source_file=? AND c.origin=? AND ("
                    "EXISTS (SELECT 1 FROM held_sources h WHERE h.source_file=?) OR "
                    "EXISTS (SELECT 1 FROM pending_source_checks p WHERE p.source_file=?)) LIMIT 1",
                    (source, ORIGIN_CONSOLIDATION_POLICY, parent, parent),
                ).fetchone()
                is not None
            )

    async def is_source_held(self, source_file: Path) -> bool:
        """Whether a stored source path is held or pending, compared as stored.

        Callers pass a path read back from the store. Resolving it again would
        follow a symlink placed at a held source's path to some other file and
        miss the hold; a caller holding a filesystem path normalises it first.

        Inside the caller's own ``transaction()`` this reads the writer. A pool
        connection with another thread's statement still open keeps an older
        snapshot, so a check meant to run under the write lock could otherwise
        miss a hold committed before the lock was taken.
        """
        async with self._lease_txn_read_db() as db:
            source = str(source_file)
            return (
                db.execute(
                    "SELECT 1 FROM held_sources WHERE source_file=? UNION "
                    "SELECT 1 FROM pending_source_checks WHERE source_file=? LIMIT 1",
                    (source, source),
                ).fetchone()
                is not None
            )

    async def is_source_pending(self, source_file: Path) -> bool:
        """Take a filesystem path and normalise it as ``hold_source`` keys rows.

        Unlike :meth:`is_source_held`, whose callers pass stored paths.
        """
        async with self._lease_read_db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM pending_source_checks WHERE source_file=?",
                    (norm_path(source_file),),
                ).fetchone()
                is not None
            )

    async def source_visibility_epoch(self) -> int:
        async with self._lease_read_db() as db:
            row = db.execute(
                "SELECT value FROM _memtomem_meta WHERE key='source_visibility_epoch'"
            ).fetchone()
            return int(row[0]) if row else 0

    async def source_hold_generation(self, source_file: Path) -> int:
        """Take a filesystem path, normalised like :meth:`is_source_pending`."""
        async with self._lease_read_db() as db:
            row = db.execute(
                "SELECT generation FROM source_visibility_generations WHERE source_file=?",
                (norm_path(source_file),),
            ).fetchone()
            return int(row[0]) if row else 0

    async def source_hold_snapshot(self, source_file: Path) -> int | None:
        """Return one source's generation only when it is hidden.

        Visibility and generation come from one SQLite statement so a watcher
        delete cannot slip between separate reads at index start. Takes the
        filesystem path being indexed, normalised like :meth:`is_source_pending`.
        """
        async with self._lease_read_db() as db:
            source = norm_path(source_file)
            row = db.execute(
                "SELECT COALESCE((SELECT generation FROM source_visibility_generations "
                "WHERE source_file=?), 0) "
                "WHERE EXISTS (SELECT 1 FROM held_sources WHERE source_file=?) "
                "OR EXISTS (SELECT 1 FROM pending_source_checks WHERE source_file=?)",
                (source, source, source),
            ).fetchone()
            return int(row[0]) if row else None

    def queue_source_check_sync(self, source_file: Path) -> bool:
        """Journal a watcher delete before its volatile debounce queue accepts it.

        Watchdog invokes handlers on its own thread, so use a short-lived WAL
        writer rather than the task-owned connection. Only indexed paths are
        recorded. Every delete advances the visibility epoch, including a
        repeated event for an already pending path, so an older index pass
        cannot clear the newer event's check.
        """
        # The observer's root was resolved when it was scheduled. A deletion
        # callback must not resolve the path again: a detached network mount
        # can make that syscall block the watchdog thread indefinitely. A
        # symlinked subpath may therefore miss the resolved chunks key; the
        # live queue still receives the event, but restart replay is not
        # guaranteed for that path.
        source = fold_path_form(os.path.abspath(os.path.expanduser(str(source_file))))
        # A contended writer must not park watchdog's observer thread for a
        # full debounce window per event. The event still enters the queue if
        # this best-effort journal write times out.
        sqlite_path = self._sqlite_path_getter() if self._sqlite_path_getter is not None else ""
        db = sqlite3.connect(str(Path(sqlite_path).expanduser()), timeout=0.05)
        try:
            if self._check_schema_current_fn is not None:
                self._check_schema_current_fn(db, force=True)
            db.execute("PRAGMA synchronous=NORMAL")
            if not db.execute(
                "SELECT 1 FROM chunks WHERE source_file=? LIMIT 1", (source,)
            ).fetchone():
                return False
            db.execute(
                "INSERT INTO pending_source_checks(source_file, queued_at) VALUES (?, ?) "
                "ON CONFLICT(source_file) DO UPDATE SET queued_at=excluded.queued_at",
                (source, utc_stamp(datetime.now(timezone.utc))),
            )
            self._bump_source_visibility(db, source)
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    async def get_pending_source_checks(self) -> list[Path]:
        async with self._lease_read_db() as db:
            return [
                Path(row[0])
                for row in db.execute(
                    "SELECT p.source_file FROM pending_source_checks p "
                    "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.source_file=p.source_file) "
                    "ORDER BY p.queued_at, p.source_file"
                ).fetchall()
            ]
