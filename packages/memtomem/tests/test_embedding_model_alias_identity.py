"""An ONNX short alias and its full id are one stored embedding identity (#2463).

``multilingual-e5-small`` and ``intfloat/multilingual-e5-small`` load the same
model and produce the same vectors, so switching a config between the two
spellings must not report a model mismatch — that mismatch blocks index writes
until the user resolves it, and ``apply-current`` deletes every vector.

The stamp keeps the spelling it was written with — status and recovery
guidance report it back to the user — so the fixtures below seed the raw meta
row explicitly and the fix lives entirely in the comparison.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from memtomem.config import EmbeddingConfig, StorageConfig, embedding_policy_fingerprint
from memtomem.embedding.identity import canonical_embedding_model, same_embedding_model
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.storage.sqlite_backend import SqliteBackend

from .helpers import make_chunk

E5_SHORT = "multilingual-e5-small"
E5_FULL = "intfloat/multilingual-e5-small"


def _backend(db_path: Path, embedding: EmbeddingConfig) -> SqliteBackend:
    return SqliteBackend(
        StorageConfig(sqlite_path=db_path),
        dimension=embedding.dimension,
        embedding_provider=embedding.provider,
        embedding_model=embedding.model,
        embedding_policy_fingerprint=embedding_policy_fingerprint(embedding),
        embedding_max_sequence_tokens=embedding.max_sequence_tokens,
        strict_dim_check=False,
    )


def _stored_model(db_path: Path) -> str | None:
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT value FROM _memtomem_meta WHERE key = 'embedding_model'"
        ).fetchone()
    return row[0] if row else None


async def _seed_populated_store(
    db_path: Path, embedding: EmbeddingConfig, *, raw_model: str | None = None
) -> list[float]:
    """Index one chunk, then optionally rewrite the model row to a legacy spelling."""
    vector = [1.0] + [0.0] * (embedding.dimension - 1)
    storage = _backend(db_path, embedding)
    await storage.initialize()
    try:
        await storage.upsert_chunks([make_chunk("alias identity evidence", embedding=vector)])
    finally:
        await storage.close()
    if raw_model is not None:
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE _memtomem_meta SET value = ? WHERE key = 'embedding_model'", (raw_model,)
            )
    return vector


async def _reopen_mismatch(db_path: Path, embedding: EmbeddingConfig, vector: list[float]):
    storage = _backend(db_path, embedding)
    await storage.initialize()
    try:
        mismatch = storage.embedding_mismatch
        coverage = await storage.get_dense_coverage()
        hits = await storage.dense_search(vector, top_k=1)
        return mismatch, coverage, [hit.chunk.content for hit in hits]
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("provider", "stored", "configured", "dimension"),
    [
        ("onnx", E5_SHORT, E5_FULL, 384),
        ("onnx", E5_FULL, E5_SHORT, 384),
        ("onnx", "bge-m3", "BAAI/bge-m3", 1024),
        ("onnx", "BAAI/bge-m3", "bge-m3", 1024),
        ("ONNX", E5_SHORT, E5_FULL, 384),
    ],
)
async def test_legacy_stamp_in_either_spelling_matches_the_other(
    tmp_path, provider, stored, configured, dimension
) -> None:
    db_path = tmp_path / "memtomem.db"
    stored_cfg = EmbeddingConfig(provider=provider, model=stored, dimension=dimension)
    vector = await _seed_populated_store(db_path, stored_cfg, raw_model=stored)
    assert _stored_model(db_path) == stored

    configured_cfg = EmbeddingConfig(provider=provider, model=configured, dimension=dimension)
    mismatch, coverage, hits = await _reopen_mismatch(db_path, configured_cfg, vector)

    assert mismatch is None
    assert coverage == {"total": 1, "with_dense": 1}
    assert hits == ["alias identity evidence"]
    # Every write gate reads the shared result, so nothing refuses the write.
    assert (
        _check_embedding_mismatch(
            SimpleNamespace(storage=SimpleNamespace(embedding_mismatch=mismatch))
        )
        is None
    )
    # Compatibility comes from the comparison; the legacy row is not rewritten.
    assert _stored_model(db_path) == stored


@pytest.mark.parametrize(
    ("stored", "configured"),
    [
        # A genuinely different model.
        (
            EmbeddingConfig(provider="onnx", model=E5_SHORT, dimension=384),
            EmbeddingConfig(provider="onnx", model="bge-small-en-v1.5", dimension=384),
        ),
        # Ollama and OpenAI send names verbatim; a short and a full name may differ.
        (
            EmbeddingConfig(provider="ollama", model="bge-m3", dimension=1024),
            EmbeddingConfig(provider="ollama", model="BAAI/bge-m3", dimension=1024),
        ),
        (
            EmbeddingConfig(provider="openai", model=E5_SHORT, dimension=384),
            EmbeddingConfig(provider="openai", model=E5_FULL, dimension=384),
        ),
        # A cross-provider pair whose ONNX side resolves to the other's name.
        (
            EmbeddingConfig(provider="ollama", model="BAAI/bge-m3", dimension=1024),
            EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024),
        ),
    ],
)
async def test_real_model_differences_still_mismatch(tmp_path, stored, configured) -> None:
    db_path = tmp_path / "memtomem.db"
    vector = await _seed_populated_store(db_path, stored, raw_model=stored.model)

    mismatch, coverage, _hits = await _reopen_mismatch(db_path, configured, vector)

    assert mismatch is not None
    assert mismatch["model_mismatch"] is True
    assert coverage == {"total": 1, "with_dense": 1}


async def test_alias_does_not_hide_a_policy_mismatch(tmp_path) -> None:
    db_path = tmp_path / "memtomem.db"
    stored = EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024)
    vector = await _seed_populated_store(db_path, stored, raw_model="bge-m3")
    configured = EmbeddingConfig(
        provider="onnx", model="BAAI/bge-m3", dimension=1024, max_sequence_tokens=128
    )

    mismatch, _coverage, _hits = await _reopen_mismatch(db_path, configured, vector)

    assert mismatch is not None
    assert mismatch["model_mismatch"] is False
    assert mismatch["policy_mismatch"] is True


async def test_partial_stamp_stays_unknown(tmp_path) -> None:
    db_path = tmp_path / "memtomem.db"
    stored = EmbeddingConfig(provider="onnx", model=E5_SHORT, dimension=384)
    vector = await _seed_populated_store(db_path, stored)
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM _memtomem_meta WHERE key = 'embedding_model'")

    mismatch, _coverage, _hits = await _reopen_mismatch(
        db_path, EmbeddingConfig(provider="onnx", model=E5_FULL, dimension=384), vector
    )

    assert mismatch is not None
    assert mismatch["model_mismatch"] is True


@pytest.mark.parametrize(
    "embedding",
    [
        EmbeddingConfig(provider="onnx", model=E5_SHORT, dimension=384),
        EmbeddingConfig(provider="ollama", model="bge-m3", dimension=1024),
    ],
)
async def test_new_and_reset_stamps_keep_the_configured_spelling(tmp_path, embedding) -> None:
    db_path = tmp_path / "memtomem.db"
    storage = _backend(db_path, embedding)
    await storage.initialize()
    try:
        assert _stored_model(db_path) == embedding.model
        await storage.reset_embedding_meta(
            1024,
            "onnx",
            "bge-m3",
            embedding_policy_fingerprint(
                EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024)
            ),
        )
        assert _stored_model(db_path) == "bge-m3"
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("provider_a", "model_a", "provider_b", "model_b", "expected"),
    [
        ("onnx", E5_SHORT, "onnx", E5_FULL, True),
        (" Onnx ", "bge-m3", "ONNX", "BAAI/bge-m3", True),
        ("onnx", E5_SHORT, "ollama", E5_FULL, False),
        ("ollama", "bge-m3", "ollama", "BAAI/bge-m3", False),
        ("ollama", "bge-m3", "onnx", "bge-m3", True),  # same spelling: unchanged
        ("onnx", E5_SHORT, "onnx", "bge-m3", False),
    ],
)
def test_same_embedding_model(provider_a, model_a, provider_b, model_b, expected) -> None:
    assert same_embedding_model(provider_a, model_a, provider_b, model_b) is expected


def test_canonical_embedding_model_is_onnx_only() -> None:
    assert canonical_embedding_model("onnx", E5_SHORT) == E5_FULL
    assert canonical_embedding_model("ollama", E5_SHORT) == E5_SHORT
    assert canonical_embedding_model("onnx", None) == ""
