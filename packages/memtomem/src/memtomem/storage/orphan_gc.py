"""Orphan project-tier chunk garbage collection (ADR-0011 follow-up #884).

When a user indexes a directory under ``project_shared`` / ``project_local``
scope and later deletes the directory, the watcher only prunes file-level
deletions *within* tracked roots — root-removal is not a case it sees.
Rows survive in ``~/.memtomem/memtomem.db`` with their now-vanished
``project_root`` still set, bloating the DB and surfacing under explicit
cross-project ``--scope=project_shared`` queries.

This module is the storage-layer half of the fix. It exposes two pure
functions over a ``sqlite3.Connection`` so they can be exercised against
an in-memory DB without booting the full Components stack — the CLI
layer (``mm gc orphan-projects``) wraps them with user-facing dry-run
and confirmation flow.

Boundary rules (matches the #884 review):

* Deletion predicate is strictly
  ``scope IN ('project_shared', 'project_local') AND project_root = ?``.
  User-scope rows that happen to carry a stale ``project_root`` value
  are out of scope for this GC — that is a separate authorship class
  (ADR-0011 §8 "no default flip, ever").
* Cleanup is opt-in. The find pass never mutates; the sweep pass deletes
  one root at a time and is invoked per explicit caller decision.
* All four storage surfaces are cleaned in one transaction: ``chunks``,
  ``chunks_fts``, ``chunks_vec`` (when present), and the
  ``_memtomem_meta`` ``ai_summary:<source>`` cache. Mirrors the pattern
  established by ``SqliteBackend.delete_by_source``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from memtomem.storage.sqlite_helpers import placeholders

logger = logging.getLogger(__name__)

_PROJECT_SCOPES: tuple[str, ...] = ("project_shared", "project_local")
_AI_SUMMARY_KEY_PREFIX = "ai_summary:"
_SAMPLE_SIZE = 3
_BATCH_SIZE = 500


@dataclass(frozen=True)
class OrphanProjectReport:
    """Per-orphan-root summary returned by :func:`find_orphan_project_roots`.

    ``scope_counts`` maps each project scope to the row count under this
    root (only scopes with at least one row are present). ``total_rows``
    is the sum of those counts. ``sample_source_files`` holds up to
    :data:`_SAMPLE_SIZE` example source paths so the CLI can show what
    the user is about to delete.
    """

    project_root: str
    total_rows: int
    scope_counts: dict[str, int]
    sample_source_files: tuple[str, ...]


@dataclass(frozen=True)
class SweepResult:
    """Per-root deletion counts returned by :func:`sweep_orphan_project_root`."""

    project_root: str
    chunks_deleted: int
    fts_deleted: int
    vec_deleted: int
    ai_summaries_deleted: int


def find_orphan_project_roots(db: sqlite3.Connection) -> list[OrphanProjectReport]:
    """Return reports for ``project_root`` values whose path no longer exists.

    Read-only. The check uses :meth:`pathlib.Path.exists` — unmounted
    filesystems and removable disks therefore appear orphaned. The caller
    is expected to gate any subsequent :func:`sweep_orphan_project_root`
    call behind explicit user confirmation; this helper is the
    mechanical detection layer only.

    Results are sorted by ``project_root`` for stable CLI output.
    """
    rows = db.execute(
        """
        SELECT project_root, scope, COUNT(*) AS cnt
        FROM chunks
        WHERE scope IN ('project_shared', 'project_local')
          AND project_root IS NOT NULL
        GROUP BY project_root, scope
        """
    ).fetchall()
    if not rows:
        return []

    by_root: dict[str, dict[str, int]] = {}
    for root, scope, count in rows:
        by_root.setdefault(root, {})[scope] = count

    reports: list[OrphanProjectReport] = []
    for root, scope_counts in by_root.items():
        if Path(root).exists():
            continue
        sample_rows = db.execute(
            """
            SELECT DISTINCT source_file FROM chunks
            WHERE project_root = ?
              AND scope IN ('project_shared', 'project_local')
            ORDER BY source_file
            LIMIT ?
            """,
            (root, _SAMPLE_SIZE),
        ).fetchall()
        reports.append(
            OrphanProjectReport(
                project_root=root,
                total_rows=sum(scope_counts.values()),
                scope_counts=dict(scope_counts),
                sample_source_files=tuple(r[0] for r in sample_rows),
            )
        )
    reports.sort(key=lambda r: r.project_root)
    return reports


def sweep_orphan_project_root(
    db: sqlite3.Connection,
    project_root: str,
    *,
    has_vec_table: bool,
) -> SweepResult:
    """Delete every project-tier chunk under ``project_root`` in one transaction.

    Cleans four surfaces atomically:

    1. ``chunks`` rows where
       ``scope IN ('project_shared','project_local') AND project_root = ?``.
    2. ``chunks_fts`` rows by rowid.
    3. ``chunks_vec`` rows by rowid (only when ``has_vec_table`` is True).
    4. ``_memtomem_meta`` ``ai_summary:<source>`` keys for any source that
       has no remaining chunks after the chunks delete.

    ``BEGIN IMMEDIATE`` is taken before the SELECT so a concurrent
    indexer cannot insert new rows for this ``project_root`` between
    detection and delete. The whole sequence rolls back on any error.

    The caller is responsible for verifying the directory really is
    gone — :func:`find_orphan_project_roots` does that as part of the
    discovery pass, but this function deliberately does not re-check
    so unit tests can drive deletion against a synthetic
    ``project_root`` string without filesystem manipulation.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute(
            """
            SELECT rowid, id, source_file FROM chunks
            WHERE scope IN ('project_shared', 'project_local')
              AND project_root = ?
            """,
            (project_root,),
        ).fetchall()

        if not rows:
            db.execute("COMMIT")
            return SweepResult(project_root, 0, 0, 0, 0)

        ids = [row[1] for row in rows]
        rowids = [row[0] for row in rows]
        affected_sources = {row[2] for row in rows if row[2]}

        # Sidecars first so an interrupted run never leaves an FTS or
        # vec row pointing at a non-existent ``chunks.rowid``. Same
        # ordering as ``_migrate_chunks_uniqueness`` (#691) and
        # ``delete_by_source``.
        fts_deleted = 0
        vec_deleted = 0
        for i in range(0, len(rowids), _BATCH_SIZE):
            batch = rowids[i : i + _BATCH_SIZE]
            ph = placeholders(len(batch))
            cursor = db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({ph})", batch)  # noqa: S608
            fts_deleted += cursor.rowcount or 0
            if has_vec_table:
                cursor = db.execute(
                    f"DELETE FROM chunks_vec WHERE rowid IN ({ph})",  # noqa: S608
                    batch,
                )
                vec_deleted += cursor.rowcount or 0

        # Parent table. The ``scope`` + ``project_root`` predicate is
        # repeated alongside the id list as a belt-and-suspenders guard
        # so a future caller bug that pollutes ``ids`` can never widen
        # the delete past the ADR-0011 auth boundary.
        chunks_deleted = 0
        for i in range(0, len(ids), _BATCH_SIZE):
            batch = ids[i : i + _BATCH_SIZE]
            ph = placeholders(len(batch))
            cursor = db.execute(
                f"DELETE FROM chunks "  # noqa: S608
                f"WHERE id IN ({ph}) "
                f"  AND scope IN ('project_shared', 'project_local') "
                f"  AND project_root = ?",
                [*batch, project_root],
            )
            chunks_deleted += cursor.rowcount or 0

        # AI summary cache: only drop when the source has no remaining
        # chunks at all. A single ``source_file`` should normally live
        # under exactly one ``project_root``, but a scope reclassification
        # could leave a stale user-scope row pointing at the same path;
        # the defensive ``LIMIT 1`` lookup protects that case.
        ai_summaries_deleted = 0
        for source_norm in affected_sources:
            remaining = db.execute(
                "SELECT 1 FROM chunks WHERE source_file = ? LIMIT 1",
                (source_norm,),
            ).fetchone()
            if remaining is None:
                cursor = db.execute(
                    "DELETE FROM _memtomem_meta WHERE key = ?",
                    (f"{_AI_SUMMARY_KEY_PREFIX}{source_norm}",),
                )
                ai_summaries_deleted += cursor.rowcount or 0

        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    return SweepResult(
        project_root=project_root,
        chunks_deleted=chunks_deleted,
        fts_deleted=fts_deleted,
        vec_deleted=vec_deleted,
        ai_summaries_deleted=ai_summaries_deleted,
    )
