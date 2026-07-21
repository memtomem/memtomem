"""Version-agnostic ``reset_all`` (#1826).

``reset_all`` must satisfy the "Delete ALL data" contract for *every* table in
the DB, not a hardcoded subset — otherwise an older binary resetting a DB
written by a newer one (which passes the downgrade fence, since additive tables
don't bump ``SCHEMA_VERSION``) silently leaves the newer tables' rows behind.
These tests pin the version-agnostic enumeration, the fail-closed handling of a
virtual table whose module is unavailable, transaction ownership, and the
bookkeeping wipes (``sqlite_stat*`` / ``sqlite_sequence``).
"""

from __future__ import annotations

import sqlite3

import pytest
import sqlite_vec
from helpers import make_chunk

import memtomem.storage.sqlite_backend as sqlite_backend
from memtomem.errors import StorageError

# The complete set of user-data tables ``reset_all`` must wipe. This is the
# parity guard: when a new user-data table is added to ``create_tables`` it
# appears in production's classification and this frozenset no longer matches,
# failing ``test_user_table_classification_is_pinned`` loudly — the signal to
# add it here (and confirm reset covers it). ``reset_all`` itself needs no edit.
EXPECTED_USER_TABLES = frozenset(
    {
        "access_log",
        "assertion_edges",
        "canonical_entities",
        "chunk_entities",
        "chunk_links",
        "chunk_relations",
        "chunks",
        "eval_case_labels",
        "eval_cases",
        "health_snapshots",
        "idempotency_ledger",
        "memory_assertions",
        "memory_candidate_transitions",
        "memory_candidates",
        "memory_policies",
        "namespace_metadata",
        "query_history",
        "schedules",
        "search_feedback",
        "session_events",
        "sessions",
        "working_memory",
    }
)

# Tables the pre-#1826 hardcoded list omitted — they either survived a reset
# entirely or were emptied only via a fragile FK cascade (unreported).
PREVIOUSLY_LEAKED = frozenset(
    {
        "assertion_edges",
        "canonical_entities",
        "chunk_links",
        "idempotency_ledger",
        "memory_assertions",
        "memory_candidate_transitions",
        "memory_candidates",
        "schedules",
    }
)


def _master_rows(db: sqlite3.Connection) -> list[tuple[str, str | None]]:
    return db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()


def _dummy_value(decl: str | None):
    d = (decl or "").upper()
    if "INT" in d:
        return 1
    if any(x in d for x in ("REAL", "FLOA", "DOUB")):
        return 1.0
    if "BLOB" in d:
        return b"x"
    return "x"


def _seed_row(db: sqlite3.Connection, table: str) -> None:
    """Insert one generic row into ``table`` respecting types/NOT NULL.

    Skips generated columns and the INTEGER-PRIMARY-KEY rowid alias. Callers
    disable foreign keys first so parent rows need not exist — the reset under
    test clears everything regardless of referential order.
    """
    cols = db.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
    names, values = [], []
    for _cid, name, decl, _notnull, _dflt, pk, hidden in cols:
        if hidden in (2, 3):  # generated columns cannot be inserted into
            continue
        if pk and "INT" in (decl or "").upper():  # autoincrement rowid alias
            continue
        names.append(f'"{name}"')
        values.append(_dummy_value(decl))
    placeholders = ",".join("?" * len(values))
    columns = ",".join(names)
    db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', values)


def _seed_all_user_tables(db: sqlite3.Connection, tables) -> None:
    db.execute("PRAGMA foreign_keys=OFF")
    for table in sorted(tables):
        _seed_row(db, table)
    db.execute("PRAGMA foreign_keys=ON")
    db.commit()


def _insert_schedule(db: sqlite3.Connection, sid: str = "s1") -> None:
    db.execute(
        "INSERT INTO schedules(id, cron_expr, job_kind, created_at) "
        "VALUES (?, '* * * * *', 'noop', '2026-01-01T00:00:00Z')",
        (sid,),
    )


def _make_module_less_vec_db(path: str) -> set[str]:
    """Create a real vec0 vtab, then return a fresh connection *without* the
    sqlite-vec module loaded — reproducing the #1826 downgrade shape where an
    older binary opens a DB containing a vtab whose module it lacks. Returns the
    prefix-derived shadow-name set for the ghost table.
    """
    creator = sqlite3.connect(path)
    creator.enable_load_extension(True)
    sqlite_vec.load(creator)
    creator.enable_load_extension(False)
    creator.execute("CREATE VIRTUAL TABLE ghost USING vec0(embedding float[4])")
    creator.execute("INSERT INTO ghost(rowid, embedding) VALUES (1, ?)", (b"\x00" * 16,))
    creator.commit()
    creator.close()
    return set()


# --------------------------------------------------------------------------- #
# Classification / parity guard
# --------------------------------------------------------------------------- #


async def test_user_table_classification_is_pinned(storage):
    """Production's ``_classify_tables`` must enumerate exactly the known
    user-data tables — an independent, self-updating drift guard. A new table
    added to ``create_tables`` breaks this until it is acknowledged here."""
    _virtual, _shadow, user = storage._classify_tables(_master_rows(storage._get_db()))
    assert set(user) == EXPECTED_USER_TABLES


async def test_prefix_collision_real_table_is_wiped(storage):
    """A real user table sharing a virtual table's name prefix (e.g.
    ``chunks_fts_private``) must NOT be misclassified as a shadow and skipped —
    ``table_list``'s authoritative ``shadow`` typing distinguishes it from a
    genuine shadow. It must be classified as a user table and wiped."""
    db = storage._get_db()
    db.execute("CREATE TABLE chunks_fts_private (id INTEGER PRIMARY KEY, secret TEXT)")
    db.execute("INSERT INTO chunks_fts_private(secret) VALUES ('leak-me')")
    db.commit()

    _virtual, shadow, user = storage._classify_tables(_master_rows(db))
    assert "chunks_fts_private" in user
    assert "chunks_fts_private" not in shadow
    # And genuine fts5 shadows are still protected from direct DELETE.
    assert "chunks_fts_data" in shadow

    deleted = await storage.reset_all()

    assert deleted["chunks_fts_private"] == 1
    assert db.execute("SELECT COUNT(*) FROM chunks_fts_private").fetchone()[0] == 0


async def test_vec0_vector_store_is_classified_shadow(storage):
    """``table_list`` types vec0's ``<vtab>_vector_chunks<NN>`` store as a plain
    ``table``; the narrow suffix add-back must still classify it as a shadow so
    it isn't directly DELETE-d (chunks_vec is drop+recreated instead)."""
    _virtual, shadow, user = storage._classify_tables(_master_rows(storage._get_db()))
    vec_stores = [n for n in shadow if n.startswith("chunks_vec_vector_chunks")]
    assert vec_stores, "vec0 vector store not classified as shadow"
    assert not any(n.startswith("chunks_vec_vector_chunks") for n in user)


async def test_attached_schema_shadow_does_not_mask_main_table(storage, tmp_path):
    """An attached DB whose FTS shadow name collides with a real ``main`` table
    must not cause that ``main`` table to be shadow-classified — the schema
    qualification in ``PRAGMA main.table_list`` closes this hole."""
    db = storage._get_db()
    db.execute("CREATE TABLE f_data (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO f_data(v) VALUES ('keep-me')")
    db.commit()

    side = str(tmp_path / "attached.db")
    db.execute("ATTACH DATABASE ? AS att", (side,))
    try:
        db.execute("CREATE VIRTUAL TABLE att.f USING fts5(body)")  # shadow att.f_data
        _virtual, shadow, user = storage._classify_tables(_master_rows(db))
        assert "f_data" in user
        assert "f_data" not in shadow
    finally:
        db.execute("DETACH DATABASE att")


# --------------------------------------------------------------------------- #
# Full wipe
# --------------------------------------------------------------------------- #


async def test_reset_all_wipes_every_user_table(storage):
    db = storage._get_db()
    _virtual, _shadow, user = storage._classify_tables(_master_rows(db))
    assert set(user) == EXPECTED_USER_TABLES  # seed set matches production
    _seed_all_user_tables(db, user)

    deleted = await storage.reset_all()

    for table in user:
        assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        assert deleted[table] == 1
    # Known virtual tables are reported with their own real counts (0 here).
    assert db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 0
    assert deleted["chunks_fts"] == 0
    assert deleted["chunks_vec"] == 0


async def test_reset_all_reports_real_fts_and_vec_counts(storage):
    """The FTS/vector receipt counts must be their own pre-reset row counts, not
    an alias of ``chunks`` or a hardcoded zero. Seed via the storage API so all
    three indexes are genuinely populated and independent."""
    await storage.upsert_chunks(
        [make_chunk(content=f"row {i}", source=f"f{i}.md") for i in range(3)]
    )
    db = storage._get_db()
    assert db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 3

    deleted = await storage.reset_all()

    assert deleted["chunks"] == 3
    assert deleted["chunks_fts"] == 3
    assert deleted["chunks_vec"] == 3
    assert db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 0


async def test_reset_all_reports_previously_leaked_tables(storage):
    db = storage._get_db()
    _virtual, _shadow, user = storage._classify_tables(_master_rows(db))
    _seed_all_user_tables(db, user)

    deleted = await storage.reset_all()

    for table in PREVIOUSLY_LEAKED:
        assert deleted.get(table) == 1, f"{table} missing from reset receipt"


async def test_reset_all_wipes_unknown_plain_table(storage):
    """The core #1826 scenario without two binaries: a table the running binary
    doesn't know about (as if added by a newer release) is still wiped."""
    db = storage._get_db()
    db.execute("CREATE TABLE extra_user_data (id INTEGER PRIMARY KEY, payload TEXT)")
    db.execute("INSERT INTO extra_user_data(payload) VALUES ('secret')")
    db.commit()

    deleted = await storage.reset_all()

    assert deleted["extra_user_data"] == 1
    assert db.execute("SELECT COUNT(*) FROM extra_user_data").fetchone()[0] == 0


async def test_reset_all_wipes_unknown_virtual_table(storage):
    """An unknown virtual table whose module IS available is dropped cleanly;
    its shadow tables are never directly DELETE-d."""
    db = storage._get_db()
    db.execute("CREATE VIRTUAL TABLE extra_fts USING fts5(body)")
    db.execute("INSERT INTO extra_fts(body) VALUES ('alpha')")
    db.execute("INSERT INTO extra_fts(body) VALUES ('beta')")
    db.commit()

    deleted = await storage.reset_all()

    assert deleted["extra_fts"] == 2
    assert (
        db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='extra_fts'").fetchone()[0] == 0
    )


async def test_reset_all_external_content_fts_count_is_pre_reset(storage):
    """An unknown external-content FTS5 table counts by reading its backing
    table. Its receipt count must be the pre-reset value — proof that unknown
    vtabs are counted before the plain-table deletes empty their content."""
    db = storage._get_db()
    db.execute("CREATE TABLE ext_src (id INTEGER PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO ext_src(id, body) VALUES (1, 'alpha'), (2, 'beta')")
    db.execute(
        "CREATE VIRTUAL TABLE ext_fts USING fts5(body, content='ext_src', content_rowid='id')"
    )
    db.commit()
    # External-content FTS counts by reading its content table (ext_src).
    assert db.execute("SELECT COUNT(*) FROM ext_fts").fetchone()[0] == 2

    deleted = await storage.reset_all()

    assert deleted["ext_fts"] == 2, "count must reflect rows before ext_src was emptied"
    assert db.execute("SELECT COUNT(*) FROM ext_src").fetchone()[0] == 0


async def test_reset_all_adversarial_table_name(storage):
    """Dynamic identifiers must be double-quote-escaped — a table name
    containing ``"`` and ``]`` must not break the reset SQL."""
    db = storage._get_db()
    db.execute('CREATE TABLE "we""ird] name" (x INTEGER)')
    db.execute('INSERT INTO "we""ird] name" VALUES (1)')
    db.commit()

    deleted = await storage.reset_all()

    assert deleted['we"ird] name'] == 1
    assert db.execute('SELECT COUNT(*) FROM "we""ird] name"').fetchone()[0] == 0


async def test_reset_all_clears_stats_and_sequence(storage):
    """``ANALYZE`` statistics carry user-derived samples and AUTOINCREMENT
    counters carry user-derived state — a full reset must clear both so a reset
    DB matches a fresh one (they stay out of the returned receipt)."""
    db = storage._get_db()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "INSERT INTO access_log(chunk_id, action, created_at) "
        "VALUES ('c1', 'read', '2026-01-01T00:00:00Z')"
    )
    db.execute("PRAGMA foreign_keys=ON")
    db.commit()
    db.execute("ANALYZE")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] > 0

    deleted = await storage.reset_all()

    assert db.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] == 0
    assert "sqlite_stat1" not in deleted
    assert "sqlite_sequence" not in deleted
    # AUTOINCREMENT restarts from 1 after the sequence wipe.
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "INSERT INTO access_log(chunk_id, action, created_at) "
        "VALUES ('c2', 'read', '2026-01-01T00:00:00Z')"
    )
    db.execute("PRAGMA foreign_keys=ON")
    db.commit()
    assert db.execute("SELECT MIN(id) FROM access_log").fetchone()[0] == 1


async def test_reset_all_preserves_meta_and_schema_version(storage):
    db = storage._get_db()
    db.execute(
        "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES ('ai_summary:/x.md', 'prose')"
    )
    db.commit()

    deleted = await storage.reset_all()

    assert deleted["ai_summaries"] == 1
    assert storage._get_stored_dimension() is not None
    assert (
        db.execute("SELECT COUNT(*) FROM _memtomem_meta WHERE key LIKE 'ai_summary:%'").fetchone()[
            0
        ]
        == 0
    )
    assert (
        db.execute("SELECT value FROM _memtomem_meta WHERE key='schema_version'").fetchone()
        is not None
    )


# --------------------------------------------------------------------------- #
# Transaction ownership / FK deferral
# --------------------------------------------------------------------------- #


async def test_reset_all_fk_deferral(storage):
    """Deletion order is arbitrary under the version-agnostic enumeration, so a
    default (``NO ACTION``) FK between two unknown tables must not raise — proof
    that ``defer_foreign_keys`` is active — and the pragma resets afterwards."""
    db = storage._get_db()
    db.execute("CREATE TABLE k_parent (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE k_child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES k_parent(id))")
    db.execute("INSERT INTO k_parent(id) VALUES (1)")
    db.execute("INSERT INTO k_child(id, pid) VALUES (1, 1)")
    db.commit()

    deleted = await storage.reset_all()

    assert deleted["k_parent"] == 1
    assert deleted["k_child"] == 1
    assert db.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0


async def test_reset_all_inside_transaction_commit(storage):
    """Inside ``transaction()`` the wipe participates in the outer commit."""
    db = storage._get_db()
    _insert_schedule(db)
    db.commit()

    async with storage.transaction():
        await storage.reset_all()

    assert db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
    assert db.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0


async def test_reset_all_inside_transaction_rollback(storage):
    """A forced rollback of the outer transaction restores everything — the
    reset is a single unit with the surrounding context."""
    db = storage._get_db()
    _insert_schedule(db)
    db.commit()

    with pytest.raises(RuntimeError, match="boom"):
        async with storage.transaction():
            await storage.reset_all()
            raise RuntimeError("boom")

    assert db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
    assert db.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0


async def test_reset_all_acquires_write_lock_before_enumeration(storage):
    """The real ``reset_all`` must issue ``BEGIN IMMEDIATE`` (taking the write
    lock) *before* it reads ``sqlite_master`` — otherwise a concurrent writer
    could change the table set between enumeration and deletion. Trace the SQL
    the method actually runs and assert the ordering. Would fail if production
    moved or dropped the lock acquisition."""
    db = storage._get_db()
    traced: list[str] = []
    db.set_trace_callback(lambda sql: traced.append(sql))
    try:
        await storage.reset_all()
    finally:
        db.set_trace_callback(None)

    begin_idx = next(
        i for i, s in enumerate(traced) if s.strip().upper().startswith("BEGIN IMMEDIATE")
    )
    enum_idx = next(i for i, s in enumerate(traced) if "FROM sqlite_master" in s)
    assert begin_idx < enum_idx


async def test_reset_all_acquires_write_lock_inside_transaction_cm(storage):
    """The outer CM locks before reset enumerates tables, so no writer can
    create a table between enumeration and deletion."""
    db = storage._get_db()
    traced: list[str] = []
    db.set_trace_callback(lambda sql: traced.append(sql))
    try:
        async with storage.transaction():
            await storage.reset_all()
    finally:
        db.set_trace_callback(None)

    begin_idx = next(
        i for i, s in enumerate(traced) if s.strip().upper().startswith("BEGIN IMMEDIATE")
    )
    enum_idx = next(i for i, s in enumerate(traced) if "FROM sqlite_master" in s)
    assert begin_idx < enum_idx


async def test_reset_all_concurrent_writer_excluded(storage):
    """With the reset's write transaction open, a second connection (timeout=0)
    cannot interleave a write — the lock is real, not advisory."""
    db = storage._get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        other = sqlite3.connect(str(storage._config.sqlite_path), timeout=0)
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            _insert_schedule(other, "x")
            other.commit()
        other.close()
    finally:
        db.rollback()


# --------------------------------------------------------------------------- #
# Unknown-module fallback (fail closed) — helper unit tests + reset_all flow
# --------------------------------------------------------------------------- #


def test_is_missing_module_error_predicate():
    assert sqlite_backend._is_missing_module_error(sqlite3.OperationalError("no such module: vec0"))
    assert not sqlite_backend._is_missing_module_error(
        sqlite3.OperationalError("database is locked")
    )
    assert not sqlite_backend._is_missing_module_error(sqlite3.OperationalError("disk I/O error"))


async def test_unknown_module_fallback_wipes_shadows(storage, tmp_path):
    """Against a real module-less connection (a vec0 vtab created elsewhere),
    the helper's ``COUNT``/``DROP`` both raise ``no such module``; with fallback
    allowed it deletes the shadow rows (data destroyed), leaves the definition,
    and reports ``incomplete=True``."""
    path = str(tmp_path / "vt.db")
    _make_module_less_vec_db(path)
    ml = sqlite3.connect(path)
    rows = _master_rows(ml)
    virtual = {n for n, sql in rows if sqlite_backend._is_virtual_table_sql(sql)}
    shadow = {
        n for n, _ in rows if n not in virtual and any(n.startswith(v + "_") for v in virtual)
    }
    assert ml.execute("SELECT COUNT(*) FROM ghost_rowids").fetchone()[0] == 1

    ml.execute("BEGIN IMMEDIATE")
    count, incomplete = storage._reset_unknown_virtual_table(ml, "ghost", shadow, True)
    ml.commit()

    assert (count, incomplete) == (0, True)
    assert ml.execute("SELECT COUNT(*) FROM ghost_rowids").fetchone()[0] == 0
    assert ml.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='ghost'").fetchone()[0] == 1
    ml.close()


async def test_unknown_module_fallback_refused_when_not_owned(storage, tmp_path):
    """Inside a borrowed transaction (fallback disallowed) the helper refuses:
    raises with 'standalone' guidance and touches no shadow rows."""
    path = str(tmp_path / "vt.db")
    _make_module_less_vec_db(path)
    ml = sqlite3.connect(path)
    rows = _master_rows(ml)
    virtual = {n for n, sql in rows if sqlite_backend._is_virtual_table_sql(sql)}
    shadow = {
        n for n, _ in rows if n not in virtual and any(n.startswith(v + "_") for v in virtual)
    }

    ml.execute("BEGIN IMMEDIATE")
    with pytest.raises(StorageError, match="standalone"):
        storage._reset_unknown_virtual_table(ml, "ghost", shadow, False)
    assert ml.execute("SELECT COUNT(*) FROM ghost_rowids").fetchone()[0] == 1
    ml.rollback()
    ml.close()


async def test_unknown_module_negative_path_reraises(storage, tmp_path, monkeypatch):
    """When the error is NOT classified as missing-module, the helper re-raises
    it verbatim and performs no shadow surgery."""
    path = str(tmp_path / "vt.db")
    _make_module_less_vec_db(path)
    ml = sqlite3.connect(path)
    rows = _master_rows(ml)
    virtual = {n for n, sql in rows if sqlite_backend._is_virtual_table_sql(sql)}
    shadow = {
        n for n, _ in rows if n not in virtual and any(n.startswith(v + "_") for v in virtual)
    }
    monkeypatch.setattr(sqlite_backend, "_is_missing_module_error", lambda exc: False)

    ml.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="no such module"):
        storage._reset_unknown_virtual_table(ml, "ghost", shadow, True)
    assert ml.execute("SELECT COUNT(*) FROM ghost_rowids").fetchone()[0] == 1
    ml.rollback()
    ml.close()


async def test_reset_all_fails_closed_on_incomplete(storage, monkeypatch):
    """When an unknown vtab can't be proven fully wiped, ``reset_all`` commits
    the work it could and raises the incomplete-reset error — never the generic
    'transaction rolled back' wrap, and never silent success."""
    db = storage._get_db()
    db.execute("CREATE VIRTUAL TABLE extra_fts USING fts5(body)")
    db.execute("INSERT INTO extra_fts(body) VALUES ('x')")
    _insert_schedule(db)
    db.commit()

    monkeypatch.setattr(storage, "_reset_unknown_virtual_table", lambda db, name, sh, af: (0, True))
    with pytest.raises(StorageError, match="reset incomplete.*unknown module"):
        await storage.reset_all()

    # The rest of the wipe is committed despite the raised error.
    assert db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0


async def test_reset_all_cm_refusal_rolls_back(storage, monkeypatch):
    """A fail-closed refusal inside ``transaction()`` propagates its remediation
    message and the outer CM rolls the whole reset back."""
    db = storage._get_db()
    db.execute("CREATE VIRTUAL TABLE extra_fts USING fts5(body)")
    _insert_schedule(db)
    db.commit()

    def _refuse(db, name, sh, af):
        raise StorageError(f"reset incomplete: table {name!r} ... re-run reset standalone")

    monkeypatch.setattr(storage, "_reset_unknown_virtual_table", _refuse)
    with pytest.raises(StorageError, match="standalone"):
        async with storage.transaction():
            await storage.reset_all()

    assert db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 1
