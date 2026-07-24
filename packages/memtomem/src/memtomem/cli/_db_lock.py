"""SQLite write-lock probe shared by destructive CLI commands.

Extracted from ``uninstall_cmd`` (#1574 item 7) so ``mm reset`` can reuse the
``BEGIN IMMEDIATE`` probe without importing the whole uninstall module (which
drags in ``init_cmd``/``RuntimeProfile``). Kept separate from
``cli/_liveness.py`` on purpose: that module is pid-flock probes with no
``sqlite3`` dependency.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


def sqlite_file_uri(db_path: Path, *, mode: str) -> str:
    """``file:`` URI for ``sqlite3.connect(..., uri=True)``.

    Percent-encodes the path (via ``Path.as_uri``) so a filename containing
    ``?`` or ``#`` can't be parsed as a URI query/fragment delimiter — an
    interpolated ``f"file:{db_path}?mode=rw"`` would silently open a
    different, truncated path for such names (Codex review). SQLite decodes
    ``%HH`` escapes in URIs, and ``as_uri`` also normalizes Windows drive
    letters/backslashes.
    """
    return f"{db_path.resolve().as_uri()}?mode={mode}"


@dataclass(frozen=True)
class DbLockState:
    """Result of probing the SQLite DB for an active writer.

    ``locked`` is True only when another connection holds a RESERVED /
    PENDING / EXCLUSIVE lock at probe time — i.e. an active writer.
    Pure readers (SHARED locks only) are not detected; that's an
    accepted tradeoff (see ``check_db_lock``).
    """

    locked: bool
    probe_error: str | None


def check_db_lock(db_path: Path) -> DbLockState:
    """Probe whether another connection holds a write lock on ``db_path``.

    Motivation: the ``.server.pid`` check only catches the MCP
    ``memtomem-server`` entrypoint. ``mm web``, ``mm watchdog``, and any
    user-run sqlite3 connection are invisible to that scheme, so a
    destructive command could silently proceed while a live writer was
    holding the WAL (observed in issue #384).

    Mechanism: open a short-timeout connection and attempt
    ``BEGIN IMMEDIATE`` — that tries to acquire a RESERVED lock and
    raises ``SQLITE_BUSY`` (``sqlite3.OperationalError`` whose message
    contains "locked"/"busy") if any other connection holds
    RESERVED/PENDING/EXCLUSIVE. On success we ``ROLLBACK`` immediately;
    the probe never modifies data.

    Tradeoff: a process that only reads (SHARED lock) does NOT block
    ``BEGIN IMMEDIATE`` in WAL mode, so a quiet-at-probe-time reader
    slips through. That's an accepted tradeoff here — the WAL-corruption
    path (active writer) is the severe case and is what this probe is
    meant to guard. Complete reader-detection would need an ``lsof``
    fallback or an extended pid-file scheme (see issue #384 discussion).

    Error handling: if the probe can't run (file missing, corrupt,
    permission denied, sqlite unavailable), returns ``locked=False`` with
    ``probe_error`` set. The callers are recovery paths (uninstall,
    reset) and must not be blocked by unrelated DB integrity issues.
    """
    try:
        present = db_path.exists()
    except OSError as exc:
        # #1949: on py3.12 ``Path.exists()`` propagates errors outside its
        # ignore-set (e.g. ``EACCES`` when the db path is a link routed
        # through an unsearchable directory) instead of returning False.
        # Fail open with ``probe_error`` set — same contract as every
        # branch below: a recovery caller must not be blocked by an FS
        # fault it can do nothing about, and the header-gate ``open()``
        # would only fail the same way. A genuinely-absent db (ENOENT, in
        # the ignore-set) still returns cleanly with ``probe_error=None``.
        return DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    if not present:
        return DbLockState(locked=False, probe_error=None)

    # Header gate: only probe real SQLite files. Opening a corrupt /
    # non-SQLite file with ``mode=rw`` can still trigger side effects on
    # sibling ``-wal`` / ``-shm`` files (observed: a fake-content WAL
    # got unlinked when SQLite tried to verify it). Stay out of that
    # code path unless the file is actually a SQLite database.
    try:
        with db_path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        return DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    if header != b"SQLite format 3\x00":
        return DbLockState(locked=False, probe_error="not a SQLite database")

    conn: sqlite3.Connection | None = None
    try:
        # mode=rw: don't auto-create if the file vanishes between stat
        # and connect (paranoia for concurrent deletions).
        conn = sqlite3.connect(
            sqlite_file_uri(db_path, mode="rw"),
            uri=True,
            timeout=0.25,
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return DbLockState(locked=False, probe_error=None)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return DbLockState(locked=True, probe_error=None)
        # Other OperationalError (not-a-database, read-only, etc.) — skip
        # probe, let the caller proceed.
        return DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    except (sqlite3.Error, OSError) as exc:
        return DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
