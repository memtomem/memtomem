"""SQLite table creation and schema migration logic."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone

from memtomem.errors import EmbeddingDimensionMismatchError, SchemaDowngradeError
from memtomem.storage.sqlite_meta import MetaManager

logger = logging.getLogger(__name__)

# ``chunk_links.link_type`` values recognised by the back-fill and (in PR-2)
# the writer. Validation lives in Python so adding a new type is one PR not
# two — see ``planning/mem-agent-share-chunk-links-rfc.md`` §Storage.
_VALID_LINK_TYPES: frozenset[str] = frozenset({"shared", "summarizes"})

# Bumping this key (e.g. ``..._v2``) triggers a re-run of the back-fill on
# the next startup — used if a future release tightens what counts as a
# share-tag (e.g. namespace prefix on the source UUID).
_CHUNK_LINKS_BACKFILL_KEY = "chunk_links_backfill_v1"

_SHARED_FROM_TAG_PREFIX = "shared-from="

# One-shot repair key for tags that were stored as their literal ``\uXXXX``
# escape text instead of the characters they encode (pre ``ensure_ascii=False``
# memory_writer fix). Bump (``..._v2``) to force a re-run.
_TAGS_UNICODE_REPAIR_KEY = "tags_unicode_repair_v1"

# Matches a single ``\uXXXX`` BMP escape sequence as literal text (a backslash,
# a ``u``, then four hex digits) — what a mis-decoded tag still carries.
_UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")

# Monotonic schema generation for the downgrade fence (#1614). Bump by 1
# whenever create_tables gains a migration an older binary must not run
# under. Same-or-older stored versions always pass (migrations stay
# additive + idempotent); only stored > SCHEMA_VERSION is fatal.
SCHEMA_VERSION = 1

_SCHEMA_VERSION_KEY = "schema_version"


def check_schema_downgrade(db: sqlite3.Connection) -> None:
    """Raise :class:`SchemaDowngradeError` if the DB records a newer schema version.

    Read-only — safe to call before any mutating setup (journal-mode PRAGMAs,
    table creation), so a refused open leaves the DB file untouched. A missing
    meta table or missing key means a fresh or pre-versioning DB and passes.
    A non-integer value cannot be a legitimate newer version (all binaries
    write integers) — it is evidence of corruption or hand-editing, so warn
    loudly and pass; the stamp at the end of ``create_tables`` overwrites it
    with the truth.
    """
    try:
        row = db.execute(
            "SELECT value FROM _memtomem_meta WHERE key = ?", (_SCHEMA_VERSION_KEY,)
        ).fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return
        raise
    if row is None:
        return
    try:
        stored_ver = int(row[0])
    except ValueError:
        logger.warning(
            "Non-integer schema_version %r in _memtomem_meta — treating as "
            "pre-versioning and restamping to %d.",
            row[0],
            SCHEMA_VERSION,
        )
        return
    if stored_ver > SCHEMA_VERSION:
        raise SchemaDowngradeError(
            f"This database has schema version {stored_ver}, but this "
            f"memtomem binary only supports up to {SCHEMA_VERSION}. "
            f"The database was created or migrated by a newer memtomem "
            f"release. Upgrade memtomem to open it: "
            f"'uv tool upgrade memtomem' or 'pip install -U memtomem'."
        )


def create_tables(
    db: sqlite3.Connection,
    meta: MetaManager,
    dimension: int,
    embedding_provider: str,
    embedding_model: str,
    *,
    embedding_policy_fingerprint: str = "",
    embedding_max_sequence_tokens: int | None = None,
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

    # ---- schema-version downgrade fence (#1614) ----
    # Must run before any migration touches user data. SqliteBackend also
    # runs this check earlier, before its journal-mode PRAGMAs, so a refused
    # open never writes; this second call covers direct callers.
    check_schema_downgrade(db)

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
            updated_at TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TEXT,
            overlap_before INTEGER NOT NULL DEFAULT 0,
            overlap_after INTEGER NOT NULL DEFAULT 0,
            importance_score REAL NOT NULL DEFAULT 0.0,
            valid_from_unix INTEGER,
            valid_to_unix INTEGER,
            scope TEXT NOT NULL DEFAULT 'user',
            project_root TEXT
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

    # Idempotent migration: temporal-validity window columns (RFC: temporal-validity).
    # NULL on either side means "no bound on this side"; both NULL means
    # always-valid (RFC §Goal 4 — chunks without frontmatter validity fields
    # stay backward-compatible).
    for col_sql in (
        "ALTER TABLE chunks ADD COLUMN valid_from_unix INTEGER",
        "ALTER TABLE chunks ADD COLUMN valid_to_unix INTEGER",
    ):
        try:
            db.execute(col_sql)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Idempotent migration: scope hierarchy columns (ADR-0011).
    # ``scope`` is one of "user" / "project_shared" / "project_local"; all
    # existing rows default to "user" so search behavior is unchanged for
    # users who do not opt into project tiers. ``project_root`` is the
    # absolute path of the canonical project root for project-scoped chunks
    # (NULL for user scope) — required so one user-local DB can hold chunks
    # from multiple worktrees without path-prefix collisions.
    for col_sql in (
        "ALTER TABLE chunks ADD COLUMN scope TEXT NOT NULL DEFAULT 'user'",
        "ALTER TABLE chunks ADD COLUMN project_root TEXT",
    ):
        try:
            db.execute(col_sql)
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

    # ---- embedding policy validation -----------------------------------
    # Pre-policy ONNX databases contain vectors generated at the model's
    # native limit. Record that legacy policy as cap=0 instead of silently
    # claiming the newly configured safety cap. Fresh DBs and non-ONNX
    # legacy DBs can safely adopt the current policy metadata.
    if embedding_policy_fingerprint:
        stored_policy = meta.get_meta("embedding_policy_fingerprint")
        if stored_policy is None:
            if vec_exists and (embedding_provider or "").lower() == "onnx":
                meta.set_meta("embedding_policy_fingerprint", "onnx:v1:max_sequence_tokens=0")
                meta.set_meta("embedding_max_sequence_tokens", "0")
            else:
                meta.set_meta("embedding_policy_fingerprint", embedding_policy_fingerprint)
                if embedding_max_sequence_tokens is not None:
                    meta.set_meta(
                        "embedding_max_sequence_tokens", str(embedding_max_sequence_tokens)
                    )

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

    # Idempotent migration (#691): collapse duplicate-hash rows and add a
    # UNIQUE constraint so multi-process indexing (mm web watcher + mm CLI /
    # MCP) cannot insert ghost rows that share
    # ``(namespace, source_file, content_hash, start_line)``. The cleanup
    # only runs once — its presence/absence is gated by
    # ``idx_chunks_unique_content`` so subsequent startups are a no-op.
    has_vec_table = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        is not None
    )
    _migrate_chunks_uniqueness(db, has_vec_table=has_vec_table)

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
    # Composite index for (scope, project_root) lookups (ADR-0011 §6 always-on
    # scope-context filter). Default search pins ``project_root = <current>``
    # for project context; cheap composite scan beats full table scan once
    # any user opts into project_memory_dirs.
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_scope
        ON chunks(scope, project_root)
    """)
    # ADR-0011 PR-D review round 7: partial sibling index on
    # ``project_root`` alone. The dominant in-project filter shape is
    # ``(scope='user' OR project_root=?)`` — the OR's second leg cannot
    # use ``idx_chunks_scope`` because ``project_root`` is the trailing
    # column behind ``scope``. Without this partial index the second
    # leg degrades to a full scan once a user accumulates project-tier
    # chunks. Partial (``WHERE project_root IS NOT NULL``) keeps the
    # index small for the user-tier majority case.
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_project_root
        ON chunks(project_root)
        WHERE project_root IS NOT NULL
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
            run_id TEXT,
            observation_json TEXT NOT NULL DEFAULT '{}',
            result_snapshot_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)
    # Additive Quality Lab observation fields. Older 0.3.x binaries safely
    # ignore these columns, so this does not require a schema-generation bump.
    existing_history_columns = {
        row[1] for row in db.execute("PRAGMA table_info(query_history)").fetchall()
    }
    observation_columns = {
        "run_id": "ALTER TABLE query_history ADD COLUMN run_id TEXT",
        "observation_json": (
            "ALTER TABLE query_history ADD COLUMN observation_json TEXT NOT NULL DEFAULT '{}'"
        ),
        "result_snapshot_json": (
            "ALTER TABLE query_history ADD COLUMN result_snapshot_json TEXT NOT NULL DEFAULT '[]'"
        ),
    }
    for column_name, col_sql in observation_columns.items():
        if column_name not in existing_history_columns:
            db.execute(col_sql)
    db.execute("CREATE INDEX IF NOT EXISTS idx_query_history_created ON query_history(created_at)")
    # The run_id unique index started life partial (WHERE run_id IS NOT NULL,
    # #1800). search_feedback references query_history(run_id), and SQLite
    # only accepts a non-partial unique index as an FK parent key, so rebuild
    # the partial variant in place. NULL run_ids (legacy rows) stay valid: a
    # non-partial unique index treats NULLs as distinct.
    run_id_index_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_query_history_run_id'"
    ).fetchone()
    if run_id_index_sql and run_id_index_sql[0] and "WHERE" in run_id_index_sql[0].upper():
        db.execute("DROP INDEX idx_query_history_run_id")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_query_history_run_id ON query_history(run_id)"
    )

    # Explicit relevance judgments attached to one observed run (#1801).
    # Rows hold only IDs, the judgment, and audit timestamps — never result
    # content, paths, or query text (observation privacy boundary). The
    # cascade keeps history pruning/reset from orphaning feedback.
    db.execute("""
        CREATE TABLE IF NOT EXISTS search_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES query_history(run_id) ON DELETE CASCADE,
            chunk_id TEXT NOT NULL,
            judgment TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_search_feedback_run_chunk "
        "ON search_feedback(run_id, chunk_id)"
    )

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

    # --- Idempotency ledger (issue #1573) ---
    # (tool, key) -> the result string of a keyed memory write. A replayed call
    # with a seen key returns the stored result and performs no write.
    # ``result`` is NULL for a *pending* claim (write in flight) and filled in
    # on completion — the pending row is the pre-write test-and-set that stops
    # two concurrent same-key calls (even to different files) from both writing.
    # Rows expire after IdempotencyMixin.IDEMPOTENCY_TTL_S; purge is lazy
    # (idempotency_claim deletes expired rows), mirroring working_memory.
    db.execute("""
        CREATE TABLE IF NOT EXISTS idempotency_ledger (
            tool TEXT NOT NULL,
            key TEXT NOT NULL,
            result TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (tool, key)
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_ledger(expires_at)"
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
    _repair_unicode_escaped_tags(db, meta)

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

    # --- Review-first automatic memory formation ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_candidates (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            operation TEXT NOT NULL,
            destination TEXT NOT NULL,
            content TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '[]',
            matched_existing_ids TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL,
            sensitivity TEXT NOT NULL DEFAULT 'normal',
            proposed_diff TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            extractor_version TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            reviewer TEXT,
            decision_reason TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            claim_started_at TEXT,
            UNIQUE(session_id, fingerprint)
        )
    """)
    try:
        db.execute("ALTER TABLE memory_candidates ADD COLUMN claim_started_at TEXT")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise
    # Existing ``writing`` rows predate claim timestamps. Stamp them at
    # upgrade time rather than treating them as immediately stale: a still
    # running old process gets the full recovery grace period, while a truly
    # stranded row becomes recoverable after the documented threshold. This
    # is idempotent because only NULL timestamps are touched.
    db.execute(
        "UPDATE memory_candidates SET claim_started_at=? "
        "WHERE status='writing' AND claim_started_at IS NULL",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_candidates_status "
        "ON memory_candidates(status, created_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_candidates_stale_claim "
        "ON memory_candidates(status, claim_started_at)"
    )
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_candidate_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL REFERENCES memory_candidates(id) ON DELETE CASCADE,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_candidate_transitions_candidate "
        "ON memory_candidate_transitions(candidate_id, created_at)"
    )

    # --- Provenance-bearing temporal assertions (derived, rebuildable index) ---
    db.execute("""
        CREATE TABLE IF NOT EXISTS canonical_entities (
            id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(canonical_name, entity_type)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS memory_assertions (
            id TEXT PRIMARY KEY,
            subject_entity_id TEXT NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
            predicate TEXT NOT NULL,
            object_value TEXT NOT NULL,
            source_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
            recorded_at TEXT NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 1.0,
            extractor_version TEXT NOT NULL
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_assertions_lookup "
        "ON memory_assertions(subject_entity_id, predicate, status)"
    )
    db.execute("""
        CREATE TABLE IF NOT EXISTS assertion_edges (
            source_assertion_id TEXT NOT NULL REFERENCES memory_assertions(id) ON DELETE CASCADE,
            target_assertion_id TEXT NOT NULL REFERENCES memory_assertions(id) ON DELETE CASCADE,
            edge_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source_assertion_id, target_assertion_id, edge_type)
        )
    """)

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

    # --- Scheduled lifecycle jobs (P2 Phase A) ---
    # Phase A interprets ``cron_expr`` in UTC; ``last_run_at`` and
    # ``created_at`` are UTC ISO strings. ``list_due`` semantics are
    # at-most-once catch-up — see ``ScheduleMixin.schedule_list_due``.
    db.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id              TEXT PRIMARY KEY,
            cron_expr       TEXT NOT NULL,
            job_kind        TEXT NOT NULL,
            params_json     TEXT NOT NULL DEFAULT '{}',
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            last_run_at     TEXT,
            last_run_status TEXT,
            last_run_error  TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_schedules_enabled ON schedules(enabled)")

    # ---- stamp schema version (monotonic, after migrations) ----
    # After, not before: a failed migration must not leave the DB claiming a
    # version it doesn't have. Atomic upsert — racing processes can never
    # lower a canonical newer value; an equal value writes 0 rows. The second
    # WHERE clause restamps any non-canonical-integer text (e.g. '2abc',
    # '1.9') that the fence treated as corruption — SQLite CAST alone would
    # read its numeric prefix and leave it in place, disagreeing with the
    # fence's int() rule.
    db.execute(
        """
        INSERT INTO _memtomem_meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        WHERE CAST(_memtomem_meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)
           OR _memtomem_meta.value != CAST(CAST(_memtomem_meta.value AS INTEGER) AS TEXT)
        """,
        (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
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


def _decode_unicode_escaped(text: str) -> str:
    """Decode literal ``\\uXXXX`` escape text, composing surrogate pairs.

    Replacing each ``\\uXXXX`` token independently yields *lone* surrogate
    code points for non-BMP characters (an emoji is two escapes, e.g.
    ``\\ud83d\\ude00``). A UTF-16 ``surrogatepass`` round-trip recombines an
    adjacent high+low pair back into the real code point so the result is a
    str that SQLite can store. Strings with no escapes return unchanged.
    """
    decoded = _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(0)[2:], 16)), text)
    if decoded == text:
        return text
    try:
        return decoded.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        # Lone/un-pairable surrogate — hand back the per-escape decode; the
        # caller test-encodes before writing and skips anything un-encodable.
        return decoded


def _repair_unicode_escaped_tags(db: sqlite3.Connection, meta: MetaManager) -> int:
    """Decode literal ``\\uXXXX`` escapes left in chunk tags by older ingest.

    Tags written before the ``memory_writer`` ``ensure_ascii=False`` fix were
    serialized as their JSON escape *text* (e.g. ``\\ucee4\\ub9ac...``) and the
    markdown parser split the array by hand without JSON-decoding, so the
    literal escape survived into ``ChunkMetadata.tags`` and the DB. This pass
    walks the affected rows once, decodes the escapes to their characters,
    de-duplicates (preserving order), and rewrites the row. It is idempotent —
    completion is recorded in ``_memtomem_meta`` and clean tags (already
    Hangul) carry no escape text, so re-runs are no-ops.

    A row whose decoded tags cannot be UTF-8 encoded (un-pairable surrogate
    junk) is left untouched rather than crash startup.

    Returns the number of rows rewritten on this call (0 once recorded).
    """
    if meta.get_meta(_TAGS_UNICODE_REPAIR_KEY) == "done":
        return 0

    # ``ensure_ascii`` serialization means every non-ASCII tag's column text
    # contains ``\u`` — clean *and* broken alike — so the LIKE is only a cheap
    # pre-filter; the json.loads round-trip below is what tells them apart.
    rows = db.execute("SELECT id, tags FROM chunks WHERE tags LIKE '%\\u%'").fetchall()

    repaired = 0
    for chunk_id, tags_json in rows:
        if not tags_json:
            continue
        try:
            tags = json.loads(tags_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(tags, list):
            continue
        # Tags are contractually strings; a non-str element (e.g. a dict) is
        # malformed and un-hashable for the dedup below — leave such a row
        # untouched rather than crash startup.
        if not all(isinstance(tag, str) for tag in tags):
            continue
        seen: set[str] = set()
        new_tags: list[str] = []
        for tag in tags:
            decoded = _decode_unicode_escaped(tag)
            if decoded not in seen:
                seen.add(decoded)
                new_tags.append(decoded)
        if new_tags == tags:
            continue
        payload = json.dumps(new_tags, ensure_ascii=False)
        try:
            payload.encode("utf-8")
        except UnicodeEncodeError:
            # Un-encodable (lone surrogate) — skip rather than fail the write.
            continue
        db.execute("UPDATE chunks SET tags=? WHERE id=?", (payload, chunk_id))
        repaired += 1

    meta.set_meta(_TAGS_UNICODE_REPAIR_KEY, "done")
    return repaired


def _migrate_chunks_uniqueness(db: sqlite3.Connection, *, has_vec_table: bool) -> None:
    """One-time cleanup + UNIQUE constraint for ``chunks`` (#691).

    Multi-process indexing (``mm web`` watcher + ``mm`` CLI / MCP) on a
    shared SQLite DB used to insert duplicate rows that shared
    ``(namespace, source_file, content_hash, start_line)`` but differed
    only in ``id``. Each process holds its own ``asyncio.Lock`` and there
    was no storage-layer guard, so both ``INSERT`` calls succeeded with
    fresh UUIDs. The differ then reused only one of the IDs per
    re-indexing round, leaving the others as silent ghosts.

    Migration steps, gated on ``idx_chunks_unique_content`` so subsequent
    startups are a no-op:

    1. Group rows by ``(namespace, source_file, content_hash, start_line)``.
    2. Within each group, keep the row with the most accumulated
       personalization (``access_count + use_count``) — tie-break by oldest
       ``created_at``, then by ``id`` — and mark the rest for deletion.
    3. Delete the loser rowids from ``chunks_fts`` and ``chunks_vec``
       (the latter only when present), then from ``chunks``.
    4. Create the UNIQUE INDEX. Once present, future
       ``INSERT OR IGNORE`` calls in ``upsert_chunks`` silently drop
       race-loser rows at the storage layer.

    **Concurrent-startup safety**: the same multi-process scenario that
    caused #691 in the first place applies to the migration too — two
    processes booting simultaneously could each see ``already_migrated=False``
    on the cheap fast-path. We therefore wrap the migration body in
    ``BEGIN IMMEDIATE`` / ``COMMIT`` (acquires SQLite's RESERVED lock so
    the second process blocks until the first commits) and re-check the
    index existence inside the transaction. The deletes themselves are
    idempotent against missing rowids, so even an interrupted run on a
    fresh process restart converges.

    **SQLite version requirement**: the keeper-selection query uses
    ``ROW_NUMBER()`` which needs SQLite ≥ 3.25 (2018-09). Python 3.12's
    bundled ``sqlite3`` is well past that floor on every supported OS.
    """
    # Cheap fast-path so already-migrated DBs don't take the lock.
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_chunks_unique_content'"
        ).fetchone()
        is not None
    ):
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        # Re-check inside the transaction in case another startup beat us
        # to the index between our fast-path check and the lock acquisition.
        if (
            db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='index' AND name='idx_chunks_unique_content'"
            ).fetchone()
            is not None
        ):
            db.execute("COMMIT")
            return

        # Collect rowids of duplicate losers in one pass. ``ROW_NUMBER()``
        # over the partition picks the keeper (rn=1) by usage then age then
        # id; rn>1 are ghosts. Returns ``[]`` on a clean DB so the rest of
        # the migration is a single ``CREATE UNIQUE INDEX``.
        loser_rowids = [
            row[0]
            for row in db.execute(
                """
                SELECT rowid FROM (
                    SELECT rowid, ROW_NUMBER() OVER (
                        PARTITION BY namespace, source_file, content_hash, start_line
                        ORDER BY (access_count + use_count) DESC,
                                 created_at ASC,
                                 id ASC
                    ) AS rn
                    FROM chunks
                )
                WHERE rn > 1
                """
            ).fetchall()
        ]

        if loser_rowids:
            group_count = db.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT 1 FROM chunks "
                "GROUP BY namespace, source_file, content_hash, start_line "
                "HAVING COUNT(*) > 1"
                ")"
            ).fetchone()[0]
            # Chunked deletes keep the parameter list under SQLite's 999-host
            # limit on older builds. Sidecars first so an interrupted run
            # never leaves an FTS/vec entry pointing at a non-existent
            # ``chunks.rowid``.
            chunk_size = 500
            for i in range(0, len(loser_rowids), chunk_size):
                batch = loser_rowids[i : i + chunk_size]
                placeholders = ",".join("?" * len(batch))
                db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", batch)
                if has_vec_table:
                    db.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({placeholders})", batch)
                db.execute(f"DELETE FROM chunks WHERE rowid IN ({placeholders})", batch)
            logger.info(
                "Cleaned up %d duplicate chunk row(s) across %d group(s) before "
                "adding UNIQUE(namespace, source_file, content_hash, start_line) — see #691",
                len(loser_rowids),
                group_count,
            )

        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_unique_content "
            "ON chunks(namespace, source_file, content_hash, start_line)"
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
