"""SQLite storage backend with FTS5 (BM25) + sqlite-vec (vector search)."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Sequence
from uuid import UUID

import sqlite_vec

from memtomem.config import StorageConfig
from memtomem.errors import StorageError
from memtomem.models import Chunk, ChunkMetadata, ChunkType, NamespaceFilter, SearchResult
from memtomem.storage import fts_tokenizer as _fts
from memtomem.storage.sqlite_helpers import (
    deserialize_f32,
    escape_like,
    namespace_sql,
    norm_path,
    placeholders,
    serialize_f32,
)
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_namespace import NamespaceOps, validate_namespace
from memtomem.storage.mixins import (
    AnalyticsMixin,
    EntityMixin,
    HistoryMixin,
    PolicyMixin,
    RelationMixin,
    ScratchMixin,
    SessionMixin,
)
from memtomem.storage.sqlite_schema import create_tables

logger = logging.getLogger(__name__)

__all__ = ["SqliteBackend", "validate_namespace"]


# Batch size for streaming rebuild_fts — bounds peak memory regardless of
# corpus size (issue #278). 1000 rows × typical chunk width stays well under
# a megabyte while keeping round-trip overhead negligible.
_REBUILD_FTS_BATCH_SIZE = 1000


def _rebuild_fts_retrieval(content: str, hierarchy_json: str) -> str:
    """Prefix ``content`` with its heading hierarchy for FTS indexing."""
    if hierarchy_json:
        try:
            h = json.loads(hierarchy_json)
            if h:
                return " > ".join(h) + "\n\n" + content
        except (ValueError, TypeError):
            pass
    return content


class SqliteBackend(
    SessionMixin,
    ScratchMixin,
    RelationMixin,
    AnalyticsMixin,
    HistoryMixin,
    EntityMixin,
    PolicyMixin,
):
    def __init__(
        self,
        config: StorageConfig,
        dimension: int = 768,
        embedding_provider: str = "",
        embedding_model: str = "",
        *,
        strict_dim_check: bool = True,
    ) -> None:
        self._config = config
        self._dimension = dimension
        self._embedding_provider = embedding_provider
        self._embedding_model = embedding_model
        # Relaxed mode is used by recovery tooling (``mm embedding-reset``)
        # to observe and fix a dim=0 / real-provider mismatch; production
        # entry points keep the default strict behavior so startup fails
        # fast with a remediation message. See issue #298.
        self._strict_dim_check = strict_dim_check
        self._db: sqlite3.Connection | None = None
        self._dim_mismatch: tuple[int, int] | None = None  # (stored, configured)
        self._model_mismatch: tuple[str, str, str, str] | None = (
            None  # (stored_prov, stored_model, cfg_prov, cfg_model)
        )
        self._meta: MetaManager | None = None
        self._ns: NamespaceOps | None = None
        self._in_transaction: bool = False
        # Invariant: _has_vec_table is True iff sqlite_master contains 'chunks_vec',
        # which holds iff self._dimension > 0. Maintained by initialize(),
        # reset_embedding_meta(), and reset_all() — all three must update this
        # flag in lockstep with the underlying DROP/CREATE.
        self._has_vec_table: bool = False

    async def initialize(self) -> None:
        db_path = Path(self._config.sqlite_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._db = sqlite3.connect(str(db_path), timeout=10)
        # Restrict DB file to owner-only access
        try:
            db_path.chmod(0o600)
        except OSError:
            pass  # May fail on some filesystems
        try:
            self._db.enable_load_extension(True)
            sqlite_vec.load(self._db)
            self._db.enable_load_extension(False)
        except Exception:
            self._db.close()
            self._db = None
            raise

        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA wal_autocheckpoint=1000")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")

        # Read-only connection pool for concurrent search operations
        self._read_pool: list[sqlite3.Connection] = []
        self._read_pool_idx = 0
        self._read_pool_lock = threading.Lock()
        for _ in range(3):
            rconn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
            rconn.execute("PRAGMA journal_mode=WAL")
            rconn.execute("PRAGMA query_only=ON")
            try:
                rconn.enable_load_extension(True)
                sqlite_vec.load(rconn)
                rconn.enable_load_extension(False)
            except Exception as exc:
                logger.warning("Failed to load sqlite-vec for read pool connection: %s", exc)
            self._read_pool.append(rconn)

        try:
            self._meta = MetaManager(self._get_db)
            self._ns = NamespaceOps(self._get_db, lambda: self._has_vec_table)

            self._dimension, self._dim_mismatch, self._model_mismatch = create_tables(
                self._db,
                self._meta,
                self._dimension,
                self._embedding_provider,
                self._embedding_model,
                strict_dim_check=self._strict_dim_check,
            )
            self._has_vec_table = (
                self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
                ).fetchone()
                is not None
            )
        except Exception:
            await self.close()
            raise

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise StorageError("Database not initialized. Call initialize() first.")
        return self._db

    def _get_read_db(self) -> sqlite3.Connection:
        """Return a read-only connection from the pool (round-robin, thread-safe)."""
        if not self._read_pool:
            return self._get_db()
        with self._read_pool_lock:
            conn = self._read_pool[self._read_pool_idx % len(self._read_pool)]
            self._read_pool_idx += 1
        return conn

    async def close(self) -> None:
        for rconn in getattr(self, "_read_pool", []):
            try:
                rconn.close()
            except Exception:
                logger.debug("Failed to close read pool connection", exc_info=True)
        if hasattr(self, "_read_pool"):
            self._read_pool.clear()
        if self._db:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("WAL checkpoint failed during close", exc_info=True)
            self._db.close()
            self._db = None

    # ---- transaction ---------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        """Async context manager for atomic multi-operation transactions.

        While inside this block, individual method commits/rollbacks are
        suppressed.  The CM commits on success or rolls back on failure.
        """
        if self._in_transaction:
            raise StorageError("Nested transactions are not supported")
        db = self._get_db()
        self._in_transaction = True
        try:
            yield
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            self._in_transaction = False

    # ---- meta delegation -----------------------------------------------------

    def _get_meta(self, key: str) -> str | None:
        assert self._meta is not None
        return self._meta.get_meta(key)

    def _set_meta(self, key: str, value: str) -> None:
        assert self._meta is not None
        self._meta.set_meta(key, value)

    def _get_stored_dimension(self) -> int | None:
        assert self._meta is not None
        return self._meta.get_stored_dimension()

    def _store_dimension(self, dim: int) -> None:
        assert self._meta is not None
        self._meta.store_dimension(dim)

    @property
    def stored_embedding_info(self) -> dict:
        """Return the embedding config actually stored in the DB."""
        assert self._meta is not None
        return self._meta.stored_embedding_info(
            self._dimension,
            self._embedding_provider,
            self._embedding_model,
        )

    @property
    def embedding_mismatch(self) -> dict | None:
        """Return mismatch info dict if stored embedding config differs from current config, else None."""
        if self._dim_mismatch is None and self._model_mismatch is None:
            return None
        stored_dim = self._dim_mismatch[0] if self._dim_mismatch else self._dimension
        cfg_dim = self._dim_mismatch[1] if self._dim_mismatch else self._dimension
        stored_prov = self._model_mismatch[0] if self._model_mismatch else self._embedding_provider
        stored_model = self._model_mismatch[1] if self._model_mismatch else self._embedding_model
        cfg_prov = self._model_mismatch[2] if self._model_mismatch else self._embedding_provider
        cfg_model = self._model_mismatch[3] if self._model_mismatch else self._embedding_model
        return {
            "dimension_mismatch": self._dim_mismatch is not None,
            "model_mismatch": self._model_mismatch is not None,
            "stored": {"dimension": stored_dim, "provider": stored_prov, "model": stored_model},
            "configured": {"dimension": cfg_dim, "provider": cfg_prov, "model": cfg_model},
        }

    def clear_embedding_mismatch(self) -> None:
        """Clear cached embedding mismatch flags.

        Call after resolving a mismatch either by resetting DB meta
        (handled automatically by ``reset_embedding_meta``) or by switching
        the runtime config to match stored DB values.
        """
        self._dim_mismatch = None
        self._model_mismatch = None

    async def reset_embedding_meta(
        self,
        dimension: int,
        provider: str = "",
        model: str = "",
    ) -> None:
        """Drop and recreate chunks_vec with *dimension*, updating all meta.

        This is the only sanctioned way to change the embedding model/dimension
        after initial creation.  All existing vector data is lost — a
        re-index is required afterwards.
        """
        assert self._meta is not None
        db = self._get_db()
        db.execute("DROP TABLE IF EXISTS chunks_vec")
        db.execute("DROP TABLE IF EXISTS chunks_vec_info")
        self._dimension = dimension
        self._meta.reset_embedding_meta(dimension, provider, model)
        if provider:
            self._embedding_provider = provider
        if model:
            self._embedding_model = model
        if self._dimension > 0:
            db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
                USING vec0(embedding float[{self._dimension}])
            """)
            self._has_vec_table = True
        else:
            self._has_vec_table = False
        db.commit()
        self.clear_embedding_mismatch()

    async def reset_vec_dimension(self, new_dimension: int) -> None:
        """Backward-compatible wrapper around reset_embedding_meta()."""
        await self.reset_embedding_meta(dimension=new_dimension)

    async def reset_all(self) -> dict[str, int]:
        """Drop all user data and reinitialize an empty schema.

        Deletes every row from chunks, FTS, vectors, and all auxiliary tables
        (access_log, query_history, sessions, etc.).  The ``_memtomem_meta``
        table is preserved so embedding config survives.

        Returns a dict mapping table name → number of deleted rows.
        """
        db = self._get_db()
        # Tables to clear, in dependency-safe order (children before parents).
        tables = [
            "session_events",
            "sessions",
            "working_memory",
            "chunk_relations",
            "chunk_entities",
            "access_log",
            "query_history",
            "namespace_metadata",
            "memory_policies",
            "health_snapshots",
        ]
        deleted: dict[str, int] = {}
        try:
            for tbl in tables:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
                ).fetchone()
                if exists:
                    count = db.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]  # noqa: S608
                    db.execute(f"DELETE FROM [{tbl}]")  # noqa: S608
                    deleted[tbl] = count

            # Core content tables
            chunk_count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            db.execute("DELETE FROM chunks")
            deleted["chunks"] = chunk_count

            # FTS virtual table — DELETE removes all content rows
            db.execute("DELETE FROM chunks_fts")
            deleted["chunks_fts"] = chunk_count

            # Vector virtual table — drop + recreate is safest for vec0
            db.execute("DROP TABLE IF EXISTS chunks_vec")
            db.execute("DROP TABLE IF EXISTS chunks_vec_info")
            if self._dimension > 0:
                db.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
                    USING vec0(embedding float[{self._dimension}])
                """)
                self._has_vec_table = True
            else:
                self._has_vec_table = False
            deleted["chunks_vec"] = chunk_count

            if not self._in_transaction:
                db.commit()
        except Exception as exc:
            if not self._in_transaction:
                db.rollback()
            raise StorageError(f"reset_all failed, transaction rolled back: {exc}") from exc
        return deleted

    # ---- chunk CRUD ----------------------------------------------------------

    async def upsert_chunks(self, chunks: Sequence[Chunk]) -> int:
        if not chunks:
            return 0

        db = self._get_db()
        try:
            chunk_ids = [str(c.id) for c in chunks]

            # Batch fetch existing {id: rowid} in a single query (P1)
            existing_rows = db.execute(
                f"SELECT id, rowid FROM chunks WHERE id IN ({placeholders(len(chunk_ids))})",
                chunk_ids,
            ).fetchall()
            existing_rowid_map = {row[0]: row[1] for row in existing_rows}

            to_update = [
                (c, existing_rowid_map[str(c.id)])
                for c in chunks
                if str(c.id) in existing_rowid_map
            ]
            to_insert = [c for c in chunks if str(c.id) not in existing_rowid_map]

            if to_update:
                db.executemany(
                    """UPDATE chunks SET content=?, content_hash=?, source_file=?,
                       heading_hierarchy=?, chunk_type=?, start_line=?, end_line=?,
                       language=?, tags=?, namespace=?, updated_at=?
                       WHERE id=?""",
                    [
                        (
                            c.content,
                            c.content_hash,
                            norm_path(c.metadata.source_file),
                            json.dumps(list(c.metadata.heading_hierarchy)),
                            c.metadata.chunk_type.value,
                            c.metadata.start_line,
                            c.metadata.end_line,
                            c.metadata.language,
                            json.dumps(list(c.metadata.tags)),
                            c.metadata.namespace,
                            c.updated_at.isoformat(timespec="seconds"),
                            str(c.id),
                        )
                        for c, _ in to_update
                    ],
                )
                db.executemany(
                    "UPDATE chunks_fts SET content=?, source_file=? WHERE rowid=?",
                    [
                        (
                            _fts.tokenize_for_fts(c.retrieval_content),
                            norm_path(c.metadata.source_file),
                            rowid,
                        )
                        for c, rowid in to_update
                    ],
                )
                vec_updates = [(c, rowid) for c, rowid in to_update if c.embedding]
                if vec_updates and self._has_vec_table:
                    db.executemany(
                        "UPDATE chunks_vec SET embedding=? WHERE rowid=?",
                        [(serialize_f32(c.embedding), rowid) for c, rowid in vec_updates],  # type: ignore[arg-type]
                    )

            if to_insert:
                db.executemany(
                    """INSERT INTO chunks
                       (id, content, content_hash, source_file, heading_hierarchy,
                        chunk_type, start_line, end_line, language, tags,
                        namespace, created_at, updated_at,
                        overlap_before, overlap_after)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (
                            str(c.id),
                            c.content,
                            c.content_hash,
                            norm_path(c.metadata.source_file),
                            json.dumps(list(c.metadata.heading_hierarchy)),
                            c.metadata.chunk_type.value,
                            c.metadata.start_line,
                            c.metadata.end_line,
                            c.metadata.language,
                            json.dumps(list(c.metadata.tags)),
                            c.metadata.namespace,
                            c.created_at.isoformat(timespec="seconds"),
                            c.updated_at.isoformat(timespec="seconds"),
                            c.metadata.overlap_before,
                            c.metadata.overlap_after,
                        )
                        for c in to_insert
                    ],
                )
                # Fetch newly assigned rowids in a single query
                new_ids = [str(c.id) for c in to_insert]
                new_rows = db.execute(
                    f"SELECT id, rowid FROM chunks WHERE id IN ({placeholders(len(new_ids))})",
                    new_ids,
                ).fetchall()
                new_rowid_map = {row[0]: row[1] for row in new_rows}

                # Defensive cleanup: remove orphaned FTS/vec entries for these
                # rowids. Orphans can arise from interrupted concurrent operations
                # (e.g. MCP + Web server sharing the same DB).
                new_rowids = list(new_rowid_map.values())
                db.execute(
                    f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders(len(new_rowids))})",
                    new_rowids,
                )
                if self._has_vec_table:
                    db.execute(
                        f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders(len(new_rowids))})",
                        new_rowids,
                    )

                db.executemany(
                    "INSERT INTO chunks_fts(rowid, content, source_file) VALUES (?,?,?)",
                    [
                        (
                            new_rowid_map[str(c.id)],
                            _fts.tokenize_for_fts(c.retrieval_content),
                            norm_path(c.metadata.source_file),
                        )
                        for c in to_insert
                        if str(c.id) in new_rowid_map
                    ],
                )
                vec_inserts = [
                    (new_rowid_map[str(c.id)], serialize_f32(c.embedding))
                    for c in to_insert
                    if c.embedding and str(c.id) in new_rowid_map
                ]
                if vec_inserts and self._has_vec_table:
                    db.executemany(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES (?,?)",
                        vec_inserts,
                    )

            if not self._in_transaction:
                db.commit()
        except Exception as exc:
            if not self._in_transaction:
                db.rollback()
            if "Dimension mismatch" in str(exc):
                raise StorageError(
                    f"Embedding dimension mismatch during upsert: "
                    f"DB expects {self._dimension}d vectors. "
                    f"Run 'mm embedding-reset' (CLI) or mem_embedding_reset (MCP) to resolve."
                ) from exc
            raise StorageError(f"upsert_chunks failed, transaction rolled back: {exc}") from exc
        return len(chunks)

    async def get_chunk(self, chunk_id: UUID) -> Chunk | None:
        db = self._get_read_db()
        row = db.execute("SELECT * FROM chunks WHERE id=?", (str(chunk_id),)).fetchone()
        if not row:
            return None
        return self._row_to_chunk(row)

    async def get_chunks_batch(self, chunk_ids: Sequence[UUID]) -> dict[UUID, Chunk]:
        """Fetch multiple chunks by ID in a single query."""
        if not chunk_ids:
            return {}
        db = self._get_read_db()
        ids_str = [str(cid) for cid in chunk_ids]
        rows = db.execute(
            f"SELECT * FROM chunks WHERE id IN ({placeholders(len(ids_str))})",
            ids_str,
        ).fetchall()
        return {UUID(row[0]): self._row_to_chunk(row) for row in rows}

    async def delete_chunks(self, chunk_ids: Sequence[UUID]) -> int:
        if not chunk_ids:
            return 0

        db = self._get_db()
        ids_str = [str(cid) for cid in chunk_ids]

        # Batch fetch rowids in a single query (P2)
        rows = db.execute(
            f"SELECT id, rowid FROM chunks WHERE id IN ({placeholders(len(ids_str))})",
            ids_str,
        ).fetchall()

        if not rows:
            return 0

        found_ids = [row[0] for row in rows]
        rowids = [row[1] for row in rows]

        try:
            db.execute(
                f"DELETE FROM chunks WHERE id IN ({placeholders(len(found_ids))})", found_ids
            )
            db.execute(
                f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders(len(rowids))})", rowids
            )
            if self._has_vec_table:
                db.execute(
                    f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders(len(rowids))})", rowids
                )
            if not self._in_transaction:
                db.commit()
        except Exception as exc:
            if not self._in_transaction:
                db.rollback()
            raise StorageError(f"delete_chunks failed, transaction rolled back: {exc}") from exc
        return len(rows)

    async def delete_by_source(self, source_file: Path) -> int:
        db = self._get_db()
        rows = db.execute(
            "SELECT id, rowid FROM chunks WHERE source_file=?",
            (norm_path(source_file),),
        ).fetchall()

        if not rows:
            return 0

        ids = [row[0] for row in rows]
        rowids = [row[1] for row in rows]

        try:
            db.execute(f"DELETE FROM chunks WHERE id IN ({placeholders(len(ids))})", ids)
            db.execute(
                f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders(len(rowids))})", rowids
            )
            if self._has_vec_table:
                db.execute(
                    f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders(len(rowids))})", rowids
                )
            if not self._in_transaction:
                db.commit()
        except Exception as exc:
            if not self._in_transaction:
                db.rollback()
            raise StorageError(f"delete_by_source failed, transaction rolled back: {exc}") from exc
        return len(rows)

    async def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from chunks table using current tokenizer.

        Returns the number of rows rebuilt.

        Runs the heavy I/O in a worker thread via :func:`asyncio.to_thread`
        so the event loop stays responsive during the rebuild, and streams
        rows in batches of ``_REBUILD_FTS_BATCH_SIZE`` so memory stays bounded
        even for corpora with hundreds of thousands of chunks (issue #278).
        The worker opens its own writer connection against the same SQLite
        file; WAL + SQLite's file-level lock serialise it against writes on
        the main connection, so the rebuild is atomic and independent of any
        transaction the main connection may hold.
        """
        assert self._db is not None
        db_path = str(Path(self._config.sqlite_path).expanduser())

        def _run() -> int:
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("DELETE FROM chunks_fts")
                cursor = conn.execute(
                    "SELECT rowid, content, source_file, heading_hierarchy FROM chunks"
                )
                total = 0
                try:
                    while True:
                        batch = cursor.fetchmany(_REBUILD_FTS_BATCH_SIZE)
                        if not batch:
                            break
                        conn.executemany(
                            "INSERT INTO chunks_fts(rowid, content, source_file) VALUES (?,?,?)",
                            [
                                (
                                    r[0],
                                    _fts.tokenize_for_fts(_rebuild_fts_retrieval(r[1], r[3])),
                                    r[2],
                                )
                                for r in batch
                            ],
                        )
                        total += len(batch)
                finally:
                    cursor.close()
                conn.commit()
                return total
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await asyncio.to_thread(_run)

    async def get_embeddings_for_chunks(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        """Fetch embeddings for a list of chunk IDs. Returns {id: embedding}."""
        if not chunk_ids or not self._has_vec_table:
            return {}
        db = self._db
        assert db is not None
        rows = db.execute(
            f"""SELECT c.id, v.embedding FROM chunks c
                JOIN chunks_vec v ON v.rowid = c.rowid
                WHERE c.id IN ({placeholders(len(chunk_ids))})""",
            chunk_ids,
        ).fetchall()
        result = {}
        for row in rows:
            try:
                result[row[0]] = deserialize_f32(row[1])
            except Exception:
                logger.warning(
                    "Failed to deserialize embedding for chunk %s",
                    row[0],
                    exc_info=True,
                )
        return result

    # ---- search --------------------------------------------------------------

    async def bm25_search(
        self,
        query: str,
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
    ) -> list[SearchResult]:
        db = self._get_read_db()
        try:
            ns_clause = ""
            ns_params: list = []
            if namespace_filter:
                frag, ns_params = namespace_sql(namespace_filter)
                if frag:
                    ns_clause = f"AND c.{frag}"

            sql = f"""SELECT c.id, c.content, c.content_hash, c.source_file,
                          c.heading_hierarchy, c.chunk_type, c.start_line, c.end_line,
                          c.language, c.tags, c.namespace, c.created_at, c.updated_at, sub.rank
                   FROM (
                       SELECT rowid, rank
                       FROM chunks_fts
                       WHERE chunks_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?
                   ) sub
                   JOIN chunks c ON c.rowid = sub.rowid {ns_clause}
                   ORDER BY sub.rank"""

            # Try AND first (default FTS5 behaviour)
            fts_query = _fts.tokenize_for_fts(query, for_query=True)
            rows = db.execute(sql, [fts_query, top_k] + ns_params).fetchall()

            # Fall back to OR if AND returns nothing and query has multiple terms
            if not rows and " " in query.strip():
                fts_query_or = _fts.tokenize_for_fts(query, for_query=True, use_or=True)
                rows = db.execute(sql, [fts_query_or, top_k] + ns_params).fetchall()

        except sqlite3.OperationalError:
            raise

        return [
            SearchResult(
                chunk=self._row_to_chunk(row[:13]),
                score=abs(row[13]),
                rank=rank_idx + 1,
                source="bm25",
            )
            for rank_idx, row in enumerate(rows)
        ]

    async def dense_search(
        self,
        embedding: list[float],
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
    ) -> list[SearchResult]:
        # bm25-only mode (dimension=0) — no chunks_vec table to query. Return
        # early instead of raising OperationalError that the search pipeline
        # would log as a misleading "Dense search unavailable" warning.
        if not self._has_vec_table:
            return []
        db = self._get_read_db()

        ns_clause = ""
        ns_params: list = []
        if namespace_filter:
            frag, ns_params = namespace_sql(namespace_filter)
            if frag:
                ns_clause = f"AND c.{frag}"

        import sqlite3 as _sqlite3

        try:
            rows = db.execute(
                f"""SELECT c.id, c.content, c.content_hash, c.source_file,
                          c.heading_hierarchy, c.chunk_type, c.start_line, c.end_line,
                          c.language, c.tags, c.namespace, c.created_at, c.updated_at, sub.distance
                   FROM (
                       SELECT rowid, distance
                       FROM chunks_vec
                       WHERE embedding MATCH ?
                       ORDER BY distance
                       LIMIT ?
                   ) sub
                   JOIN chunks c ON c.rowid = sub.rowid {ns_clause}
                   ORDER BY sub.distance""",
                [serialize_f32(embedding), top_k] + ns_params,
            ).fetchall()
        except _sqlite3.OperationalError as exc:
            if "Dimension mismatch" in str(exc):
                raise ValueError(
                    f"Embedding dimension mismatch: query has {len(embedding)}d "
                    f"but DB expects {self._dimension}d. "
                    f"Check MEMTOMEM_EMBEDDING__MODEL / MEMTOMEM_EMBEDDING__DIMENSION."
                ) from exc
            raise

        return [
            SearchResult(
                chunk=self._row_to_chunk(row[:13]),
                score=1.0 / (1.0 + row[13]),
                rank=rank_idx + 1,
                source="dense",
            )
            for rank_idx, row in enumerate(rows)
        ]

    # ---- query helpers -------------------------------------------------------

    async def get_chunk_hashes(self, source_file: Path) -> dict[str, str]:
        db = self._get_db()
        rows = db.execute(
            "SELECT id, content_hash FROM chunks WHERE source_file=?",
            (norm_path(source_file),),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_content_hash_to_id(self) -> dict[str, str]:
        """Return ``{content_hash: chunk_id}`` for every chunk in the DB.

        Used by import's ``on_conflict`` logic to detect collisions before
        upsert. When multiple rows share a hash (existing pre-v2 duplicates),
        an arbitrary row id wins — callers treat this as "a UUID to reuse",
        not a unique mapping.
        """
        db = self._get_read_db()
        rows = db.execute("SELECT content_hash, id FROM chunks").fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_stats(self) -> dict[str, int]:
        db = self._get_read_db()
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sources = db.execute("SELECT COUNT(DISTINCT source_file) FROM chunks").fetchone()[0]
        return {"total_chunks": total, "total_sources": sources}

    async def get_chunk_size_distribution(
        self,
        source_file: Path | None = None,
    ) -> list[dict]:
        """Return chunk count per token-size bucket.

        Token estimate: LENGTH(content) / 3.
        If source_file is given, filter to that source only.
        """
        db = self._get_db()
        where = ""
        params: list = []
        if source_file is not None:
            where = "WHERE source_file = ?"
            params.append(norm_path(source_file))

        rows = db.execute(
            "SELECT "
            "  CASE "
            "    WHEN LENGTH(content)/3 < 32   THEN '0-32' "
            "    WHEN LENGTH(content)/3 < 64   THEN '32-64' "
            "    WHEN LENGTH(content)/3 < 128  THEN '64-128' "
            "    WHEN LENGTH(content)/3 < 256  THEN '128-256' "
            "    WHEN LENGTH(content)/3 < 512  THEN '256-512' "
            "    WHEN LENGTH(content)/3 < 1024 THEN '512-1024' "
            "    ELSE '1024+' "
            "  END AS bucket, "
            f"  COUNT(*) AS cnt FROM chunks {where} GROUP BY bucket",
            params,
        ).fetchall()
        ordered = ["0-32", "32-64", "64-128", "128-256", "256-512", "512-1024", "1024+"]
        counts = {row[0]: row[1] for row in rows}
        return [{"bucket": b, "count": counts.get(b, 0)} for b in ordered]

    async def list_chunks_by_source(self, source_file: Path, limit: int = 50) -> list[Chunk]:
        db = self._get_read_db()
        rows = db.execute(
            "SELECT * FROM chunks WHERE source_file=? ORDER BY start_line LIMIT ?",
            (norm_path(source_file), limit),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    async def list_chunks_by_sources(
        self,
        source_files: Sequence[Path],
        limit_per_file: int = 10000,
    ) -> dict[Path, list[Chunk]]:
        """Batch-fetch chunks for multiple source files in a single query."""
        if not source_files:
            return {}

        db = self._get_read_db()
        norm_paths = [norm_path(sf) for sf in source_files]

        rows = db.execute(
            f"SELECT * FROM chunks WHERE source_file IN ({placeholders(len(norm_paths))}) "
            "ORDER BY source_file, start_line",
            norm_paths,
        ).fetchall()

        result: dict[Path, list[Chunk]] = {sf: [] for sf in source_files}
        norm_to_path = {norm_path(sf): sf for sf in source_files}

        for row in rows:
            chunk = self._row_to_chunk(row)
            sf_key = norm_to_path.get(str(chunk.metadata.source_file))
            if sf_key is not None and len(result[sf_key]) < limit_per_file:
                result[sf_key].append(chunk)

        return result

    async def recall_chunks(
        self,
        since=None,
        until=None,
        source_filter: str | None = None,
        limit: int = 20,
        namespace_filter: NamespaceFilter | None = None,
    ) -> list[Chunk]:
        db = self._get_read_db()
        conditions: list[str] = []
        params: list[object] = []

        if since is not None:
            conditions.append("created_at >= ?")
            params.append(since.isoformat())
        if until is not None:
            conditions.append("created_at < ?")
            params.append(until.isoformat())
        if source_filter is not None:
            conditions.append("source_file LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(source_filter)}%")
        if namespace_filter is not None:
            frag, ns_params = namespace_sql(namespace_filter)
            if frag:
                conditions.append(frag)
                params.extend(ns_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = db.execute(
            f"SELECT * FROM chunks {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    async def get_all_source_files(self) -> set[Path]:
        db = self._get_db()
        rows = db.execute("SELECT DISTINCT source_file FROM chunks").fetchall()
        return {Path(row[0]) for row in rows}

    async def get_source_files_with_counts(
        self,
    ) -> list[tuple[Path, int, str | None, str | None, int, int, int]]:
        """Return (path, chunk_count, last_updated, namespaces, avg_tokens, min_tokens, max_tokens)."""
        db = self._get_db()
        rows = db.execute(
            "SELECT source_file, COUNT(*), MAX(updated_at), GROUP_CONCAT(DISTINCT namespace),"
            " CAST(AVG(LENGTH(content)/3) AS INTEGER),"
            " MIN(LENGTH(content)/3),"
            " MAX(LENGTH(content)/3)"
            " FROM chunks GROUP BY source_file ORDER BY source_file"
        ).fetchall()
        return [
            (Path(row[0]), row[1], row[2], row[3], row[4] or 0, row[5] or 0, row[6] or 0)
            for row in rows
        ]

    async def get_tag_counts(self) -> list[tuple[str, int]]:
        db = self._get_read_db()
        rows = db.execute(
            "SELECT value, COUNT(*) as cnt "
            "FROM chunks, json_each(chunks.tags) "
            "GROUP BY value ORDER BY cnt DESC"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    async def increment_access(self, chunk_ids: Sequence[UUID]) -> None:
        """Increment access_count and update last_accessed_at for given chunks."""
        if not chunk_ids:
            return
        from datetime import datetime, timezone

        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.executemany(
            "UPDATE chunks SET access_count = access_count + 1, last_accessed_at = ? WHERE id = ?",
            [(now, str(cid)) for cid in chunk_ids],
        )
        db.commit()

    async def get_access_counts(self, chunk_ids: Sequence[UUID]) -> dict[str, int]:
        """Return access_count for the given chunk IDs."""
        if not chunk_ids:
            return {}
        db = self._get_read_db()
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = db.execute(
            f"SELECT id, access_count FROM chunks WHERE id IN ({placeholders})",
            [str(cid) for cid in chunk_ids],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ---- session, scratch, relations, tags, history, analytics ──────────
    # These methods are provided by Mixin classes:
    #   SessionMixin, ScratchMixin, RelationMixin, AnalyticsMixin, HistoryMixin
    # See storage/mixins/ for implementations.

    # ---- REMOVED: session methods (now in SessionMixin) ──────────────
    # ---- REMOVED: scratch methods (now in ScratchMixin) ──────────────
    # ---- REMOVED: relation + tag methods (now in RelationMixin) ──────
    # ---- REMOVED: history methods (now in HistoryMixin) ──────────────
    # ---- REMOVED: analytics methods (now in AnalyticsMixin) ──────────

    # ---- namespace delegation ────────────────────────────────────────
    # (kept here — not a mixin candidate due to _ns dependency)

    # ---- namespace delegation ------------------------------------------------

    async def list_namespaces(self) -> list[tuple[str, int]]:
        assert self._ns is not None
        return await self._ns.list_namespaces()

    async def count_chunks_by_ns_prefix(self, prefixes: Sequence[str]) -> int:
        assert self._ns is not None
        return await self._ns.count_chunks_by_ns_prefix(prefixes)

    async def delete_by_namespace(self, namespace: str) -> int:
        assert self._ns is not None
        return await self._ns.delete_by_namespace(namespace)

    async def rename_namespace(self, old: str, new: str) -> int:
        assert self._ns is not None
        return await self._ns.rename_namespace(old, new)

    async def get_namespace_meta(self, namespace: str) -> dict | None:
        assert self._ns is not None
        return await self._ns.get_namespace_meta(namespace)

    async def set_namespace_meta(
        self,
        namespace: str,
        description: str | None = None,
        color: str | None = None,
    ) -> None:
        assert self._ns is not None
        return await self._ns.set_namespace_meta(namespace, description, color)

    async def list_namespace_meta(self) -> list[dict]:
        assert self._ns is not None
        return await self._ns.list_namespace_meta()

    async def assign_namespace(
        self,
        namespace: str,
        source_filter: str | None = None,
        old_namespace: str | None = None,
    ) -> int:
        assert self._ns is not None
        return await self._ns.assign_namespace(namespace, source_filter, old_namespace)

    # ---- row deserialization -------------------------------------------------

    def _row_to_chunk(self, row: tuple) -> Chunk:
        # Core 13 columns + optional personalization columns (access_count, use_count, last_accessed_at)
        (
            chunk_id,
            content,
            content_hash,
            source_file,
            heading_hierarchy,
            chunk_type,
            start_line,
            end_line,
            language,
            tags,
            namespace,
            created_at,
            updated_at,
        ) = row[:13]

        from datetime import datetime, timezone

        # --- heading_hierarchy ---
        try:
            hh = tuple(json.loads(heading_hierarchy))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted heading_hierarchy for chunk %s", chunk_id)
            hh = ()

        # --- chunk_type ---
        try:
            ct = ChunkType(chunk_type)
        except ValueError:
            logger.warning("Unknown chunk_type '%s' for chunk %s", chunk_type, chunk_id)
            ct = ChunkType.RAW_TEXT

        # --- tags ---
        try:
            parsed_tags = tuple(json.loads(tags))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupted tags for chunk %s", chunk_id)
            parsed_tags = ()

        # Overlap columns (may not exist in older DBs — columns 16,17 after personalization cols 13,14,15)
        ob, oa = 0, 0
        if len(row) >= 18:
            ob = row[16] or 0
            oa = row[17] or 0

        metadata = ChunkMetadata(
            source_file=Path(source_file),
            heading_hierarchy=hh,
            chunk_type=ct,
            start_line=start_line,
            end_line=end_line,
            language=language,
            tags=parsed_tags,
            namespace=namespace,
            overlap_before=ob,
            overlap_after=oa,
        )

        # --- timestamps (always timezone-aware) ---
        try:
            ca = datetime.fromisoformat(created_at)
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning("Corrupted created_at for chunk %s", chunk_id)
            ca = datetime.now(timezone.utc)

        try:
            ua = datetime.fromisoformat(updated_at)
            if ua.tzinfo is None:
                ua = ua.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning("Corrupted updated_at for chunk %s", chunk_id)
            ua = datetime.now(timezone.utc)

        return Chunk(
            content=content,
            metadata=metadata,
            id=UUID(chunk_id),
            content_hash=content_hash,
            created_at=ca,
            updated_at=ua,
        )

    # ---- search history, importance, analytics, sessions, scratch, relations ──
    # All provided by Mixin classes. See storage/mixins/ for implementations.
