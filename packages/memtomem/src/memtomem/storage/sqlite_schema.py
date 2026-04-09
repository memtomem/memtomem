"""SQLite table creation and schema migration logic."""

from __future__ import annotations

import logging
import re
import sqlite3

from memtomem.storage.sqlite_meta import MetaManager

logger = logging.getLogger(__name__)


def create_tables(
    db: sqlite3.Connection,
    meta: MetaManager,
    dimension: int,
    embedding_provider: str,
    embedding_model: str,
) -> tuple[int, tuple[int, int] | None, tuple[str, str, str, str] | None]:
    """Create all required tables and return effective (dimension, dim_mismatch, model_mismatch).

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
    except sqlite3.OperationalError:
        pass  # column already exists

    # Idempotent migration: personalization columns
    for col_sql in (
        "ALTER TABLE chunks ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chunks ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chunks ADD COLUMN last_accessed_at TEXT",
    ):
        try:
            db.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Idempotent migration: overlap columns for chunk_overlap_tokens
    for col_sql in (
        "ALTER TABLE chunks ADD COLUMN overlap_before INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE chunks ADD COLUMN overlap_after INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            db.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Idempotent migration: importance_score column
    try:
        db.execute("ALTER TABLE chunks ADD COLUMN importance_score REAL NOT NULL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass  # column already exists

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
