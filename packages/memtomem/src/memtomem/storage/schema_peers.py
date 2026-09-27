"""Refuse a schema migration while an older server may still have the DB open (#2564).

The downgrade fence (:func:`~memtomem.storage.sqlite_schema.check_schema_downgrade`)
only runs when a process opens the database. A server that opened it under an
older schema keeps its connection, so once another process migrates the file
that server goes on reading and writing without the newer invariants. Servers
already released cannot be taught to notice, so the migrating side has to look
before it moves the stamp.

Why a live registration means "older schema" without recording versions: a
server registers in the instance registry only after its storage has opened,
and opening stamps the current schema. So a registration observed *before*
re-reading a stamp that is still below :data:`SCHEMA_VERSION` belongs to a
process that opened the database under that older stamp. The order matters:
reading the stamp first would let a same-version peer migrate and register in
between and be mistaken for an older one.

Not covered: an older server that opens the database between this check and
the migration's commit, and processes that never register (``mm web`` and
short-lived CLI commands).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from memtomem._instance_registry import (
    current_process_id,
    enumerate_live_instances,
    store_digest_for,
)
from memtomem.errors import SchemaMigrationBlockedError
from memtomem.storage.sqlite_schema import SCHEMA_VERSION, read_schema_version


def _is_empty(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None


def _migrates(db: sqlite3.Connection) -> bool:
    """Whether opening this database would move its schema stamp."""
    stored = read_schema_version(db)
    if stored is not None:
        return stored < SCHEMA_VERSION
    # No trusted stamp: a brand-new file has nothing an older server could
    # be holding, while a pre-versioning or corrupt-stamp DB is migrated.
    return not _is_empty(db)


async def refuse_migration_under_live_peers(db: sqlite3.Connection, db_path: Path) -> None:
    """Raise :class:`SchemaMigrationBlockedError` if migrating could strand an older server.

    Costs one SELECT when no migration is due, which is every open but the
    first one after an upgrade. The registry walk runs in a worker thread: it
    takes a bounded cross-process lock that must not block the event loop.
    """
    if not _migrates(db):
        return
    digest = store_digest_for(db_path)
    if digest is None:
        return
    result = await asyncio.to_thread(enumerate_live_instances, digest)
    own = current_process_id()
    peers = sorted({info.pid for info in result.instances if info.procid != own})
    # Re-read after enumerating (see module docstring for why the order matters).
    if not _migrates(db):
        return
    stored = read_schema_version(db)
    current = "an unversioned schema" if stored is None else f"schema version {stored}"
    if peers:
        pids = ", ".join(str(pid) for pid in peers)
        raise SchemaMigrationBlockedError(
            f"This database is at {current}, and opening it would migrate it to "
            f"schema version {SCHEMA_VERSION}. {len(peers)} running memtomem "
            f"server(s) (pid {pids}) have it open under the older schema and would "
            f"keep writing without the newer schema's safeguards. Nothing was "
            f"migrated. Stop those servers, then retry."
        )
    if not result.complete:
        raise SchemaMigrationBlockedError(
            f"This database is at {current}, and opening it would migrate it to "
            f"schema version {SCHEMA_VERSION}, but the server registry could not "
            f"be fully read, so it is unknown whether an older memtomem server "
            f"still has it open. Nothing was migrated. Retry once other memtomem "
            f"processes have finished starting; if it keeps failing, "
            f"'mm upgrade --dry-run' lists the registry problems without stopping "
            f"or installing anything."
        )
