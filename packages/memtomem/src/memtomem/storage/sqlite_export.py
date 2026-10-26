"""Snapshot-consistent export reads and matching count-only previews."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from memtomem.storage.sqlite_helpers import utc_bound
from memtomem.storage.sqlite_visibility import VISIBLE_SOURCE_C


# SQL fragments below are fixed literals; all caller values are bound parameters.
def _where(source_filter, tag_filter, since, namespace_filter):
    terms, params = [], []
    if source_filter:
        terms.append("instr(replace(c.source_file, char(92), '/'), ?) > 0")
        params.append(source_filter.replace("\\", "/"))
    if tag_filter:
        terms.append(
            "json_type(CASE WHEN json_valid(c.tags) THEN c.tags ELSE '[]' END) = 'array' "
            "AND EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.tags) "
            "THEN c.tags ELSE '[]' END) WHERE value = ?)"
        )
        params.append(tag_filter)
    if since:
        terms.append("c.created_at >= ?")
        params.append(utc_bound(since))
    if namespace_filter:
        terms.append("c.namespace = ?")
        params.append(namespace_filter)
    return " AND ".join(terms) or "1", params


@contextmanager
def _snapshot(db):
    # The leased connection cannot be reused while the deferred read snapshot is open.
    owned = not db.in_transaction
    if owned:
        db.execute("BEGIN")
    try:
        yield db
    finally:
        if owned:
            db.rollback()


def _counts(db, where, params):
    visible = db.execute(
        f"SELECT COUNT(*) FROM chunks c WHERE {VISIBLE_SOURCE_C} AND ({where})",  # nosec B608
        params,
    ).fetchone()[0]
    omitted = db.execute(
        f"WITH visibility AS (SELECT c.source_file, MAX({VISIBLE_SOURCE_C}) AS visible "  # nosec B608
        "FROM chunks c GROUP BY c.source_file) "
        "SELECT COUNT(DISTINCT c.source_file) FROM chunks c JOIN visibility v "
        f"ON v.source_file=c.source_file WHERE v.visible=0 AND ({where})",
        params,
    ).fetchone()[0]
    return visible, omitted


async def read_export(
    storage,
    *,
    source_filter=None,
    tag_filter=None,
    since: datetime | None = None,
    namespace_filter=None,
    count_only=False,
):
    where, params = _where(source_filter, tag_filter, since, namespace_filter)
    async with storage._lease_txn_read_db() as db:
        with _snapshot(db):
            total, omitted = _counts(db, where, params)
            if count_only:
                return total, omitted
            rows = db.execute(
                f"SELECT c.* FROM chunks c WHERE {VISIBLE_SOURCE_C} AND ({where}) "  # nosec B608
                "ORDER BY c.source_file, c.start_line, c.rowid",
                params,
            ).fetchall()
            edges = db.execute(
                f"WITH selected AS (SELECT c.id FROM chunks c WHERE {VISIBLE_SOURCE_C} "  # nosec B608
                f"AND ({where})) SELECT source_id, target_id, relation_type FROM chunk_relations "
                "WHERE source_id IN (SELECT id FROM selected) "
                "AND target_id IN (SELECT id FROM selected) ORDER BY source_id, target_id",
                params,
            ).fetchall()
            return [storage._row_to_chunk(row) for row in rows], edges, omitted
