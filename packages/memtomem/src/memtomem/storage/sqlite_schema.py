"""SQLite table creation and schema migration logic."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from memtomem.errors import EmbeddingDimensionMismatchError
from memtomem.storage.sqlite_meta import MetaManager

logger = logging.getLogger(__name__)

# ``chunk_links.link_type`` values recognised by the back-fill and (in PR-2)
# the writer. Validation lives in Python so adding a new type is one PR not
# two — see ``planning/mem-agent-share-chunk-links-rfc.md`` §Storage.
_VALID_LINK_TYPES: frozenset[str] = frozenset({"shared"})

# Bumping this key (e.g. ``..._v2``) triggers a re-run of the back-fill on
# the next startup — used if a future release tightens what counts as a
# share-tag (e.g. namespace prefix on the source UUID).
_CHUNK_LINKS_BACKFILL_KEY = "chunk_links_backfill_v1"

_SHARED_FROM_TAG_PREFIX = "shared-from="


def create_tables(
    db: sqlite3.Connection,
    meta: MetaManager,
    dimension: int,
    embedding_provider: str,
    embedding_model: str,
    *,
    strict_dim_check: bool = True,
) -> tuple[int, tuple[int, int] | None, tuple[str, str, str, str] | None]:
    """Create all required tables and return effective (dimension, dim_mismatch, model_mismatch).

    When ``strict_dim_check`` is True (default), a contradictory state —
    effective ``dimension == 0`` with a non-``none`` configured provider —
    raises :class:`EmbeddingDimensionMismatchError`. Recovery tooling
    (``mm embedding-reset``) passes ``strict_dim_check=False`` so it can
    observe the broken state and reset it.

    Returns:
        A 3-tuple of ``(effective_dimension, dim_mismatch_or_None, model_mismatch_or_None)``.
    """
    dim_mismatch: tuple[int, int] | None = None
    model_mismatch: tuple[str, str, str, str] | None = None

    # Meta table for persisting configuration (e.g. embedding dimension)
    db.execute("""
        CREATE TABLE IF NOT EXISTS _memtomem_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_file TEXT NOT NULL,
            heading_hierarchy TEXT NOT NULL DEFAULT '[]',
            chunk_type TEXT NOT NULL DEFAULT 'raw_text',
            start_line INTEGER NOT NULL DEFAULT 0,
            end_line INTEGER NOT NULL DEFAULT 0,
            language TEXT NOT NULL DEFAULT 'en',
            tags TEXT NOT NULL DEFAULT '[]',
            namespace TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Idempotent migration: add namespace column to existing DBs
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN namespace TEXT NOT NULL DEFAULT 'default'")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    # Idempotent migration: personalization columns
    for col_sql in (
        "ALTER TABLE chunks ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chunks ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chunks ADD COLUMN last_accessed_at TEXT",
    ):
        try:
            db.execute(col_sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Idempotent migration: overlap columns for chunk_overlap_tokens
    for col_sql in (
        "ALTER TABLE chunks ADD COLUMN overlap_before INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chunks ADD COLUMN overlap_after INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            db.execute(col_sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Idempotent migration: importance_score column
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN importance_score REAL NOT NULL DEFAULT 0.0")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise

    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
        USING fts5(content, source_file, tokenize='unicode61')
    """)

    # Determine effective dimension: stored meta > config
    stored_dim = meta.get_stored_dimension()
    vec_exists = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        is not None
    )

    if stored_dim is not None:
        # DB already has a recorded dimension — honour it to preserve data
        if stored_dim != dimension:
            logger.warning(
                "Stored embedding dimension %d differs from configured %d — "
                "using stored dimension to preserve indexed data. "
                "Run 'mm embedding-reset' (CLI) or mem_embedding_reset (MCP) to change.",
                stored_dim,
                dimension,
            )
            dim_mismatch = (stored_dim, dimension)
        dimension = stored_dim
    elif vec_exists:
        # Legacy DB: vec table exists but no meta row yet.
        existing_vec_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        m = re.search(r"float\[(\d+)\]", (existing_vec_sql[0] or "") if existing_vec_sql else "")
        if m:
            legacy_dim = int(m.group(1))
            if legacy_dim != dimension:
                logger.warning(
                    "Legacy DB: chunks_vec dimension %d differs from configured %d — "
                    "using stored dimension to preserve indexed data.",
                    legacy_dim,
                    dimension,
                )
            dimension = legacy_dim
            meta.store_dimension(legacy_dim)
    else:
        # Fresh DB — store the configured dimension
        meta.store_dimension(dimension)

    # ---- embedding provider/model validation ----------------------------
    stored_provider = meta.get_meta("embedding_provider")
    stored_model = meta.get_meta("embedding_model")

    if stored_provider is not None and stored_model is not None:
        # DB has recorded provider/model — check against config
        if embedding_provider and embedding_model:
            if stored_provider != embedding_provider or stored_model != embedding_model:
                logger.warning(
                    "Stored embedding model %s/%s differs from configured %s/%s. "
                    "Search quality may be degraded. "
                    "Run 'mm embedding-reset' (CLI) or mem_embedding_reset (MCP) to resolve.",
                    stored_provider,
                    stored_model,
                    embedding_provider,
                    embedding_model,
                )
                model_mismatch = (
                    stored_provider,
                    stored_model,
                    embedding_provider,
                    embedding_model,
                )
    else:
        # New or legacy DB — backfill provider/model from current config
        if embedding_provider:
            meta.set_meta("embedding_provider", embedding_provider)
        if embedding_model:
            meta.set_meta("embedding_model", embedding_model)

    # ---- dim=0 / real-provider mismatch -- fail fast at startup ---------
    # Catches the legacy NoopEmbedder → real-provider switch: stored
    # dimension is 0 (so ``chunks_vec`` was never created) but the runtime
    # embedder is configured to produce real vectors. Without this gate,
    # startup succeeds and every subsequent ``upsert_chunks`` crashes with
    # ``no such table: chunks_vec``. See issue #298.
    if dimension == 0 and (embedding_provider or "").lower() not in ("", "none"):
        if strict_dim_check:
            raise EmbeddingDimensionMismatchError(
                f"DB embedding_dimension=0 but configured provider is "
                f"'{embedding_provider}'. This usually means the DB was "
                f"initialized with provider=none (NoopEmbedder) and the "
                f"config was later switched to a real provider without "
                f"resetting. Run 'mm embedding-reset --mode apply-current' "
                f"(CLI) or mem_embedding_reset (MCP) to recreate chunks_vec "
                f"with the configured dimension."
            )
        logger.warning(
            "DB embedding_dimension=0 but configured provider is '%s' — "
            "continuing in recovery mode (strict_dim_check=False). "
            "Run 'mm embedding-reset --mode apply-current' to fix.",
            embedding_provider,
        )

    if dimension > 0:
        db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
            USING vec0(embedding float[{dimension}])
        """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_source
        ON chunks(source_file)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_hash
        ON chunks(content_hash)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_namespace
        ON chunks(namespace)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_created_at
        ON chunks(created_at)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_access_count
        ON chunks(access_count)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_importance
        ON chunks(importance_score)
    """)

    # --- Personalization tables ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id TEXT NOT NULL,
            action TEXT NOT NULL,
            query_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_access_log_chunk ON access_log(chunk_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_access_log_created ON access_log(created_at)")

    db.execute("""
        CREATE TABLE IF NOT EXISTS query_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            query_embedding BLOB NOT NULL,
            result_chunk_ids TEXT NOT NULL,
            result_scores TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_query_history_created ON query_history(created_at)")

    db.execute("""
        CREATE TABLE IF NOT EXISTS namespace_metadata (
            namespace TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # --- Session / Episodic memory tables ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL DEFAULT 'default',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            summary TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            namespace TEXT NOT NULL DEFAULT 'default'
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at)")

    db.execute("""
        CREATE TABLE IF NOT EXISTS session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            content TEXT NOT NULL,
            chunk_ids TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id)"
    )

    # Idempotent migration: metadata column for session_events
    try:
        db.execute("ALTER TABLE session_events ADD COLUMN metadata TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # column already exists

    # --- Working memory ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS working_memory (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            promoted BOOLEAN DEFAULT 0
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_working_session_created "
        "ON working_memory(session_id, created_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_working_expires "
        "ON working_memory(expires_at) WHERE expires_at IS NOT NULL"
    )

    db.execute("""
        CREATE TABLE IF NOT EXISTS chunk_relations (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'related',
            created_at TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id),
            FOREIGN KEY (source_id) REFERENCES chunks(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES chunks(id) ON DELETE CASCADE
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_relations_source ON chunk_relations(source_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_relations_target ON chunk_relations(target_id)")

    # --- Cross-namespace share lineage ---
    # Storage-layer FK + cascade replacement for the ``shared-from=<uuid>``
    # audit tag previously written by ``mem_agent_share``. The tag survived
    # in markdown, but tag-only provenance does not benefit from an index
    # and breaks on UUID churn (reindex re-issues chunk ids). See
    # ``planning/mem-agent-share-chunk-links-rfc.md``.
    #
    # ``ON DELETE SET NULL`` on ``source_id`` keeps the destination chunk
    # alive when the source is deleted (matches existing copy-on-share
    # durability). ``ON DELETE CASCADE`` on ``target_id`` drops the row
    # when the destination chunk goes away.
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunk_links (
            source_id TEXT,
            target_id TEXT NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'shared',
            namespace_target TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (target_id, link_type),
            FOREIGN KEY (source_id) REFERENCES chunks(id) ON DELETE SET NULL,
            FOREIGN KEY (target_id) REFERENCES chunks(id) ON DELETE CASCADE
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_links_source ON chunk_links(source_id, link_type)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_links_namespace ON chunk_links(namespace_target)"
    )

    _backfill_chunk_links(db, meta)

    # --- Entity extraction table ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunk_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1.0,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_entities_chunk ON chunk_entities(chunk_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON chunk_entities(entity_type)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_entities_type_value ON chunk_entities(entity_type, entity_value)"
    )

    # --- Memory policies table ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_policies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            policy_type TEXT NOT NULL,
            config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            namespace_filter TEXT,
            last_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # --- Health watchdog snapshots ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS health_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL,
            check_name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',
            created_at REAL NOT NULL
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_health_snap_name "
        "ON health_snapshots(check_name, created_at)"
    )

    db.commit()

    return dimension, dim_mismatch, model_mismatch


def _backfill_chunk_links(db: sqlite3.Connection, meta: MetaManager) -> int:
    """Populate ``chunk_links`` from pre-RFC ``shared-from=<uuid>`` tags.

    ``mem_agent_share`` historically encoded provenance as a
    ``shared-from=<source-uuid>`` audit tag on the destination chunk's
    ``tags`` array. This one-shot pass walks those rows once per database
    and inserts the equivalent ``chunk_links`` row so structured
    provenance (FK + cascade + index) is available without waiting for
    a re-share. Idempotent: completion is recorded in ``_memtomem_meta``
    and re-runs are no-ops.

    Sources whose UUID no longer resolves (already deleted) are stored
    with ``source_id=NULL`` — same end-state as a post-RFC share whose
    source was later deleted (``ON DELETE SET NULL``).

    Returns the number of rows inserted on this call (0 once recorded).
    """
    if meta.get_meta(_CHUNK_LINKS_BACKFILL_KEY) == "done":
        return 0

    rows = db.execute(
        "SELECT id, namespace, tags FROM chunks WHERE tags LIKE ?",
        (f"%{_SHARED_FROM_TAG_PREFIX}%",),
    ).fetchall()

    inserted = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for target_id, namespace, tags_json in rows:
        try:
            tags = json.loads(tags_json) if tags_json else []
        except (ValueError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        source_uuid: str | None = None
        for tag in tags:
            if not isinstance(tag, str) or not tag.startswith(_SHARED_FROM_TAG_PREFIX):
                continue
            value = tag[len(_SHARED_FROM_TAG_PREFIX) :].strip()
            if value:
                source_uuid = value
                break
        if source_uuid is None:
            continue

        src_exists = db.execute("SELECT 1 FROM chunks WHERE id = ?", (source_uuid,)).fetchone()
        source_id_to_store = source_uuid if src_exists else None

        cursor = db.execute(
            "INSERT OR IGNORE INTO chunk_links "
            "(source_id, target_id, link_type, namespace_target, created_at) "
            "VALUES (?, ?, 'shared', ?, ?)",
            (source_id_to_store, target_id, namespace, now),
        )
        if cursor.rowcount > 0:
            inserted += 1

    meta.set_meta(_CHUNK_LINKS_BACKFILL_KEY, "done")
    return inserted
