"""One-shot DB repair of unicode-escaped chunk tags.

Tags ingested before the ``memory_writer`` ``ensure_ascii=False`` fix were
stored as their literal ``\\uXXXX`` escape text instead of the characters they
encode (a Korean tag surfaced in the tag cloud as ``\\ucee4...``). The
``_repair_unicode_escaped_tags`` migration in ``create_tables`` decodes those
rows once per database without forcing a full re-embed re-index.

These mirror the ``chunk_links`` back-fill tests: seed AFTER the first
``create_tables`` pass, clear the idempotency marker, then re-init so the
migration runs against the seeded rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import sqlite_vec

from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import create_tables

_MARKER_KEY = "tags_unicode_repair_v1"


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _initialize(db: sqlite3.Connection) -> None:
    meta = MetaManager(lambda: db)
    create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")


def _insert_chunk(db: sqlite3.Connection, chunk_id: str, *, tags_json: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
        "tags, created_at, updated_at) VALUES (?, '', ?, '', 'default', ?, ?, ?)",
        (chunk_id, chunk_id, tags_json, now, now),
    )


def _rerun_migration(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM _memtomem_meta WHERE key=?", (_MARKER_KEY,))
    db.commit()
    _initialize(db)


def _escaped_text(hangul: str) -> str:
    """The literal ``\\uXXXX`` escape *text* an older file stored for *hangul*."""
    escaped = json.dumps(hangul)[1:-1]  # strip the surrounding quotes
    assert "\\u" in escaped, "fixture must reproduce the on-disk escaped shape"
    return escaped


def _stored_tags(db: sqlite3.Connection, chunk_id: str) -> list[str]:
    row = db.execute("SELECT tags FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    return json.loads(row[0])


class TestRepairUnicodeEscapedTags:
    def test_decodes_escaped_tag_to_hangul(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            broken = _escaped_text("커리큘럼설계")
            _insert_chunk(db, "esc-1", tags_json=json.dumps([broken]))
            _rerun_migration(db)
            assert _stored_tags(db, "esc-1") == ["커리큘럼설계"]
        finally:
            db.close()

    def test_decodes_surrogate_pair_emoji(self) -> None:
        """Non-BMP tags (emoji = a high+low ``\\uXXXX`` pair) decode atomically.

        Decoding each escape independently would yield two lone surrogates and
        crash the row's ``UPDATE`` on UTF-8 encode; the UTF-16 recompose fixes
        it. Regression for the surrogate-pair startup crash.
        """
        db = _connect()
        try:
            _initialize(db)
            broken = _escaped_text("😀")  # → literal '😀' text
            _insert_chunk(db, "emoji-1", tags_json=json.dumps([broken]))
            _rerun_migration(db)  # must not raise
            assert _stored_tags(db, "emoji-1") == ["😀"]
        finally:
            db.close()

    def test_unpairable_surrogate_left_untouched(self) -> None:
        """A lone (high-only) surrogate escape can't form valid UTF-8 — the row
        must be skipped, never crash startup."""
        db = _connect()
        try:
            _initialize(db)
            # Column text ``["\\ud83d"]`` → json.loads yields the *escape text*
            # ``\ud83d`` (6 chars), which decodes to an un-encodable surrogate.
            _insert_chunk(db, "lone-1", tags_json=r'["\\ud83d"]')
            _rerun_migration(db)  # must not raise
            assert _stored_tags(db, "lone-1") == [r"\ud83d"]
        finally:
            db.close()

    def test_clean_hangul_tag_untouched(self) -> None:
        """A correctly-stored Hangul tag (ensure_ascii column text) decodes fine
        and the migration leaves its value intact."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "clean-1", tags_json=json.dumps(["재사용자산"]))
            _rerun_migration(db)
            assert _stored_tags(db, "clean-1") == ["재사용자산"]
        finally:
            db.close()

    def test_ascii_tag_untouched(self) -> None:
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "ascii-1", tags_json='["FCA", "shared-from=abc"]')
            _rerun_migration(db)
            assert _stored_tags(db, "ascii-1") == ["FCA", "shared-from=abc"]
        finally:
            db.close()

    def test_decode_collapses_duplicate(self) -> None:
        """Escaped + already-decoded copies of the same tag de-dup to one."""
        db = _connect()
        try:
            _initialize(db)
            broken = _escaped_text("커리큘럼설계")
            _insert_chunk(db, "dup-1", tags_json=json.dumps([broken, "커리큘럼설계"]))
            _rerun_migration(db)
            assert _stored_tags(db, "dup-1") == ["커리큘럼설계"]
        finally:
            db.close()

    def test_idempotent_marker_short_circuits(self) -> None:
        """Marker recorded; rows added after the run are not re-scanned."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "esc-early", tags_json=json.dumps([_escaped_text("초안")]))
            _rerun_migration(db)
            assert _stored_tags(db, "esc-early") == ["초안"]

            marker = db.execute(
                "SELECT value FROM _memtomem_meta WHERE key=?", (_MARKER_KEY,)
            ).fetchone()
            assert marker is not None and marker[0] == "done"

            # A new escaped row added AFTER the migration must stay untouched
            # (later writes go through the fixed writer/parser, not a re-scan).
            broken_late = _escaped_text("후속")
            _insert_chunk(db, "esc-late", tags_json=json.dumps([broken_late]))
            db.commit()
            _initialize(db)  # marker still "done" → no-op
            assert _stored_tags(db, "esc-late") == [broken_late]
        finally:
            db.close()

    def test_running_twice_is_stable(self) -> None:
        """Re-running the repair on already-decoded data changes nothing."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "esc-2", tags_json=json.dumps([_escaped_text("재사용자산")]))
            _rerun_migration(db)
            first = _stored_tags(db, "esc-2")
            _rerun_migration(db)
            assert _stored_tags(db, "esc-2") == first == ["재사용자산"]
        finally:
            db.close()

    def test_corrupt_tags_json_skipped(self) -> None:
        """Malformed ``tags`` JSON must not crash the migration."""
        db = _connect()
        try:
            _initialize(db)
            _insert_chunk(db, "bad-1", tags_json="not json \\u0041 at all")
            _rerun_migration(db)  # must not raise
        finally:
            db.close()

    def test_non_string_tag_element_left_untouched(self) -> None:
        """A valid JSON array whose element is non-str (e.g. a dict) is malformed
        and un-hashable for dedup — the row must be skipped, never crash."""
        db = _connect()
        try:
            _initialize(db)
            # ensure_ascii column text carries \u so the LIKE prefilter walks it.
            _insert_chunk(db, "obj-1", tags_json=json.dumps([{"label": "가"}]))
            _rerun_migration(db)  # must not raise
            assert _stored_tags(db, "obj-1") == [{"label": "가"}]
        finally:
            db.close()
