"""Analytics and reporting storage methods — replaces direct _get_db() in tools."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from memtomem.storage.sqlite_helpers import project_boundary_key
from memtomem.storage.sqlite_scope import scope_context_sql

logger = logging.getLogger(__name__)


def _visible_chunks_where(
    namespace: str | None,
    project_context_root: Path | None,
    *,
    alias: str = "",
) -> tuple[str, list]:
    clause, params = scope_context_sql(None, project_context_root, column_alias=alias)
    if namespace is not None:
        clause += f" AND {alias}namespace = ?"
        params.append(namespace)
    return clause, params


#: ``reason`` value on health-report blocks that carry no per-project answer.
#: The rows behind them (``sessions``, ``working_memory``) have no
#: ``project_root`` column, so a project-scoped report cannot compute them
#: without leaking whole-store counts.
NO_PROJECT_IDENTITY = "no_project_identity"


def _unavailable_block(*fields: str) -> dict:
    """Build a health-report block whose counts have no project-scoped answer.

    Every count is ``None`` rather than ``0`` so a renderer can tell "not
    applicable at this scope" from "genuinely empty"; ``available`` is the
    flag to branch on and ``reason`` says why (#2281).
    """
    block: dict = dict.fromkeys(fields)
    block["available"] = False
    block["reason"] = NO_PROJECT_IDENTITY
    return block


class AnalyticsMixin:
    """Mixin providing analytics methods. Requires self._get_db() and
    self._in_transaction."""

    async def get_health_report(
        self,
        namespace: str | None = None,
        *,
        project_context_root: Path | None = None,
    ) -> dict:
        """Compute a memory health report — replaces raw SQL in evaluation.py and web/routes/evaluation.py.

        The ``sessions`` and ``working_memory`` blocks are always
        ``available: false`` with ``None`` counts: those tables carry no
        project identity, so there is no per-project answer to give (see
        :func:`_unavailable_block`). Consumers must branch on ``available``
        before formatting the counts.
        """
        db = self._get_db()
        visible_sql, visible_params = _visible_chunks_where(namespace, project_context_root)

        # Single query for chunk aggregate counts
        agg = db.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN access_count > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN tags != '[]' AND tags != '' THEN 1 ELSE 0 END) "
            f"FROM chunks WHERE {visible_sql}",
            visible_params,
        ).fetchone()
        total_chunks, accessed, tagged = agg[0], agg[1] or 0, agg[2] or 0
        access_pct = round(accessed / total_chunks * 100, 1) if total_chunks else 0
        tag_pct = round(tagged / total_chunks * 100, 1) if total_chunks else 0

        top_accessed = db.execute(
            "SELECT id, content, access_count FROM chunks WHERE access_count > 0 AND "
            f"{visible_sql} ORDER BY access_count DESC LIMIT 10",
            visible_params,
        ).fetchall()
        top_list = [{"id": r[0], "content": r[1][:120], "access_count": r[2]} for r in top_accessed]

        ns_rows = db.execute(
            "SELECT COALESCE(namespace, 'default'), COUNT(*) FROM chunks WHERE "
            f"{visible_sql} GROUP BY namespace ORDER BY COUNT(*) DESC",
            visible_params,
        ).fetchall()
        ns_dist = [{"namespace": r[0], "count": r[1]} for r in ns_rows]

        total_sources = db.execute(
            f"SELECT COUNT(DISTINCT source_file) FROM chunks WHERE {visible_sql}",
            visible_params,
        ).fetchone()[0]

        relation_count = db.execute(
            "SELECT relation_count FROM (WITH visible AS (SELECT id FROM chunks WHERE "
            f"{visible_sql}) "
            "SELECT COUNT(*) AS relation_count FROM chunk_relations r "
            "JOIN visible s ON s.id = r.source_id "
            "JOIN visible t ON t.id = r.target_id)",
            visible_params,
        ).fetchone()[0]

        dead_pct = round((total_chunks - accessed) / total_chunks * 100, 1) if total_chunks else 0

        return {
            "total_chunks": total_chunks,
            "total_sources": total_sources,
            "access_coverage": {"accessed": accessed, "total": total_chunks, "pct": access_pct},
            "tag_coverage": {"tagged": tagged, "total": total_chunks, "pct": tag_pct},
            "dead_memories_pct": dead_pct,
            "top_accessed": top_list,
            "namespace_distribution": ns_dist,
            # Sessions and scratch rows have no project identity. Public
            # reports fail closed instead of returning whole-store counts;
            # system-wide maintenance can query those tables explicitly.
            # The blocks stay in the payload but every count is ``None``
            # behind ``available: false`` — a zero would read as "this
            # install has no sessions", which is a different claim (#2281).
            "sessions": _unavailable_block("total", "active", "recent_7d"),
            "working_memory": _unavailable_block("total", "promoted"),
            "cross_references": relation_count,
        }

    async def get_frequently_accessed(
        self,
        namespace: str | None = None,
        limit: int = 20,
        *,
        project_context_root: Path | None = None,
    ) -> list[dict]:
        """Return most accessed chunks with hierarchy info — for reflection."""
        import json as _json

        db = self._get_db()
        visible_sql, params = _visible_chunks_where(namespace, project_context_root)
        # ``visible_sql`` is built by ``scope_context_sql`` from fixed column
        # names with ``?`` placeholders; every caller value travels in
        # ``params``. Interpolated because a scope predicate is structure, not
        # a value, and SQLite cannot bind that.
        query = (
            "SELECT heading_hierarchy, source_file, SUM(access_count) as total_access "
            f"FROM chunks WHERE access_count > 0 AND {visible_sql} "  # nosec B608
        )
        query += "GROUP BY heading_hierarchy, source_file ORDER BY total_access DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [
            {
                "hierarchy": _json.loads(r[0]) if r[0] else [],
                "source_file": r[1],
                "total_access": r[2],
            }
            for r in rows
        ]

    async def get_agent_sessions(
        self,
        since: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return agent activity summary — for reflection."""
        db = self._get_db()
        query = "SELECT agent_id, COUNT(*) as cnt, MAX(started_at) as last FROM sessions "
        params: list = []
        if since:
            query += "WHERE started_at >= ? "
            params.append(since)
        query += "GROUP BY agent_id ORDER BY cnt DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [{"agent_id": r[0], "session_count": r[1], "last_session": r[2]} for r in rows]

    async def get_knowledge_gaps(
        self, limit: int = 10, *, project_context_root: Path | None = None
    ) -> list[dict]:
        """Return frequent queries with no results — for reflection."""
        db = self._get_db()
        try:
            rows = db.execute(
                "SELECT query_text, COUNT(*) as cnt FROM query_history "
                "WHERE result_chunk_ids = '[]' AND project_key = ? "
                "AND legacy_unscoped = 0 GROUP BY query_text "
                "ORDER BY cnt DESC LIMIT ?",
                (project_boundary_key(project_context_root), limit),
            ).fetchall()
            return [{"query": r[0], "count": r[1]} for r in rows]
        except sqlite3.OperationalError as exc:
            # Only the expected "table not created yet on a fresh DB" case
            # degrades to []. A real bug (bad column, SQL syntax) also
            # raises OperationalError — re-raise those instead of hiding a
            # regression behind an empty result.
            if "no such table" not in str(exc).lower():
                raise
            logger.debug("get_knowledge_gaps: query_history table missing", exc_info=True)
            return []

    async def get_most_connected(
        self,
        limit: int = 5,
        *,
        namespace: str | None = None,
        project_context_root: Path | None = None,
    ) -> list[dict]:
        """Return the caller's most cross-referenced chunks — for reflection.

        Boundary-aware by construction (#2244): the hub row and *both*
        endpoints of every counted edge are screened against the ADR-0011
        scope fragment, so ``link_count`` is the visible degree, and the
        ranking and ``limit`` are applied after that screen rather than
        before it. A hub whose every edge leaves the boundary has no visible
        degree and is absent from the result — it is never reported as zero.
        Callers therefore do not need to over-fetch and re-rank.
        """
        db = self._get_db()
        visible_sql, params = _visible_chunks_where(namespace, project_context_root)
        # ``adjacency`` is the undirected neighbour set ``get_related`` returns,
        # restricted to visible endpoints: each stored row read from both ends,
        # deduplicated on ``(hub, neighbour, relation_type)`` by the ``UNION``.
        # That identity is ``get_related``'s own, so two rows joining the same
        # pair under different relation types stay two links, as they do there.
        # Summing two directed ``COUNT``s instead would count twice both a
        # self-relation and a same-type pair stored in both endpoint orders —
        # ``mem_link`` writes one row, so that shape comes from linking the two
        # ends separately — inflating the degree above the links a caller can
        # actually follow, and with it the ranking.
        rows = db.execute(
            "SELECT chunk_id, link_count FROM (WITH visible AS (SELECT id FROM chunks WHERE "
            f"{visible_sql}), adjacency AS ("  # nosec B608
            "  SELECT r.source_id AS chunk_id, r.target_id AS neighbour_id, "
            "         r.relation_type AS rel FROM chunk_relations r "
            "  JOIN visible s ON s.id = r.source_id JOIN visible t ON t.id = r.target_id "
            "  UNION "
            "  SELECT r.target_id, r.source_id, r.relation_type FROM chunk_relations r "
            "  JOIN visible s ON s.id = r.source_id JOIN visible t ON t.id = r.target_id"
            ") SELECT chunk_id, COUNT(*) AS link_count FROM adjacency "
            "GROUP BY chunk_id ORDER BY link_count DESC, chunk_id ASC LIMIT ?)",
            [*params, limit],
        ).fetchall()
        return [{"chunk_id": r[0], "link_count": r[1]} for r in rows]

    async def get_chunk_factors(
        self,
        namespace: str | None = None,
        *,
        project_context_root: Path | None = None,
    ) -> list[dict]:
        """Return access_count, tag_count, relation_count per chunk — for importance scoring."""
        import json as _json2

        db = self._get_db()
        visible_sql, params = _visible_chunks_where(namespace, project_context_root, alias="c.")
        # Same contract as ``get_frequently_accessed`` above: structure is
        # interpolated, values stay bound in ``params``.
        query = (
            "SELECT * FROM (WITH visible AS (SELECT c.* FROM chunks c WHERE "
            f"{visible_sql}) "  # nosec B608
            "SELECT c.id, c.access_count, c.updated_at, c.tags, "
            "(SELECT COUNT(*) FROM chunk_relations cr "
            " WHERE (cr.source_id = c.id AND cr.target_id IN (SELECT id FROM visible)) "
            "    OR (cr.target_id = c.id AND cr.source_id IN (SELECT id FROM visible))) "
            "AS rel_count FROM visible c)"
        )
        rows = db.execute(query, params).fetchall()
        results = []
        for r in rows:
            tags = _json2.loads(r[3]) if r[3] else []
            results.append(
                {
                    "id": r[0],
                    "access_count": r[1],
                    "updated_at": r[2],
                    "tag_count": len(tags),
                    "relation_count": r[4],
                }
            )
        return results

    async def get_consolidation_groups(self, min_size: int = 3, max_groups: int = 10) -> list[dict]:
        """Return source files with enough chunks for consolidation — for scheduler."""
        db = self._get_db()
        rows = db.execute(
            "SELECT source_file, COUNT(*) as cnt FROM chunks GROUP BY source_file HAVING cnt >= ? ORDER BY cnt DESC LIMIT ?",
            (min_size, max_groups),
        ).fetchall()
        return [{"source": r[0], "chunk_count": r[1]} for r in rows]

    async def update_importance_scores(self, scores: dict[str, float]) -> int:
        if not scores:
            return 0
        db = self._get_db()
        with self._rolls_back_if_standalone(db):
            db.executemany(
                "UPDATE chunks SET importance_score = ? WHERE id = ?",
                [(score, chunk_id) for chunk_id, score in scores.items()],
            )
            # Transaction-aware: ``apply_consolidation`` decays the originals in the
            # same transaction that creates their summary, so committing here would
            # end the caller's transaction early and strand a partial write (#2158).
            self._commit_if_standalone(db)
        return len(scores)

    async def get_importance_scores(self, chunk_ids: list) -> dict[str, float]:
        if not chunk_ids:
            return {}
        db = self._get_db()
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = db.execute(
            f"SELECT id, importance_score FROM chunks WHERE id IN ({placeholders})",
            [str(cid) for cid in chunk_ids],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_activity_summary(
        self,
        since: str | None = None,
        until: str | None = None,
        namespace: str | None = None,
    ) -> list[dict]:
        """Aggregate created/updated/accessed counts by day.

        ``since`` and ``until`` are ``YYYY-MM-DD`` day labels and **both ends
        are inclusive** — the comparison is `DATE(created_at) BETWEEN`, so
        ``until`` names the last day counted, not the first day excluded.
        Callers holding an exclusive instant must step back to the day it
        admits before passing it here.
        """
        db = self._get_read_db()

        # Build date filter
        where_parts: list[str] = []
        params: list[str] = []
        if since:
            where_parts.append("DATE(created_at) >= ?")
            params.append(since)
        if until:
            where_parts.append("DATE(created_at) <= ?")
            params.append(until)
        ns_clause = ""
        if namespace:
            ns_clause = " AND namespace = ?"

        created_date_filter = (" AND " + " AND ".join(where_parts)) if where_parts else ""
        updated_where_parts = [p.replace("created_at", "updated_at") for p in where_parts]
        updated_date_filter = (
            (" AND " + " AND ".join(updated_where_parts)) if updated_where_parts else ""
        )

        # Created per day
        created_params = list(params)
        if namespace:
            created_params.append(namespace)
        created_rows = db.execute(
            f"SELECT DATE(created_at) as day, COUNT(*) FROM chunks WHERE 1=1{created_date_filter}{ns_clause} GROUP BY day ORDER BY day",
            created_params,
        ).fetchall()
        created = {r[0]: r[1] for r in created_rows}

        # Updated per day (exclude same-day creates)
        updated_params = list(params)
        if namespace:
            updated_params.append(namespace)
        updated_rows = db.execute(
            f"SELECT DATE(updated_at) as day, COUNT(*) FROM chunks WHERE updated_at != created_at{updated_date_filter}{ns_clause} GROUP BY day ORDER BY day",
            updated_params,
        ).fetchall()
        updated = {r[0]: r[1] for r in updated_rows}

        # Accessed per day (from access_log)
        access_params = list(params)
        accessed: dict[str, int] = {}
        try:
            access_rows = db.execute(
                f"SELECT DATE(created_at) as day, COUNT(*) FROM access_log WHERE 1=1{created_date_filter} GROUP BY day ORDER BY day",
                access_params,
            ).fetchall()
            accessed = {r[0]: r[1] for r in access_rows}
        except sqlite3.OperationalError as exc:
            # Only degrade for the expected missing-table case on a fresh
            # DB; re-raise real query bugs (bad column, syntax) so they
            # aren't hidden behind a partial timeline.
            if "no such table" not in str(exc).lower():
                raise
            logger.debug("get_activity_summary: access_log table missing", exc_info=True)

        # Merge all days
        all_days = sorted(set(created) | set(updated) | set(accessed))
        return [
            {
                "date": day,
                "created": created.get(day, 0),
                "updated": updated.get(day, 0),
                "accessed": accessed.get(day, 0),
            }
            for day in all_days
        ]
