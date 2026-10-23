"""BM25, dense vector search, recall, and source-browsing queries for SqliteBackend."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import AsyncIterator, Sequence
from uuid import UUID

from memtomem.errors import (
    ExhaustiveDenseSearchLimitError,
    QueryEmbeddingDimensionError,
)
from memtomem.models import (
    Chunk,
    NamespaceFilter,
    ScopeFilter,
    SearchResult,
)
from memtomem.storage import fts_tokenizer as _fts
from memtomem.storage.base import (
    ChunkAuditRow,
    ContextWindowRows,
    SearchMetadataFilter,
    parse_tag_filter,
)
from memtomem.storage.sqlite_helpers import (
    escape_like,
    match_source_filter_value,
    namespace_sql,
    norm_path,
    placeholders,
    serialize_f32,
    utc_bound,
)
from memtomem.storage.sqlite_scope import scope_context_sql, scope_sort_priority_case
from memtomem.storage.sqlite_visibility import (
    NOT_WITHHELD_CHUNKS,
    VISIBLE_SOURCE_C,
    VISIBLE_SOURCE_CHUNKS,
    VISIBLE_SOURCE_PATH,
    neighbor_visibility_sql,
    visible_path_sql,
)

logger = logging.getLogger(__name__)

# Hard ceiling on the ``k`` of a sqlite-vec KNN query (``embedding MATCH ?``
# with an inner ``LIMIT``). sqlite-vec 0.1.9 rejects anything larger with
# ``k value in knn query too large, provided <k> and the limit is 4096``.
VEC_MAX_KNN_K = 4096

# Substring identifying sqlite-vec's own refusal of an oversized KNN ``k``.
_VEC_K_TOO_LARGE = "k value in knn query too large"

_VISIBLE_SOURCE_C = f"AND {VISIBLE_SOURCE_C}"
_VISIBLE_SOURCE = VISIBLE_SOURCE_CHUNKS

#: The two ``chunks_fts`` columns, as fts5 column-filter names (#2224).
_FTS_CONTENT_COLUMN = "content"
_FTS_SOURCE_COLUMN = "source_file"


def _chunk_ids_sql(column_alias: str = "") -> str:
    """SQL fragment restricting rows to an explicit chunk-id set."""
    return f"{column_alias}id IN (SELECT value FROM json_each(?))"


def _chunk_ids_param(chunk_ids: Sequence[UUID]) -> str:
    return json.dumps([str(cid) for cid in chunk_ids])


def _tags_any_sql(column_alias: str = "") -> str:
    """SQL fragment matching rows carrying ANY tag from a bound JSON array."""
    return (
        f"CASE WHEN json_valid({column_alias}tags) "
        f"THEN json_type({column_alias}tags) = 'array' AND EXISTS ("
        f"SELECT 1 FROM json_each({column_alias}tags) AS ct "
        f"WHERE ct.value IN (SELECT wt.value FROM json_each(?) AS wt)) "
        f"ELSE 0 END"
    )


def _tags_any_param(tags: Sequence[str]) -> str:
    return json.dumps(list(tags))


def _metadata_filter_sql(
    metadata_filter: SearchMetadataFilter | None,
    *,
    column_alias: str = "",
) -> tuple[str, list[object]]:
    if metadata_filter is None:
        return "", []
    conditions: list[str] = []
    params: list[object] = []
    if metadata_filter.tags_any:
        conditions.append(f"({_tags_any_sql(column_alias)})")
        params.append(_tags_any_param(metadata_filter.tags_any))
    if metadata_filter.source_exact:
        values = [norm_path(Path(value)) for value in metadata_filter.source_exact]
        conditions.append(f"{column_alias}source_file IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(values))
    if metadata_filter.chunk_types:
        conditions.append(
            f"{column_alias}chunk_type IN ({placeholders(len(metadata_filter.chunk_types))})"
        )
        params.extend(metadata_filter.chunk_types)
    if metadata_filter.created_from is not None:
        conditions.append(f"{column_alias}created_at >= ?")
        params.append(utc_bound(metadata_filter.created_from))
    if metadata_filter.created_before is not None:
        conditions.append(f"{column_alias}created_at < ?")
        params.append(utc_bound(metadata_filter.created_before))
    return " AND ".join(conditions), params


def _restrict_to_column(column: str, body: str) -> str:
    """Wrap an already-tokenized expression in one ``chunks_fts`` column filter."""
    if column not in (_FTS_CONTENT_COLUMN, _FTS_SOURCE_COLUMN):  # pragma: no cover - guard
        raise ValueError(f"not a chunks_fts column: {column!r}")
    return "{" + column + "} : (" + body + ")"


def _column_match(column: str, query: str, *, use_or: bool) -> str:
    """Tokenize *query* and restrict it to one column — one call, one analysis."""
    return _restrict_to_column(column, _fts.tokenize_for_fts(query, for_query=True, use_or=use_or))


class SearchOpsMixin:
    """Search, recall, context-window, and audit read operations for SqliteBackend."""

    async def bm25_search(
        self,
        query: str,
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        source_filter: str | None = None,
    ) -> list[SearchResult]:
        async with self._lease_read_db() as db:
            ns_clause = ""
            ns_params: list = []
            if namespace_filter:
                frag, ns_params = namespace_sql(namespace_filter)
                if frag:
                    ns_clause = f"AND c.{frag}"

            scope_frag, scope_params = scope_context_sql(
                scope_filter, project_context_root, column_alias="c."
            )
            scope_clause = f"AND ({scope_frag})"
            tie_break = scope_sort_priority_case("c.")
            metadata_frag, metadata_params = _metadata_filter_sql(
                metadata_filter, column_alias="c."
            )
            metadata_clause = f"AND ({metadata_frag})" if metadata_frag else ""
            source_clause = (
                "AND memtomem_source_match(?, c.source_file) = 1" if source_filter else ""
            )
            source_params = [source_filter] if source_filter else []

            and_body = _fts.tokenize_for_fts(query, for_query=True)
            or_body = _fts.tokenize_for_fts(query, for_query=True, use_or=True)
            bodies = [and_body] if or_body == and_body else [and_body, or_body]

            def _query(conn: sqlite3.Connection) -> list:
                sql = f"""SELECT c.*, fts.rank
                       FROM chunks_fts fts
                       JOIN chunks c ON c.rowid = fts.rowid
                       WHERE chunks_fts MATCH ? {ns_clause} {scope_clause} {metadata_clause} {source_clause} {_VISIBLE_SOURCE_C}
                       ORDER BY fts.rank, {tie_break}, c.id
                       LIMIT ?"""
                rows: list = []
                for column in (_FTS_CONTENT_COLUMN, _FTS_SOURCE_COLUMN):
                    for body in bodies:
                        rows = conn.execute(
                            sql,
                            [_restrict_to_column(column, body)]
                            + ns_params
                            + scope_params
                            + metadata_params
                            + source_params
                            + [top_k],
                        ).fetchall()
                        if rows:
                            break
                    if rows:
                        break
                return rows

            rows = await self._run_read_in_thread(db, _query)

        return [
            SearchResult(
                chunk=self._row_to_chunk(row[:-1]),
                score=abs(row[-1]),
                rank=rank_idx + 1,
                source="bm25",
            )
            for rank_idx, row in enumerate(rows)
        ]

    @property
    def dense_enabled(self) -> bool:
        """Whether this store can answer a dense search at all."""
        return self._has_vec_table

    async def dense_search(
        self,
        embedding: list[float],
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        source_filter: str | None = None,
        *,
        exhaustive: bool = False,
    ) -> list[SearchResult]:
        if not self._has_vec_table:
            return []

        async with self._lease_read_db() as db:
            if len(embedding) != self._dimension:
                raise QueryEmbeddingDimensionError.vetted(
                    f"Embedding dimension mismatch: query has {len(embedding)}d "
                    f"but DB expects {self._dimension}d. "
                    f"Check MEMTOMEM_EMBEDDING__MODEL / "
                    f"MEMTOMEM_EMBEDDING__DIMENSION."
                )

            ns_clause = ""
            ns_params: list = []
            if namespace_filter:
                frag, ns_params = namespace_sql(namespace_filter)
                if frag:
                    ns_clause = f"AND c.{frag}"

            scope_frag, scope_params = scope_context_sql(
                scope_filter, project_context_root, column_alias="c."
            )
            scope_clause = f"AND ({scope_frag})"
            tie_break = scope_sort_priority_case("c.")
            required_tags = (
                set(metadata_filter.tags_any) if metadata_filter is not None else set()
            )
            required_source_filter = source_filter
            sql_metadata_filter = (
                dataclass_replace(metadata_filter, tags_any=())
                if metadata_filter is not None and metadata_filter.tags_any
                else metadata_filter
            )
            metadata_frag, metadata_params = _metadata_filter_sql(
                sql_metadata_filter, column_alias="c."
            )
            metadata_clause = f"AND ({metadata_frag})" if metadata_frag else ""

            from memtomem.storage import sqlite_backend as _sb

            vec_max_knn_k = _sb.VEC_MAX_KNN_K

            def _query(conn: sqlite3.Connection) -> list:
                sql = f"""SELECT c.*, sub.distance
                       FROM (
                           SELECT rowid, distance
                           FROM chunks_vec
                           WHERE embedding MATCH ?
                           ORDER BY distance
                           LIMIT ?
                       ) sub
                       JOIN chunks c ON c.rowid = sub.rowid
                       WHERE {VISIBLE_SOURCE_C} {ns_clause} {scope_clause} {metadata_clause}
                       ORDER BY sub.distance, {tie_break}, c.id
                       LIMIT ?"""
                total_vec_rows = self._cached_vec_row_count(conn)
                if not total_vec_rows:
                    return []
                knn_ceiling = min(total_vec_rows, vec_max_knn_k)
                if exhaustive and total_vec_rows > vec_max_knn_k:
                    raise ExhaustiveDenseSearchLimitError.vetted(
                        f"Exhaustive dense search needs to scan all {total_vec_rows} embeddings, "
                        f"but sqlite-vec caps a KNN query at {vec_max_knn_k}. Deterministic "
                        f"replay/evaluation is unavailable on a store this large — run it "
                        f"against a smaller evaluation store, or use ordinary "
                        f"(non-exhaustive) search, whose adaptive over-fetch is clamped and "
                        f"therefore still returns results here."
                    )

                attempts: list[int] = []
                for raw_k in (
                    [total_vec_rows or 1]
                    if exhaustive
                    else [
                        max(top_k * 5, 100),
                        max(top_k * 50, 1000),
                        total_vec_rows,
                    ]
                ):
                    clamped = max(1, min(raw_k, knn_ceiling))
                    if clamped not in attempts:
                        attempts.append(clamped)

                rows: list = []
                for inner_k in attempts:
                    try:
                        outer_limit = (
                            inner_k if required_tags or required_source_filter else top_k
                        )
                        rows = conn.execute(
                            sql,
                            [serialize_f32(embedding), inner_k]
                            + ns_params
                            + scope_params
                            + metadata_params
                            + [outer_limit],
                        ).fetchall()
                    except sqlite3.OperationalError as exc:
                        if "Dimension mismatch" in str(exc):
                            raise QueryEmbeddingDimensionError.vetted(
                                f"Embedding dimension mismatch: query has {len(embedding)}d "
                                f"but DB expects {self._dimension}d. "
                                f"Check MEMTOMEM_EMBEDDING__MODEL / "
                                f"MEMTOMEM_EMBEDDING__DIMENSION."
                            ) from exc
                        if rows and _VEC_K_TOO_LARGE in str(exc):
                            logger.warning(
                                "Dense KNN escalation to k=%d failed (%s); keeping the %d "
                                "candidate(s) from the previous attempt",
                                inner_k,
                                exc,
                                len(rows),
                            )
                            break
                        raise
                    if len(rows) >= top_k or inner_k >= knn_ceiling:
                        break
                return rows

            rows = await self._run_read_in_thread(db, _query)

        chunks = [(self._row_to_chunk(row[:-1]), row[-1]) for row in rows]
        if required_tags:
            chunks = [pair for pair in chunks if required_tags & set(pair[0].metadata.tags)]
        if required_source_filter:
            chunks = [
                pair
                for pair in chunks
                if match_source_filter_value(
                    required_source_filter, str(pair[0].metadata.source_file)
                )
            ]
        if required_tags or required_source_filter:
            chunks = chunks[:top_k]
        return [
            SearchResult(
                chunk=chunk,
                score=1.0 / (1.0 + distance),
                rank=rank_idx + 1,
                source="dense",
            )
            for rank_idx, (chunk, distance) in enumerate(chunks)
        ]

    async def get_context_windows(
        self,
        source_file: Path,
        anchor_ids: Sequence[UUID],
        window: int,
        *,
        ns_filter: NamespaceFilter | None,
        system_prefixes: Sequence[str],
        scope_filter: ScopeFilter | None,
        project_context_root: Path | None,
        as_of_unix: int | None,
    ) -> dict[UUID, ContextWindowRows | None]:
        """Physical neighbours plus visible ordinal/total, without listing the file."""
        if not anchor_ids:
            return {}

        norm_source = norm_path(source_file)
        vis_sql, vis_params = neighbor_visibility_sql(
            ns_filter=ns_filter,
            system_prefixes=system_prefixes,
            scope_filter=scope_filter,
            project_context_root=project_context_root,
            as_of_unix=as_of_unix,
        )
        wanted = [str(a) for a in anchor_ids]
        async with self._lease_read_db() as db:
            rows = db.execute(
                f"""
                SELECT id, start_line, rid, vis, COALESCE(visible_before, 0), visible_all
                FROM (
                    SELECT id, start_line, rid, vis,
                           SUM(vis) OVER (
                               ORDER BY start_line, rid
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ) AS visible_before,
                           SUM(vis) OVER () AS visible_all
                    FROM (
                        SELECT id, start_line, rowid AS rid,
                               CASE WHEN ({vis_sql}) AND {NOT_WITHHELD_CHUNKS}
                                    THEN 1 ELSE 0 END AS vis
                        FROM chunks WHERE source_file=?
                    )
                )
                WHERE id IN ({placeholders(len(wanted))})
                """,
                (*vis_params, norm_source, *wanted),
            ).fetchall()

            located = {row[0]: row for row in rows}
            result: dict[UUID, ContextWindowRows | None] = {}
            for anchor_id in anchor_ids:
                row = located.get(str(anchor_id))
                if row is None:
                    result[anchor_id] = None
                    continue
                _, start_line, rid, vis, visible_before, visible_all = row
                key = (start_line, rid, str(anchor_id))
                result[anchor_id] = ContextWindowRows(
                    before=self._neighbor_rows(db, norm_source, key, window, before=True),
                    after=self._neighbor_rows(db, norm_source, key, window, before=False),
                    visible_before=int(visible_before),
                    visible_total_excluding_anchor=int(visible_all or 0) - int(vis),
                )
            return result

    def _neighbor_rows(
        self,
        db: sqlite3.Connection,
        norm_source: str,
        anchor: tuple[int, int, str],
        window: int,
        *,
        before: bool,
    ) -> list[Chunk]:
        """The ``window`` rows physically adjacent to the anchor."""
        if window <= 0:
            return []
        start_line, rid, anchor_id = anchor
        op, direction = ("<", "DESC") if before else (">", "ASC")
        rows = db.execute(
            "SELECT * FROM chunks WHERE rowid IN ("
            f"SELECT rowid FROM chunks WHERE source_file=? AND (start_line, rowid) {op} (?, ?) "
            f"AND id <> ? ORDER BY start_line {direction}, rowid {direction} LIMIT ?"
            f") AND {NOT_WITHHELD_CHUNKS} "  # nosec B608
            f"ORDER BY start_line {direction}, rowid {direction}",
            (norm_source, start_line, rid, anchor_id, window),
        ).fetchall()
        chunks = [self._row_to_chunk(row) for row in rows]
        return list(reversed(chunks)) if before else chunks

    async def recall_chunks(
        self,
        since=None,
        until=None,
        source_filter: str | None = None,
        limit: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        tag_filter: str | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        chunk_ids: Sequence[UUID] | None = None,
    ) -> list[Chunk]:
        conditions: list[str] = [_VISIBLE_SOURCE]
        params: list[object] = []

        if chunk_ids is not None:
            conditions.append(_chunk_ids_sql())
            params.append(_chunk_ids_param(chunk_ids))
        if since is not None:
            conditions.append("created_at >= ?")
            params.append(utc_bound(since))
        if until is not None:
            conditions.append("created_at < ?")
            params.append(utc_bound(until))
        if source_filter is not None:
            conditions.append("source_file LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(source_filter)}%")
        if namespace_filter is not None:
            frag, ns_params = namespace_sql(namespace_filter)
            if frag:
                conditions.append(frag)
                params.extend(ns_params)
        if tag_filter is not None:
            tags = parse_tag_filter(tag_filter)
            if tags:
                conditions.append(f"({_tags_any_sql('chunks.')})")
                params.append(_tags_any_param(tags))

        metadata_frag, metadata_params = _metadata_filter_sql(metadata_filter)
        if metadata_frag:
            conditions.append(metadata_frag)
            params.extend(metadata_params)

        scope_frag, scope_params = scope_context_sql(scope_filter, project_context_root)
        conditions.append(scope_frag)
        params.extend(scope_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        async with self._lease_read_db() as db:
            rows = db.execute(
                f"SELECT * FROM chunks {where} "
                f"ORDER BY created_at DESC, {scope_sort_priority_case()}, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    async def sum_chunk_content_chars(
        self,
        chunk_ids: Sequence[UUID],
        project_context_root: Path | None = None,
    ) -> tuple[int, int]:
        """Return ``(count, total_content_chars)`` without materializing rows."""
        conditions = [_chunk_ids_sql(), _VISIBLE_SOURCE]
        params: list[object] = [_chunk_ids_param(chunk_ids)]
        scope_frag, scope_params = scope_context_sql(None, project_context_root)
        conditions.append(scope_frag)
        params.extend(scope_params)
        async with self._lease_read_db() as db:
            row = db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) FROM chunks "
                f"WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone()
        return int(row[0]), int(row[1])

    async def search_source_files_by_content(self, query: str, limit: int = 10000) -> list[Path]:
        term = query.strip()
        if not term:
            return []
        escaped_term = f"%{escape_like(term)}%"
        escaped_json_term = f"%{escape_like(json.dumps(term, ensure_ascii=True)[1:-1])}%"
        async with self._lease_read_db() as db:
            rows = db.execute(
                "SELECT source_file FROM chunks "
                f"WHERE {_VISIBLE_SOURCE} AND (content LIKE ? ESCAPE '\\' "
                "   OR heading_hierarchy LIKE ? ESCAPE '\\' "
                "   OR heading_hierarchy LIKE ? ESCAPE '\\') "
                "GROUP BY source_file "
                "ORDER BY MAX(updated_at) DESC, source_file "
                "LIMIT ?",
                (escaped_term, escaped_term, escaped_json_term, limit),
            ).fetchall()
        return [Path(row[0]) for row in rows]

    async def get_source_files_page(
        self,
        *,
        after: str | None,
        limit: int,
        source_filter: str | None = None,
        namespace: str | None = None,
        system_prefixes: Sequence[str] = (),
    ) -> tuple[list[tuple[Path, int, str | None, str | None, int, int, int]], str | None]:
        """One page of :meth:`get_source_files_with_counts`, filtered before ``LIMIT``."""
        where = ["chunks.source_file > ?"]
        params: list[object] = [after or ""]
        if namespace:
            where.append("instr(chunks.namespace, ?) > 0")
            params.append(namespace)
        having: list[str] = []
        if source_filter:
            having.append("memtomem_source_match(?, chunks.source_file)")
            params.append(source_filter)
        having.append("memtomem_list_admit(GROUP_CONCAT(DISTINCT chunks.namespace), ?, ?)")
        params += [namespace, json.dumps(list(system_prefixes))]
        having.append(VISIBLE_SOURCE_PATH)
        params.append(limit + 1)
        async with self._lease_read_db() as db:
            rows = db.execute(
                "SELECT c.source_file, COUNT(*), MAX(c.updated_at), GROUP_CONCAT(DISTINCT c.namespace),"
                " CAST(AVG(LENGTH(c.content)/3) AS INTEGER),"
                " MIN(LENGTH(c.content)/3),"
                " MAX(LENGTH(c.content)/3)"
                " FROM (SELECT chunks.source_file FROM chunks"  # nosec B608 — fixed fragments, values bound
                f" WHERE {' AND '.join(where)}"
                f" GROUP BY chunks.source_file HAVING {' AND '.join(having)}"
                " ORDER BY chunks.source_file LIMIT ?) AS page"
                " JOIN chunks c ON c.source_file = page.source_file"
                " GROUP BY c.source_file ORDER BY c.source_file",
                params,
            ).fetchall()
        next_cursor = rows[limit - 1][0] if len(rows) > limit else None
        return [
            (Path(row[0]), row[1], row[2], row[3], row[4] or 0, row[5] or 0, row[6] or 0)
            for row in rows[:limit]
        ], next_cursor

    async def get_source_summaries(self) -> dict[str, tuple[list[str], str]]:
        """Return ``{source_file_path_str: (heading_hierarchy, first_chunk_content)}``."""
        async with self._lease_read_db() as db:
            rows = db.execute(
                "SELECT f.source_file, f.content, f.heading_hierarchy FROM ("
                "  SELECT source_file, content, heading_hierarchy,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY source_file ORDER BY start_line, rowid"
                "         ) AS rn"
                "  FROM chunks"
                f") AS f WHERE f.rn = 1 AND {visible_path_sql('f.source_file')}"  # nosec B608
            ).fetchall()
        result: dict[str, tuple[list[str], str]] = {}
        for source, content, hh_json in rows:
            try:
                hh = list(json.loads(hh_json)) if hh_json else []
            except (json.JSONDecodeError, TypeError):
                hh = []
            result[source] = (hh, content or "")
        return result

    async def iter_chunks_for_audit(
        self,
        *,
        scope: str,
        source_exact: Path | None = None,
        source_prefix: Path | None = None,
        project_root: Path | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[ChunkAuditRow]:
        """Stream chunks in ``scope`` for a privacy audit walk."""
        if source_exact is not None and source_prefix is not None:
            raise ValueError(
                "iter_chunks_for_audit: source_exact and source_prefix are mutually exclusive"
            )
        if scope == "user" and project_root is not None:
            raise ValueError(
                "iter_chunks_for_audit: project_root must be None when scope='user' "
                "(user-tier rows have project_root IS NULL by contract)"
            )

        where_parts = ["COALESCE(scope, 'user') = ?"]
        params: list[object] = [scope]
        if project_root is not None:
            where_parts.append("project_root = ?")
            params.append(norm_path(project_root))
        if source_exact is not None:
            where_parts.append("source_file = ?")
            params.append(norm_path(source_exact))
        elif source_prefix is not None:
            prefix = norm_path(source_prefix)
            anchored = prefix.rstrip("/\\") + os.sep
            where_parts.append("substr(source_file, 1, ?) = ?")
            params.append(len(anchored))
            params.append(anchored)

        where_sql = " AND ".join(where_parts)

        last_id: str | None = None
        while True:
            if last_id is None:
                query = (
                    "SELECT id, source_file, content, COALESCE(scope, 'user'), "
                    "project_root FROM chunks "
                    f"WHERE {where_sql} ORDER BY id LIMIT ?"
                )
                batch_params = (*params, batch_size)
            else:
                query = (
                    "SELECT id, source_file, content, COALESCE(scope, 'user'), "
                    "project_root FROM chunks "
                    f"WHERE {where_sql} AND id > ? ORDER BY id LIMIT ?"
                )
                batch_params = (*params, last_id, batch_size)

            async with self._lease_read_db() as db:
                rows = db.execute(query, batch_params).fetchall()
            if not rows:
                return

            for row in rows:
                chunk_id, source_file, content, row_scope, row_project_root = row
                yield ChunkAuditRow(
                    chunk_id=str(chunk_id),
                    source=Path(source_file),
                    content=str(content),
                    scope=str(row_scope),
                    project_root=Path(row_project_root) if row_project_root else None,
                )
            last_id = str(rows[-1][0])
            if len(rows) < batch_size:
                return
