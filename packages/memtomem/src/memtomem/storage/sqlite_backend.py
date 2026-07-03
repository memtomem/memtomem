"""SQLite storage backend with FTS5 (BM25) + sqlite-vec (vector search)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Sequence
from uuid import UUID

import sqlite_vec

from memtomem.config import StorageConfig
from memtomem.errors import StorageError
from memtomem.storage.base import ChunkAuditRow
from memtomem.models import (
    Chunk,
    ChunkMetadata,
    ChunkType,
    NamespaceFilter,
    ScopeFilter,
    SearchResult,
)
from memtomem.storage import fts_tokenizer as _fts
from memtomem.storage.sqlite_helpers import (
    deserialize_f32,
    escape_like,
    namespace_sql,
    norm_path,
    placeholders,
    serialize_f32,
)
from memtomem.storage.orphan_gc import (
    OrphanProjectReport,
    SweepResult,
    find_orphan_project_roots,
    sweep_orphan_project_root,
)
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_namespace import NamespaceOps
from memtomem.storage.sqlite_scope import scope_context_sql, scope_sort_priority_case
from memtomem.storage.mixins import (
    AnalyticsMixin,
    EntityMixin,
    HistoryMixin,
    IdempotencyMixin,
    PolicyMixin,
    RelationMixin,
    ScheduleMixin,
    ScratchMixin,
    SessionMixin,
    ShareLinkMixin,
)
from memtomem.storage.sqlite_schema import create_tables

logger = logging.getLogger(__name__)

__all__ = ["SqliteBackend"]


# Batch size for streaming rebuild_fts — bounds peak memory regardless of
# corpus size (issue #278). 1000 rows × typical chunk width stays well under
# a megabyte while keeping round-trip overhead negligible.
_REBUILD_FTS_BATCH_SIZE = 1000


# Prefix for per-source AI summary records in the ``_memtomem_meta`` k/v
# table. The full key is ``ai_summary:<resolved-NFC-path>`` so a prefix
# scan (``key LIKE 'ai_summary:%'``) cleanly separates summary rows from
# the embedding-meta keys that share the same table.
_AI_SUMMARY_KEY_PREFIX = "ai_summary:"


def _ai_summary_key(source_file: Path) -> str:
    return f"{_AI_SUMMARY_KEY_PREFIX}{norm_path(source_file)}"


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
    IdempotencyMixin,
    RelationMixin,
    ShareLinkMixin,
    AnalyticsMixin,
    HistoryMixin,
    EntityMixin,
    PolicyMixin,
    ScheduleMixin,
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
        # In-process serialization for tag-management read-modify-write
        # paths (rename / delete / merge) and ``auto_tag_storage`` so they
        # can't interleave on the same chunks.tags column. Cross-process
        # safety still falls back to SQLite's WAL file lock — this is a
        # single-process invariant only.
        self._tag_write_lock: asyncio.Lock = asyncio.Lock()
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
    def db_path(self) -> Path:
        """Filesystem path of the SQLite database file (``~`` expanded).

        Mirrors how :meth:`initialize` resolves ``sqlite_path``. Used by the
        export/import provenance marker to locate its sidecar key next to the
        DB (``memtomem.provenance.key_path_for_db``).
        """
        return Path(self._config.sqlite_path).expanduser()

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
        table is preserved so embedding config survives, *except* for
        ``ai_summary:*`` rows — those carry user-derived prose generated
        from indexed source content and must respect the "Delete ALL data"
        contract just like the chunks they were summarising.

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
                    count = db.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
                    db.execute(f"DELETE FROM [{tbl}]")
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

            # AI summary cache lives in ``_memtomem_meta`` (the table is
            # otherwise preserved for embedding config). Rows under the
            # ``ai_summary:`` prefix carry LLM-generated prose derived from
            # indexed sources, so they must be cleared with the rest of
            # the user data — leaving them behind would let
            # ``get_all_ai_summaries`` keep returning content for chunks
            # that no longer exist, and break the "Delete ALL data" UI
            # contract.
            ai_summary_count = db.execute(
                "SELECT COUNT(*) FROM _memtomem_meta WHERE key LIKE ?",
                (f"{_AI_SUMMARY_KEY_PREFIX}%",),
            ).fetchone()[0]
            db.execute(
                "DELETE FROM _memtomem_meta WHERE key LIKE ?",
                (f"{_AI_SUMMARY_KEY_PREFIX}%",),
            )
            deleted["ai_summaries"] = ai_summary_count

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
                       language=?, tags=?, namespace=?, updated_at=?,
                       valid_from_unix=?, valid_to_unix=?,
                       scope=?, project_root=?
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
                            c.metadata.valid_from_unix,
                            c.metadata.valid_to_unix,
                            c.metadata.scope,
                            str(c.metadata.project_root) if c.metadata.project_root else None,
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
                # ``INSERT OR IGNORE``: the UNIQUE index on
                # ``(namespace, source_file, content_hash, start_line)`` is
                # the multi-process race guard for #691. Two processes
                # (mm web watcher + mm CLI / MCP) each call ``upsert_chunks``
                # with their own freshly-generated chunk ids; whichever
                # commits first wins, the loser's row is silently dropped.
                # The follow-up ``SELECT id, rowid WHERE id IN (...)`` below
                # then naturally skips dropped ids when populating
                # ``chunks_fts`` and ``chunks_vec``, so no orphan sidecars
                # are created for race losers.
                db.executemany(
                    """INSERT OR IGNORE INTO chunks
                       (id, content, content_hash, source_file, heading_hierarchy,
                        chunk_type, start_line, end_line, language, tags,
                        namespace, created_at, updated_at,
                        overlap_before, overlap_after,
                        valid_from_unix, valid_to_unix,
                        scope, project_root)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                            c.metadata.valid_from_unix,
                            c.metadata.valid_to_unix,
                            c.metadata.scope,
                            str(c.metadata.project_root) if c.metadata.project_root else None,
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
                # (e.g. MCP + Web server sharing the same DB). Skipped when
                # ``new_rowid_map`` is empty — that path fires when every row in
                # ``to_insert`` was dropped by ``INSERT OR IGNORE`` (the #691
                # race-loser case): there are no rowids to scrub or repopulate.
                new_rowids = list(new_rowid_map.values())
                if new_rowids:
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

        # Batch fetch rowids + source_file in a single query (P2). The
        # ``source_file`` column travels along so that *after* the delete
        # we can check which sources lost their last chunk and need their
        # AI summary cache cleared — partial deletions leave the summary
        # in place (the signature drifts and gets refreshed on the next
        # reindex), but a fully-emptied source has no future reindex to
        # rely on, so its cached prose has to go now.
        rows = db.execute(
            f"SELECT id, rowid, source_file FROM chunks WHERE id IN ({placeholders(len(ids_str))})",
            ids_str,
        ).fetchall()

        if not rows:
            return 0

        found_ids = [row[0] for row in rows]
        rowids = [row[1] for row in rows]
        affected_sources = {row[2] for row in rows if row[2]}

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

            # AI summary cache cleanup for sources that just lost their
            # last chunk — but only when this delete is the *final*
            # word. The reindex path in ``IndexingEngine._index_file``
            # wraps a delete+upsert pair in a single transaction; if a
            # source has no unchanged chunks the delete temporarily
            # empties it before the upsert lands, and clearing here
            # would drop a still-valid summary. The fail-soft contract
            # for AI summaries (LLM error → keep old prose, indexing
            # continues) requires that we don't pre-emptively flush
            # the cache for what is really a rewrite.
            #
            # Skip cleanup when ``_in_transaction`` is True; the
            # outer scope (post-upsert ``maybe_update_ai_summary``,
            # explicit ``delete_by_source``, or session-end
            # ``reset_all``) is responsible for resolving the cache
            # state once the multi-step operation completes. Standalone
            # ``delete_chunks`` calls (web chunk-delete fallback,
            # dedup, decay sweeps) hit the cleanup branch as before.
            #
            # ``source_file`` is already in normalised form in the
            # chunks table (see ``upsert_chunks`` → ``norm_path``),
            # so we feed it directly to the meta-key prefix without
            # re-resolving (resolving here would mismatch on macOS
            # symlink cases like ``/tmp`` → ``/private/tmp`` because
            # the original chunk row was stored as resolved already).
            if not self._in_transaction:
                for source_norm in affected_sources:
                    remaining = db.execute(
                        "SELECT 1 FROM chunks WHERE source_file=? LIMIT 1",
                        (source_norm,),
                    ).fetchone()
                    if remaining is None:
                        db.execute(
                            "DELETE FROM _memtomem_meta WHERE key=?",
                            (f"{_AI_SUMMARY_KEY_PREFIX}{source_norm}",),
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
            # Even with no chunks, an orphaned ai_summary cache row from a
            # prior generation could linger — clear it unconditionally so
            # the source-tab preview doesn't keep referencing a deleted
            # file. Cheap (single row by primary key) so we don't gate it.
            db.execute(
                "DELETE FROM _memtomem_meta WHERE key=?",
                (_ai_summary_key(source_file),),
            )
            if not self._in_transaction:
                db.commit()
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
            db.execute(
                "DELETE FROM _memtomem_meta WHERE key=?",
                (_ai_summary_key(source_file),),
            )
            if not self._in_transaction:
                db.commit()
        except Exception as exc:
            if not self._in_transaction:
                db.rollback()
            raise StorageError(f"delete_by_source failed, transaction rolled back: {exc}") from exc
        return len(rows)

    async def find_orphan_project_roots(self) -> list[OrphanProjectReport]:
        """Detect project-tier chunks whose ``project_root`` no longer exists on disk.

        Thin async wrapper around :func:`memtomem.storage.orphan_gc.find_orphan_project_roots`
        so the CLI (``mm gc orphan-projects``) can call it via the
        Components stack while the underlying pure function stays
        unit-testable against a synthetic ``sqlite3.Connection``. See
        ADR-0011 follow-up #884 for the surface contract.
        """
        return find_orphan_project_roots(self._get_read_db())

    async def sweep_orphan_project_root(self, project_root: str) -> SweepResult:
        """Delete every project-tier chunk under ``project_root`` in one transaction.

        Thin async wrapper around
        :func:`memtomem.storage.orphan_gc.sweep_orphan_project_root` that
        threads in :attr:`_has_vec_table` so the helper need not poke at
        the backend's invariants. See ADR-0011 follow-up #884.
        """
        return sweep_orphan_project_root(
            self._get_db(),
            project_root,
            has_vec_table=self._has_vec_table,
        )

    async def list_scopes_by_source(self, source_file: Path) -> set[str]:
        """Return the distinct persisted scopes for chunks from ``source_file``."""
        db = self._get_read_db()
        rows = db.execute(
            "SELECT DISTINCT COALESCE(scope, 'user') FROM chunks WHERE source_file=?",
            (norm_path(source_file),),
        ).fetchall()
        return {str(row[0] or "user") for row in rows}

    async def list_scopes_by_namespace(self, namespace: str) -> set[str]:
        """Return the distinct persisted scopes for chunks in ``namespace``.

        ADR-0011 PR-D: ``mem_delete(namespace=...)`` uses this to refuse
        bulk deletes that would remove ``project_shared`` chunks without
        ``confirm_project_shared=True``. Project-shared memories can sit
        in the same default namespace as user memories, so the
        namespace string alone does not imply the trust tier.
        """
        db = self._get_read_db()
        rows = db.execute(
            "SELECT DISTINCT COALESCE(scope, 'user') FROM chunks WHERE namespace=?",
            (namespace,),
        ).fetchall()
        return {str(row[0] or "user") for row in rows}

    async def list_sources_by_namespace(self, namespace: str) -> list[Path]:
        """Return the distinct source files holding chunks in ``namespace``.

        Issue #1570: ``mem_delete(namespace=...)`` locks each of these files
        before the bulk delete so a concurrent per-file CRUD span cannot
        re-index one of them afterwards and resurrect the deleted rows.
        """
        db = self._get_read_db()
        rows = db.execute(
            "SELECT DISTINCT source_file FROM chunks WHERE namespace=?",
            (namespace,),
        ).fetchall()
        return [Path(str(row[0])) for row in rows]

    async def iter_chunks_for_audit(
        self,
        *,
        scope: str,
        source_exact: Path | None = None,
        source_prefix: Path | None = None,
        project_root: Path | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[ChunkAuditRow]:
        """Stream chunks in ``scope`` for a privacy audit walk.

        Independent of search / recall: no embedding lookup, no tag
        decode, no UI-side ordering. Uses ``ORDER BY id`` (PK) with a
        keyset cursor so pagination stays stable even if rows mutate
        between batches (the audit is read-only by contract, but cursor
        stability is cheaper to guarantee than to debug).

        ``source_exact`` and ``source_prefix`` are mutually exclusive
        contracts owned by the caller (CLI ``--source`` resolver). Both
        are normalised via :func:`norm_path` before the query, mirroring
        the storage layer's existing source-path equality contract used
        by :meth:`list_chunks_by_source` and friends.

        ``project_root`` is the ADR-0011 / ADR-0016 / issue #934 cross-
        project isolation gate. When ``scope`` is a project tier
        (``project_shared`` / ``project_local``) and multiple project
        roots share the same SQLite DB, the caller passes the current
        project root so the audit only walks rows owned by that root.
        ``None`` (the default) means "no project filter" — the
        ``--scope=user`` path always passes ``None`` because the user
        tier is global by design. Mixing ``scope='user'`` with a
        non-None ``project_root`` is a caller bug and is rejected up
        front; user-tier rows have ``project_root IS NULL`` in the
        chunks table so the filter would silently elide every user row.
        """
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
            # ADR-0011 / issue #934 cross-project isolation. ``project_root``
            # in the chunks table is the string returned by ``norm_path``
            # at write time, so we apply the same normalisation here to
            # keep the equality contract byte-exact across platforms (the
            # source-path normalisation pattern above).
            where_parts.append("project_root = ?")
            params.append(norm_path(project_root))
        if source_exact is not None:
            where_parts.append("source_file = ?")
            params.append(norm_path(source_exact))
        elif source_prefix is not None:
            prefix = norm_path(source_prefix)
            # Component-aware prefix: anchor on ``<prefix><sep>`` so a request
            # for ``docs`` does not match ``docsuite``. ``norm_path`` already
            # resolves symlinks and NFC-normalises so the prefix and stored
            # paths share the same canonical form.
            #
            # ``substr(...) = ?`` instead of ``LIKE``: SQLite's built-in LIKE
            # is case-insensitive for ASCII by default, and COLLATE BINARY
            # does not override LIKE — so ``LIKE 'docs/%'`` would also match
            # ``DOCS/foo.md`` on a case-sensitive filesystem and turn an
            # audit ``--source docs`` into a false-positive over an unrelated
            # tree (Codex review on #905 P2-a). ``substr`` equality is a
            # binary string compare, case-sensitive, and avoids the LIKE /
            # GLOB metacharacter escape contract entirely.
            #
            # Separator is platform-native: stored paths come from
            # ``norm_path`` → ``Path.resolve()`` which emits ``\`` on Windows
            # and ``/`` on POSIX. Hardcoding ``/`` would build a prefix like
            # ``C:\repo\docs/`` on Windows and silently match no rows under
            # ``C:\repo\docs\...`` (Codex P2-b). Strip both separator forms
            # from the input so a caller passing a POSIX-style filter on
            # Windows (e.g. ``--source docs/sub``) still anchors correctly.
            anchored = prefix.rstrip("/\\") + os.sep
            where_parts.append("substr(source_file, 1, ?) = ?")
            params.append(len(anchored))
            params.append(anchored)

        where_sql = " AND ".join(where_parts)
        db = self._get_read_db()

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

            rows = db.execute(query, batch_params).fetchall()
            if not rows:
                return

            for row in rows:
                chunk_id, source_file, content, row_scope, project_root = row
                yield ChunkAuditRow(
                    chunk_id=str(chunk_id),
                    source=Path(source_file),
                    content=str(content),
                    scope=str(row_scope),
                    project_root=Path(project_root) if project_root else None,
                )
            last_id = str(rows[-1][0])
            if len(rows) < batch_size:
                return

    async def update_chunks_scope_for_source(
        self,
        old_path: Path,
        new_path: Path,
        new_scope: str,
        new_project_root: Path | None,
    ) -> int:
        """Move indexed chunks to a new source path and scope without changing IDs.

        ADR-0011 PR-D round 10 (B2 partial fix): the SELECT-then-UPDATE
        pair is wrapped in an explicit ``BEGIN IMMEDIATE`` so a
        concurrent writer (e.g. the indexer watcher firing
        ``index_file(new_path)`` between our two statements) cannot
        sneak in INSERTs for ``new_path`` and end up with duplicate
        chunks at the destination. ``BEGIN IMMEDIATE`` acquires a
        ``RESERVED`` lock up front, blocking other writers but not
        readers — Python's default lazy transaction start would only
        promote on the first DML, leaving the SELECT phase exposed.
        """
        db = self._get_db()
        old_norm = norm_path(old_path)
        new_norm = norm_path(new_path)
        project_root = str(new_project_root) if new_project_root else None
        # Take an explicit RESERVED lock before the SELECT so the
        # rowid set we read can't be invalidated by a concurrent
        # watcher INSERT before we UPDATE. ``BEGIN IMMEDIATE`` is a
        # no-op if we're already inside an outer ``transaction()``
        # context (sqlite raises which we swallow); guard via the
        # backend's own ``_in_transaction`` flag.
        opened_tx = False
        if not self._in_transaction:
            db.execute("BEGIN IMMEDIATE")
            opened_tx = True
        try:
            rows = db.execute(
                "SELECT rowid FROM chunks WHERE source_file=?",
                (old_norm,),
            ).fetchall()
            if not rows:
                if opened_tx:
                    db.commit()
                return 0
            rowids = [row[0] for row in rows]
            db.execute(
                "UPDATE chunks SET source_file=?, scope=?, project_root=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE source_file=?",
                (new_norm, new_scope, project_root, old_norm),
            )
            db.execute(
                f"UPDATE chunks_fts SET source_file=? WHERE rowid IN ({placeholders(len(rowids))})",
                [new_norm, *rowids],
            )
            # Move the AI summary cache row alongside the chunks. The
            # cache key is derived from the source path, so an in-place
            # path rewrite would otherwise leave an ``ai_summary:<old>``
            # row attached to chunks that now live at <new> — the new
            # path would render with no AI summary while the orphan row
            # kept contributing to ``count_language_drift`` and
            # ``get_all_ai_summaries``. The summary describes the same
            # *content*, so renaming (rather than dropping) is the
            # correct semantic: a path migration via ``mm context
            # memory migrate`` doesn't change what the file is about,
            # only where it lives.
            #
            # ``INSERT OR REPLACE`` against the new key is necessary
            # because the destination path could already have its own
            # cache row in pathological cases (e.g., the user moved
            # files in opposite directions across two migrations); the
            # source-of-truth for the migrated chunks is the row keyed
            # by ``old`` because that's the one whose signature matched
            # the chunk hashes we just rewrote. After the swap, delete
            # the old key so the orphan can't drift back in.
            old_summary_key = f"{_AI_SUMMARY_KEY_PREFIX}{old_norm}"
            new_summary_key = f"{_AI_SUMMARY_KEY_PREFIX}{new_norm}"
            old_summary = db.execute(
                "SELECT value FROM _memtomem_meta WHERE key=?",
                (old_summary_key,),
            ).fetchone()
            if old_summary is not None:
                db.execute(
                    "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
                    (new_summary_key, old_summary[0]),
                )
                db.execute(
                    "DELETE FROM _memtomem_meta WHERE key=?",
                    (old_summary_key,),
                )
            if opened_tx:
                db.commit()
        except Exception as exc:
            if opened_tx:
                db.rollback()
            raise StorageError(
                f"update_chunks_scope_for_source failed, transaction rolled back: {exc}"
            ) from exc
        return len(rowids)

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
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
    ) -> list[SearchResult]:
        db = self._get_read_db()
        try:
            ns_clause = ""
            ns_params: list = []
            if namespace_filter:
                frag, ns_params = namespace_sql(namespace_filter)
                if frag:
                    ns_clause = f"AND c.{frag}"

            # ADR-0011 §6: scope-context filter is ALWAYS appended even
            # when the caller does not pass an explicit scope_filter,
            # so cross-project leak is impossible by construction.
            scope_frag, scope_params = scope_context_sql(
                scope_filter, project_context_root, column_alias="c."
            )
            scope_clause = f"AND ({scope_frag})"
            tie_break = scope_sort_priority_case("c.")

            # ADR-0011 §6 + PR-D review #2: filter must run *inside* the
            # FTS candidate selection, not after a post-LIMIT join. With
            # the previous shape (``LIMIT k`` on chunks_fts MATCH, then
            # filter), the global top-k could come entirely from another
            # project's chunks; the current project's matches sat just
            # below the cutoff and were dropped. Joining chunks first
            # and applying the namespace/scope predicates inside the
            # same query lets SQLite/FTS5 lazy-iterate matches and stop
            # once ``top_k`` filter-passing rows accumulate.
            #
            # ``c.*`` carries the full chunks-row layout into
            # ``_row_to_chunk`` so all defensive guards (overlap,
            # importance, validity) activate. Score sits at the
            # trailing position after the chunk columns — see
            # ``_chunks_table_column_count`` consumer below.
            sql = f"""SELECT c.*, fts.rank
                   FROM chunks_fts fts
                   JOIN chunks c ON c.rowid = fts.rowid
                   WHERE chunks_fts MATCH ? {ns_clause} {scope_clause}
                   ORDER BY fts.rank, {tie_break}
                   LIMIT ?"""

            # Try AND first (default FTS5 behaviour)
            fts_query = _fts.tokenize_for_fts(query, for_query=True)
            rows = db.execute(sql, [fts_query] + ns_params + scope_params + [top_k]).fetchall()

            # Fall back to OR if AND returns nothing and query has multiple terms
            if not rows and " " in query.strip():
                fts_query_or = _fts.tokenize_for_fts(query, for_query=True, use_or=True)
                rows = db.execute(
                    sql, [fts_query_or] + ns_params + scope_params + [top_k]
                ).fetchall()

        except sqlite3.OperationalError:
            raise

        return [
            SearchResult(
                chunk=self._row_to_chunk(row[:-1]),
                score=abs(row[-1]),
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
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
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

        # ADR-0011 §6: always-on scope-context fragment.
        scope_frag, scope_params = scope_context_sql(
            scope_filter, project_context_root, column_alias="c."
        )
        scope_clause = f"AND ({scope_frag})"
        tie_break = scope_sort_priority_case("c.")

        import sqlite3 as _sqlite3

        # ADR-0011 §6 + PR-D review (round 2): sqlite-vec's
        # ``embedding MATCH ?`` uses the inner ``LIMIT`` as the KNN
        # ``K`` — the namespace / scope filter must run outside that
        # subquery, so the inner K decides how many candidates the
        # outer filter is allowed to see. A *fixed* over-fetch (e.g.
        # ``top_k * 5``) silently drops valid scoped matches when
        # cross-project / cross-namespace skew exceeds that factor.
        #
        # Adaptive over-fetch: try a small K first (fast for the
        # common case where filter passes nearly everything), and if
        # the post-filter result is short of ``top_k`` AND the inner
        # K did not exhaust ``chunks_vec`` (i.e. there could still be
        # filter-passing matches beyond the cutoff), retry with a
        # larger K. Cap retries at the table size so the worst case
        # is "scan every embedding once," which matches the semantics
        # the caller would expect from "find me the nearest scoped
        # row."
        sql = f"""SELECT c.*, sub.distance
               FROM (
                   SELECT rowid, distance
                   FROM chunks_vec
                   WHERE embedding MATCH ?
                   ORDER BY distance
                   LIMIT ?
               ) sub
               JOIN chunks c ON c.rowid = sub.rowid {ns_clause} {scope_clause}
               ORDER BY sub.distance, {tie_break}
               LIMIT ?"""

        # Total embedding rows — the upper bound for a meaningful
        # KNN K. Cheap; sqlite stores chunks_vec row counts in its
        # internal stats and ``COUNT(*)`` is O(table-size) only on
        # the rare cold-cache path.
        total_vec_rows = db.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] or 0

        # Schedule: start at the previous fixed factor for the
        # common case, then jump to "essentially unbounded" before
        # giving up. Stop early when an attempt either returned
        # ``top_k`` rows OR already saw every embedding.
        attempts = [
            max(top_k * 5, 100),
            max(top_k * 50, 1000),
            total_vec_rows,
        ]
        rows: list = []
        for inner_k in attempts:
            inner_k = max(1, min(inner_k, total_vec_rows or inner_k))
            try:
                rows = db.execute(
                    sql,
                    [serialize_f32(embedding), inner_k] + ns_params + scope_params + [top_k],
                ).fetchall()
            except _sqlite3.OperationalError as exc:
                if "Dimension mismatch" in str(exc):
                    raise ValueError(
                        f"Embedding dimension mismatch: query has {len(embedding)}d "
                        f"but DB expects {self._dimension}d. "
                        f"Check MEMTOMEM_EMBEDDING__MODEL / "
                        f"MEMTOMEM_EMBEDDING__DIMENSION."
                    ) from exc
                raise
            # Done if we hit the requested top_k OR if this attempt
            # already scanned every embedding (cannot do better by
            # retrying).
            if len(rows) >= top_k or inner_k >= (total_vec_rows or 0):
                break

        return [
            SearchResult(
                chunk=self._row_to_chunk(row[:-1]),
                score=1.0 / (1.0 + row[-1]),
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

    async def get_chunk_ids_by_hashes(self, content_hashes: Sequence[str]) -> dict[str, UUID]:
        """Return ``{content_hash: chunk_id}`` for hashes present in the DB.

        Used by import to dedup by content across instances (cross-PC merge,
        idempotent re-import). If the same hash appears on multiple rows,
        one of them is returned — the caller must treat hash match as
        "an equivalent chunk exists," not "the unique row."
        """
        if not content_hashes:
            return {}
        db = self._get_read_db()
        unique = list(set(content_hashes))
        rows = db.execute(
            f"SELECT content_hash, id FROM chunks "
            f"WHERE content_hash IN ({placeholders(len(unique))})",
            unique,
        ).fetchall()
        return {row[0]: UUID(row[1]) for row in rows}

    async def get_stats(self) -> dict[str, int]:
        db = self._get_read_db()
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sources = db.execute("SELECT COUNT(DISTINCT source_file) FROM chunks").fetchone()[0]
        return {"total_chunks": total, "total_sources": sources}

    async def get_dense_coverage(self) -> dict[str, int]:
        """Return dense-vector coverage: ``{"total": N, "with_dense": M}``.

        ``M < N`` when chunks were indexed before the embedder finished
        loading (NoopEmbedder dimension==0 path, or an init-time failure
        that fell through to BM25-only). ``M == 0`` also when
        ``chunks_vec`` is absent — typical right after
        ``mm embedding-reset --mode purge`` or before the first indexing
        run creates the virtual table.

        ``with_dense`` joins ``chunks`` ⋈ ``chunks_vec`` on rowid so the
        count tracks **retrievable** chunks only. A raw
        ``COUNT(*) FROM chunks_vec`` would over-report when an
        interrupted upsert or concurrent writer leaves stale vec
        sidecars behind (orphan_gc.py already treats this state as
        possible) — the rollup would then show ``with_dense == total``
        even with some current chunks missing a vector, hiding the
        BM25-only condition this telemetry exists to surface.

        Surface used by ``/api/embedding-status`` and ``mem_status`` so
        operators can see at a glance whether dense retrieval is going
        to find anything before they wonder why semantic search is
        returning only BM25-flavored results.
        """
        db = self._get_read_db()
        total = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        with_dense = 0
        if self._has_vec_table:
            with_dense = db.execute(
                "SELECT COUNT(*) FROM chunks c INNER JOIN chunks_vec v ON v.rowid = c.rowid"
            ).fetchone()[0]
        return {"total": total, "with_dense": with_dense}

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

    async def count_chunks_by_source(self, source_file: Path) -> int:
        db = self._get_read_db()
        row = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_file=?",
            (norm_path(source_file),),
        ).fetchone()
        return int(row[0]) if row else 0

    async def count_chunk_links_for_source(self, source_file: Path) -> int:
        # ADR-0011 #886: `mm context memory-migrate` reports the size of the
        # chunk_links "neighborhood" attached to a moving source. For v1
        # single-DB chunk-id-stable rename the entire neighborhood is
        # preserved (chunks.id never changes, FK rows are untouched), so
        # the displayed "N preserved, 0 dropped" line is computed from this
        # value rather than hard-coded. Cross-DB migration (deferred) is
        # where the dropped half would start to matter.
        db = self._get_read_db()
        norm = norm_path(source_file)
        row = db.execute(
            "SELECT COUNT(*) FROM chunk_links "
            "WHERE source_id IN (SELECT id FROM chunks WHERE source_file=?) "
            "   OR target_id IN (SELECT id FROM chunks WHERE source_file=?)",
            (norm, norm),
        ).fetchone()
        return int(row[0]) if row else 0

    async def list_chunks_by_tag(self, tag: str, limit: int = 10) -> list[Chunk]:
        # Dry-run sample for the global tag-management ops (rename / delete /
        # merge). Those ops mutate EVERY chunk carrying the tag regardless of
        # scope tier, and ``count_chunks_by_tag`` counts globally to match, so
        # the sample must draw from the same global row set. Routing through
        # ``recall_chunks`` looked tidy (#750) but it always appends the
        # ADR-0011 §6 scope-context fragment, which silently narrowed samples
        # to ``scope='user'`` when no project context was pinned — a
        # project-only tag then previewed as "N affected, 0 samples" while the
        # apply still wiped N rows. Mirror ``count_chunks_by_tag``'s
        # ``EXISTS(json_each)`` membership here so sample / count / apply agree
        # on one row set (#688). ``id`` breaks created_at ties deterministically.
        db = self._get_read_db()
        rows = db.execute(
            "SELECT * FROM chunks WHERE EXISTS "
            "(SELECT 1 FROM json_each(chunks.tags) WHERE value = ?) "
            "ORDER BY created_at DESC, id LIMIT ?",
            (tag, limit),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    async def count_chunks_by_tag(self, tag: str) -> int:
        db = self._get_read_db()
        row = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE EXISTS "
            "(SELECT 1 FROM json_each(chunks.tags) WHERE value = ?)",
            (tag,),
        ).fetchone()
        return int(row[0]) if row else 0

    async def count_chunks_by_any_tag(self, tags: Sequence[str]) -> int:
        # Single-query union count for the merge dry-run path. Counting per
        # tag and Python-side deduping would either cap at the per-tag scan
        # limit (under-reports) or fetch every row (slow); the EXISTS+IN
        # form lets SQLite de-dup once per chunk regardless of how many
        # source tags overlap on the same row.
        if not tags:
            return 0
        placeholders = ",".join("?" for _ in tags)
        db = self._get_read_db()
        row = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE EXISTS "
            f"(SELECT 1 FROM json_each(chunks.tags) WHERE value IN ({placeholders}))",
            tuple(tags),
        ).fetchone()
        return int(row[0]) if row else 0

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
        tag_filter: str | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
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
        if tag_filter is not None:
            # Comma-separated tags = OR matching, mirroring the
            # post-fusion semantics in ``SearchPipeline.search`` so the
            # tag-only path (#750) ranks the same set the keyword path
            # would have filtered down to.
            tags = [t.strip() for t in tag_filter.split(",") if t.strip()]
            if tags:
                placeholders = ",".join("?" for _ in tags)
                conditions.append(
                    f"EXISTS (SELECT 1 FROM json_each(chunks.tags) "
                    f"WHERE json_each.value IN ({placeholders}))"
                )
                params.extend(tags)

        # ADR-0011 §6: always-on scope-context fragment.
        scope_frag, scope_params = scope_context_sql(scope_filter, project_context_root)
        conditions.append(scope_frag)
        params.extend(scope_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = db.execute(
            f"SELECT * FROM chunks {where} "
            f"ORDER BY created_at DESC, {scope_sort_priority_case()} LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    async def get_all_source_files(self) -> set[Path]:
        db = self._get_db()
        rows = db.execute("SELECT DISTINCT source_file FROM chunks").fetchall()
        return {Path(row[0]) for row in rows}

    async def search_source_files_by_content(self, query: str, limit: int = 10000) -> list[Path]:
        term = query.strip()
        if not term:
            return []
        db = self._get_read_db()
        escaped_term = f"%{escape_like(term)}%"
        escaped_json_term = f"%{escape_like(json.dumps(term, ensure_ascii=True)[1:-1])}%"
        rows = db.execute(
            "SELECT source_file FROM chunks "
            "WHERE content LIKE ? ESCAPE '\\' "
            "   OR heading_hierarchy LIKE ? ESCAPE '\\' "
            "   OR heading_hierarchy LIKE ? ESCAPE '\\' "
            "GROUP BY source_file "
            "ORDER BY MAX(updated_at) DESC, source_file "
            "LIMIT ?",
            (escaped_term, escaped_term, escaped_json_term, limit),
        ).fetchall()
        return [Path(row[0]) for row in rows]

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

    async def get_source_summaries(self) -> dict[str, tuple[list[str], str]]:
        """Return ``{source_file_path_str: (heading_hierarchy, first_chunk_content)}``.

        The "first chunk" is the section with the smallest ``start_line`` per
        source. Powers the Source tab's heuristic preview (first heading +
        first paragraph) — drives the fallback shown when no AI summary has
        been generated yet, or when the LLM is disabled. Pure read-side
        aggregation; no LLM, no extra storage column.
        """
        db = self._get_read_db()
        rows = db.execute(
            "SELECT source_file, content, heading_hierarchy FROM ("
            "  SELECT source_file, content, heading_hierarchy,"
            "         ROW_NUMBER() OVER ("
            "           PARTITION BY source_file ORDER BY start_line, rowid"
            "         ) AS rn"
            "  FROM chunks"
            ") WHERE rn = 1"
        ).fetchall()
        result: dict[str, tuple[list[str], str]] = {}
        for source, content, hh_json in rows:
            try:
                hh = list(json.loads(hh_json)) if hh_json else []
            except (json.JSONDecodeError, TypeError):
                hh = []
            result[source] = (hh, content or "")
        return result

    # ---- AI summary cache (per-source LLM-generated preview) ----------------

    async def get_ai_summary(self, source_file: Path) -> dict | None:
        """Return the cached AI summary record for ``source_file``, or None.

        Record shape: ``{"summary": str, "signature": str, "language": str,
        "generated_at": str}``. Returns None when no row exists or the JSON
        is corrupt — callers treat both as "no cache, regenerate".
        """
        assert self._meta is not None
        raw = self._meta.get_meta(_ai_summary_key(source_file))
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupt ai_summary record for %s", source_file)
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    async def set_ai_summary(
        self,
        source_file: Path,
        summary: str,
        signature: str,
        language: str,
    ) -> None:
        """Persist an AI summary record. Overwrites any prior value."""
        from datetime import datetime, timezone

        assert self._meta is not None
        record = {
            "summary": summary,
            "signature": signature,
            "language": language,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._meta.set_meta(_ai_summary_key(source_file), json.dumps(record))

    async def delete_ai_summary(self, source_file: Path) -> None:
        """Drop the AI summary cache row for ``source_file``, if any.

        Called from the indexing pipeline when a refresh determines the
        cache is stale — e.g., a reindex produced zero chunks (source
        emptied / became unchunkable), or the LLM failed on a content-
        drifted source. Idempotent: deleting a missing row is a no-op.
        Standalone from ``delete_by_source`` so the summarizer can clear
        the prose without also tearing down the chunk rows.
        """
        db = self._get_db()
        db.execute(
            "DELETE FROM _memtomem_meta WHERE key=?",
            (_ai_summary_key(source_file),),
        )
        if not self._in_transaction:
            db.commit()

    async def get_all_ai_summaries(self) -> dict[str, dict]:
        """Return ``{normalised_path: record}`` for every cached AI summary.

        Prefix-scans ``_memtomem_meta`` for keys starting with
        ``ai_summary:`` so unrelated meta rows (embedding dimension etc.)
        don't leak in. Records with corrupt JSON are silently dropped — the
        Source-tab API treats them as "no preview".
        """
        db = self._get_read_db()
        rows = db.execute(
            "SELECT key, value FROM _memtomem_meta WHERE key LIKE ?",
            (f"{_AI_SUMMARY_KEY_PREFIX}%",),
        ).fetchall()
        result: dict[str, dict] = {}
        for key, value in rows:
            path = key[len(_AI_SUMMARY_KEY_PREFIX) :]
            try:
                obj = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict):
                result[path] = obj
        return result

    async def count_language_drift(self, target_language: str) -> int:
        """Count cached summaries whose ``language`` is not ``target_language``.

        Drives the Source-tab "N summaries are in <X> (setting: <Y>)" banner.
        Records missing a ``language`` field count as drift — treated as
        legacy entries that need an explicit regeneration to resolve.
        """
        all_summaries = await self.get_all_ai_summaries()
        return sum(1 for rec in all_summaries.values() if rec.get("language") != target_language)

    async def list_language_drift_paths(self, target_language: str) -> list[Path]:
        """Return paths whose cached summary language ≠ ``target_language``.

        Bulk-regenerate endpoint consumes this to avoid touching entries
        that already match the requested language.
        """
        all_summaries = await self.get_all_ai_summaries()
        return [
            Path(p) for p, rec in all_summaries.items() if rec.get("language") != target_language
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

        # Validity-window columns (may not exist in older DBs) — columns 19,20
        # after importance_score (18). NULL → unbounded on that side.
        vfrom: int | None = None
        vto: int | None = None
        if len(row) >= 21:
            vfrom = row[19]
            vto = row[20]

        # Scope axis columns (may not exist in older DBs) — columns 21,22
        # after validity. ADR-0011: ``user`` is the default for legacy rows;
        # ``project_root`` is NULL for user scope, an absolute path for
        # project tiers.
        scope_val: str = "user"
        project_root_val: Path | None = None
        if len(row) >= 22:
            scope_val = row[21] or "user"
        if len(row) >= 23:
            raw_pr = row[22]
            if raw_pr:
                project_root_val = Path(raw_pr)

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
            valid_from_unix=vfrom,
            valid_to_unix=vto,
            scope=scope_val,
            project_root=project_root_val,
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
