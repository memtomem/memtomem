"""Analytics and reporting storage methods — replaces direct _get_db() in tools."""

from __future__ import annotations


class AnalyticsMixin:
    """Mixin providing analytics methods. Requires self._get_db()."""

    async def get_health_report(self, namespace: str | None = None) -> dict:
        """Compute a memory health report — replaces raw SQL in evaluation.py and web/routes/evaluation.py."""
        db = self._get_db()

        # Single query for chunk aggregate counts
        agg = db.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN access_count > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN tags != '[]' AND tags != '' THEN 1 ELSE 0 END) "
            "FROM chunks"
        ).fetchone()
        total_chunks, accessed, tagged = agg[0], agg[1] or 0, agg[2] or 0
        access_pct = round(accessed / total_chunks * 100, 1) if total_chunks else 0
        tag_pct = round(tagged / total_chunks * 100, 1) if total_chunks else 0

        top_accessed = db.execute(
            "SELECT id, content, access_count FROM chunks WHERE access_count > 0 ORDER BY access_count DESC LIMIT 10",
        ).fetchall()
        top_list = [{"id": r[0], "content": r[1][:120], "access_count": r[2]} for r in top_accessed]

        ns_rows = db.execute(
            "SELECT COALESCE(namespace, 'default'), COUNT(*) FROM chunks GROUP BY namespace ORDER BY COUNT(*) DESC",
        ).fetchall()
        ns_dist = [{"namespace": r[0], "count": r[1]} for r in ns_rows]

        # Single query for session/scratch/relation counts
        aux = db.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM sessions), "
            "(SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL), "
            "(SELECT COUNT(*) FROM working_memory), "
            "(SELECT COUNT(*) FROM working_memory WHERE promoted = 1), "
            "(SELECT COUNT(*) FROM chunk_relations)"
        ).fetchone()
        total_sessions, active_sessions, scratch_count, promoted_count, relation_count = aux

        dead_pct = round((total_chunks - accessed) / total_chunks * 100, 1) if total_chunks else 0

        return {
            "total_chunks": total_chunks,
            "access_coverage": {"accessed": accessed, "total": total_chunks, "pct": access_pct},
            "tag_coverage": {"tagged": tagged, "total": total_chunks, "pct": tag_pct},
            "dead_memories_pct": dead_pct,
            "top_accessed": top_list,
            "namespace_distribution": ns_dist,
            "sessions": {"total": total_sessions, "active": active_sessions},
            "working_memory": {"total": scratch_count, "promoted": promoted_count},
            "cross_references": relation_count,
        }

    async def get_frequently_accessed(
        self,
        namespace: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return most accessed chunks with hierarchy info — for reflection."""
        import json as _json

        db = self._get_db()
        query = (
            "SELECT heading_hierarchy, source_file, SUM(access_count) as total_access "
            "FROM chunks WHERE access_count > 0 "
        )
        params: list = []
        if namespace:
            query += "AND namespace = ? "
            params.append(namespace)
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

    async def get_knowledge_gaps(self, limit: int = 10) -> list[dict]:
        """Return frequent queries with no results — for reflection."""
        db = self._get_db()
        try:
            rows = db.execute(
                "SELECT query_text, COUNT(*) as cnt FROM query_history "
                "WHERE result_chunk_ids = '[]' GROUP BY query_text ORDER BY cnt DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"query": r[0], "count": r[1]} for r in rows]
        except Exception:
            return []

    async def get_most_connected(self, limit: int = 5) -> list[dict]:
        """Return chunks with most cross-references — for reflection."""
        db = self._get_db()
        relation_count = db.execute("SELECT COUNT(*) FROM chunk_relations").fetchone()[0]
        if relation_count == 0:
            return []
        rows = db.execute(
            "SELECT chunk_id, cnt FROM ("
            "  SELECT source_id as chunk_id, COUNT(*) as cnt FROM chunk_relations GROUP BY source_id "
            "  UNION ALL "
            "  SELECT target_id, COUNT(*) FROM chunk_relations GROUP BY target_id"
            ") GROUP BY chunk_id ORDER BY SUM(cnt) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"chunk_id": r[0], "link_count": r[1]} for r in rows]

    async def get_chunk_factors(self, namespace: str | None = None) -> list[dict]:
        """Return access_count, tag_count, relation_count per chunk — for importance scoring."""
        import json as _json2

        db = self._get_db()
        query = (
            "SELECT c.id, c.access_count, c.updated_at, c.tags, "
            "(SELECT COUNT(*) FROM chunk_relations cr WHERE cr.source_id = c.id OR cr.target_id = c.id) as rel_count "
            "FROM chunks c"
        )
        params: list = []
        if namespace:
            query += " WHERE c.namespace = ?"
            params.append(namespace)
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
        db.executemany(
            "UPDATE chunks SET importance_score = ? WHERE id = ?",
            [(score, chunk_id) for chunk_id, score in scores.items()],
        )
        db.commit()
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
        """Aggregate created/updated/accessed counts by day."""
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

        date_filter = (" AND " + " AND ".join(where_parts)) if where_parts else ""

        # Created per day
        created_params = list(params)
        if namespace:
            created_params.append(namespace)
        created_rows = db.execute(
            f"SELECT DATE(created_at) as day, COUNT(*) FROM chunks WHERE 1=1{date_filter}{ns_clause} GROUP BY day ORDER BY day",
            created_params,
        ).fetchall()
        created = {r[0]: r[1] for r in created_rows}

        # Updated per day (exclude same-day creates)
        updated_params = list(params)
        if namespace:
            updated_params.append(namespace)
        updated_rows = db.execute(
            f"SELECT DATE(updated_at) as day, COUNT(*) FROM chunks WHERE updated_at != created_at{date_filter.replace('created_at', 'updated_at')}{ns_clause} GROUP BY day ORDER BY day",
            updated_params,
        ).fetchall()
        updated = {r[0]: r[1] for r in updated_rows}

        # Accessed per day (from access_log)
        access_params = list(params)
        accessed: dict[str, int] = {}
        try:
            access_rows = db.execute(
                f"SELECT DATE(created_at) as day, COUNT(*) FROM access_log WHERE 1=1{date_filter} GROUP BY day ORDER BY day",
                access_params,
            ).fetchall()
            accessed = {r[0]: r[1] for r in access_rows}
        except Exception:
            pass

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
