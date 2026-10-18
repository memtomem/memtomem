"""Storage init does not stamp a populated store's missing policy from a mismatched opener (#2619).

The ``embedding_policy_fingerprint`` row is the only record of the policy the
stored vectors were built with. Init used to backfill a missing row from
whichever config opened the store first, so a populated ONNX store opened
under ``provider=none`` was recorded as ``none:v1``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from helpers import make_chunk

from memtomem.config import EmbeddingConfig, StorageConfig, embedding_policy_fingerprint
from memtomem.storage.sqlite_backend import SqliteBackend

_E5 = "intfloat/multilingual-e5-small"
_POLICY_ROWS = ("embedding_policy_fingerprint", "embedding_max_sequence_tokens")


def _rows(db_path: Path, *keys: str) -> dict[str, str | None]:
    with sqlite3.connect(db_path) as db:
        return {
            key: (
                row[0]
                if (
                    row := db.execute(
                        "SELECT value FROM _memtomem_meta WHERE key=?", (key,)
                    ).fetchone()
                )
                else None
            )
            for key in keys
        }


def _write_rows(db_path: Path, **rows: str | None) -> None:
    with sqlite3.connect(db_path) as db:
        for key, value in rows.items():
            if value is None:
                db.execute("DELETE FROM _memtomem_meta WHERE key=?", (key,))
            else:
                db.execute(
                    "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
                    (key, value),
                )


async def _populated_e5_store(db_path: Path) -> None:
    """One E5 vector, then the policy rows removed as a pre-policy store lacks them."""
    section = EmbeddingConfig(provider="onnx", model=_E5)
    storage = SqliteBackend(
        StorageConfig(sqlite_path=db_path),
        dimension=section.dimension,
        embedding_provider="onnx",
        embedding_model=_E5,
        embedding_policy_fingerprint=embedding_policy_fingerprint(section),
        embedding_max_sequence_tokens=section.max_sequence_tokens,
    )
    await storage.initialize()
    try:
        await storage.upsert_chunks([make_chunk(embedding=[0.1] * section.dimension)])
    finally:
        await storage.close()
    _write_rows(db_path, **dict.fromkeys(_POLICY_ROWS))


def _none_opener(db_path: Path) -> SqliteBackend:
    # The fingerprint is passed explicitly: an empty one skips the backfill
    # altogether, which would make the assertions below pass on old code.
    return SqliteBackend(
        StorageConfig(sqlite_path=db_path),
        dimension=0,
        embedding_provider="none",
        embedding_model="",
        embedding_policy_fingerprint="none:v1",
        strict_dim_check=False,
    )


async def test_a_none_opener_leaves_a_populated_onnx_store_unstamped(tmp_path):
    db_path = tmp_path / "e5.db"
    await _populated_e5_store(db_path)

    storage = _none_opener(db_path)
    await storage.initialize()
    try:
        assert _rows(db_path, *_POLICY_ROWS) == dict.fromkeys(_POLICY_ROWS)
        assert storage.stored_embedding_info["policy_fingerprint"] is None
        assert storage.stored_embedding_info["max_sequence_tokens"] is None
        mismatch = storage.embedding_mismatch
        assert mismatch is not None
        assert mismatch["stored"]["policy_fingerprint"] is None
        assert mismatch["stored"]["max_sequence_tokens"] is None
        assert await storage.get_vector_count() == 1
    finally:
        await storage.close()


async def test_a_surviving_cap_row_is_unknown_without_its_policy(tmp_path):
    db_path = tmp_path / "e5.db"
    await _populated_e5_store(db_path)
    _write_rows(db_path, embedding_max_sequence_tokens="1024")

    storage = _none_opener(db_path)
    await storage.initialize()
    try:
        assert storage.stored_embedding_info["max_sequence_tokens"] is None
    finally:
        await storage.close()


async def test_the_mismatch_report_keeps_the_open_time_answer(tmp_path):
    """A row another process writes later is not reported as this opener's
    policy, and reading the report never needs the writer connection."""
    db_path = tmp_path / "e5.db"
    await _populated_e5_store(db_path)
    storage = _none_opener(db_path)
    await storage.initialize()
    try:
        _write_rows(db_path, embedding_policy_fingerprint="ollama:v1")
        assert storage.embedding_mismatch["stored"]["policy_fingerprint"] is None

        async def read_in_another_task():
            return storage.embedding_mismatch

        async with storage.transaction():
            mismatch = await asyncio.create_task(read_in_another_task())
        assert mismatch["stored"]["policy_fingerprint"] is None
    finally:
        await storage.close()


async def test_a_failed_reset_still_reports_the_policy_as_unknown(tmp_path):
    """The reset writes the policy row, then fails and rolls it back."""
    db_path = tmp_path / "e5.db"
    await _populated_e5_store(db_path)
    storage = _none_opener(db_path)
    await storage.initialize()
    try:

        def deny_vector_table(action, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_VTABLE else 0

        storage._get_db().set_authorizer(deny_vector_table)
        with pytest.raises(sqlite3.DatabaseError):
            await storage.reset_embedding_meta(
                dimension=768,
                provider="ollama",
                model="nomic-embed-text",
                policy_fingerprint="ollama:v1",
            )
        storage._get_db().set_authorizer(None)
        assert _rows(db_path, "embedding_policy_fingerprint") == {
            "embedding_policy_fingerprint": None
        }
        assert storage.embedding_mismatch["stored"]["policy_fingerprint"] is None
    finally:
        await storage.close()


async def test_a_matching_onnx_opener_then_records_the_legacy_policy(tmp_path):
    db_path = tmp_path / "e5.db"
    await _populated_e5_store(db_path)
    storage = _none_opener(db_path)
    await storage.initialize()
    await storage.close()

    section = EmbeddingConfig(provider="onnx", model=_E5)
    matching = SqliteBackend(
        StorageConfig(sqlite_path=db_path),
        dimension=section.dimension,
        embedding_provider="onnx",
        embedding_model=_E5,
        embedding_policy_fingerprint=embedding_policy_fingerprint(section),
        embedding_max_sequence_tokens=section.max_sequence_tokens,
    )
    await matching.initialize()
    await matching.close()
    assert _rows(db_path, *_POLICY_ROWS) == {
        "embedding_policy_fingerprint": "onnx:v1:max_sequence_tokens=0",
        "embedding_max_sequence_tokens": "0",
    }


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({"embedding_provider": None, "embedding_model": _E5}, id="model-only"),
        pytest.param({"embedding_provider": "", "embedding_model": _E5}, id="empty-provider"),
        pytest.param({"embedding_provider": "ONNX", "embedding_model": ""}, id="provider-only"),
    ],
)
async def test_a_partial_identity_stamp_is_not_backfilled(tmp_path, identity):
    db_path = tmp_path / "partial.db"
    await _populated_e5_store(db_path)
    _write_rows(db_path, **identity)

    storage = _none_opener(db_path)
    await storage.initialize()
    await storage.close()
    assert _rows(db_path, "embedding_policy_fingerprint")["embedding_policy_fingerprint"] is None


async def test_a_fresh_store_records_the_opener_policy(tmp_path):
    db_path = tmp_path / "fresh.db"
    storage = _none_opener(db_path)
    await storage.initialize()
    try:
        assert _rows(db_path, "embedding_policy_fingerprint") == {
            "embedding_policy_fingerprint": "none:v1"
        }
        assert storage.stored_embedding_info["policy_fingerprint"] == "none:v1"
    finally:
        await storage.close()


async def test_a_store_without_identity_rows_keeps_the_old_backfill(tmp_path):
    """A pre-identity store says nothing to disagree with; behaviour is unchanged."""
    db_path = tmp_path / "pre-identity.db"
    await _populated_e5_store(db_path)
    _write_rows(db_path, embedding_provider=None, embedding_model=None)

    storage = _none_opener(db_path)
    await storage.initialize()
    await storage.close()
    assert _rows(db_path, "embedding_policy_fingerprint") == {
        "embedding_policy_fingerprint": "none:v1"
    }


async def test_the_same_non_onnx_provider_adopts_its_policy(tmp_path):
    db_path = tmp_path / "ollama.db"

    def ollama(model: str) -> SqliteBackend:
        return SqliteBackend(
            StorageConfig(sqlite_path=db_path),
            dimension=4,
            embedding_provider="ollama",
            embedding_model=model,
            embedding_policy_fingerprint="ollama:v1",
            strict_dim_check=False,
        )

    storage = ollama("nomic-embed-text")
    await storage.initialize()
    try:
        await storage.upsert_chunks([make_chunk(embedding=[0.1] * 4)])
    finally:
        await storage.close()
    _write_rows(db_path, **dict.fromkeys(_POLICY_ROWS))

    reopened = ollama("nomic-embed-text")
    await reopened.initialize()
    await reopened.close()
    assert _rows(db_path, "embedding_policy_fingerprint") == {
        "embedding_policy_fingerprint": "ollama:v1"
    }
