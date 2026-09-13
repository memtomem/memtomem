"""An empty dim=0 store adopts the configured embedding identity (#2416).

A store opened while the ``embedding`` config section was rejected — or under
``provider="none"`` — is stamped dimension 0. After the config names a real
provider, the ordinary component build (``create_storage``) takes the
configured identity when the store holds no ``chunks_vec`` and no chunk rows,
instead of reporting a mismatch whose ``apply-current`` reset would destroy
nothing. Recovery and probe opens (the ``SqliteBackend`` default) keep
observing the stamp as recorded.

Also pins the provider/model backfill: a stored provider ``none`` whose model
row is absent or empty is reported as ``none``/``""``, not completed with the
configured model and provider. Other partial stamps keep the legacy backfill.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.config import Mem2MemConfig, StorageConfig
from memtomem.errors import EmbeddingDimensionMismatchError, SchemaDowngradeError
from memtomem.storage.factory import create_storage
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import SCHEMA_VERSION

from .helpers import make_chunk, set_home

_E5 = "intfloat/multilingual-e5-small"
_POLICY = "onnx:v1:max_sequence_tokens=512:test"


def _config(db_path: Path, *, dimension: int = 384, model: str = _E5) -> Mem2MemConfig:
    cfg = Mem2MemConfig()
    cfg.storage.sqlite_path = db_path
    cfg.embedding.provider = "onnx"
    cfg.embedding.model = model
    cfg.embedding.dimension = dimension
    return cfg


def _onnx_backend(db_path: Path, *, adopt: bool, dimension: int = 384) -> SqliteBackend:
    return SqliteBackend(
        StorageConfig(sqlite_path=db_path),
        dimension=dimension,
        embedding_provider="onnx",
        embedding_model=_E5,
        embedding_policy_fingerprint=_POLICY,
        embedding_max_sequence_tokens=512,
        adopt_unpopulated_embedding_stamp=adopt,
    )


async def _stamp_dim0(db_path: Path, *, chunks: int = 0) -> None:
    """Open once under ``provider=none`` — what a build under a rejected
    ``embedding`` section does — optionally writing BM25-only chunks."""
    storage = SqliteBackend(
        StorageConfig(sqlite_path=db_path),
        dimension=0,
        embedding_provider="none",
        embedding_model="",
        embedding_policy_fingerprint="none:v1",
        embedding_max_sequence_tokens=1024,
    )
    await storage.initialize()
    try:
        if chunks:
            await storage.upsert_chunks(
                [make_chunk(f"bm25 only {i}", embedding=[]) for i in range(chunks)]
            )
    finally:
        await storage.close()


def _meta(db_path: Path) -> dict[str, str]:
    db = sqlite3.connect(str(db_path))
    try:
        return dict(
            db.execute(
                "SELECT key, value FROM _memtomem_meta WHERE key LIKE 'embedding_%'"
            ).fetchall()
        )
    finally:
        db.close()


def _tables(db_path: Path) -> set[str]:
    db = sqlite3.connect(str(db_path))
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


_STAMP_DIM0 = {
    "embedding_dimension": "0",
    "embedding_provider": "none",
    "embedding_policy_fingerprint": "none:v1",
    "embedding_max_sequence_tokens": "1024",
}


class TestAdoption:
    async def test_the_issue_sequence_adopts_and_writes_vectors(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        assert _meta(db_path) == _STAMP_DIM0

        storage = _onnx_backend(db_path, adopt=True)
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is None
            assert _meta(db_path) == {
                "embedding_dimension": "384",
                "embedding_provider": "onnx",
                "embedding_model": _E5,
                "embedding_policy_fingerprint": _POLICY,
                "embedding_max_sequence_tokens": "512",
            }
            assert "chunks_vec" in _tables(db_path)
            vector = [1.0] + [0.0] * 383
            await storage.upsert_chunks([make_chunk("dense now", embedding=vector)])
            hits = await storage.dense_search(vector, top_k=1)
            assert [hit.chunk.content for hit in hits] == ["dense now"]
        finally:
            await storage.close()

    async def test_create_storage_opts_in(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        storage = create_storage(_config(db_path))
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is None
            assert _meta(db_path)["embedding_dimension"] == "384"
        finally:
            await storage.close()

    async def test_default_open_observes_the_stamp(self, tmp_path: Path) -> None:
        """Recovery and probe callers construct without the flag."""
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        with pytest.raises(EmbeddingDimensionMismatchError):
            await _onnx_backend(db_path, adopt=False).initialize()
        assert _meta(db_path) == _STAMP_DIM0

    async def test_a_chunk_blocks_adoption(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path, chunks=1)
        before = _meta(db_path)
        with pytest.raises(EmbeddingDimensionMismatchError):
            await _onnx_backend(db_path, adopt=True).initialize()
        assert _meta(db_path) == before
        assert "chunks_vec" not in _tables(db_path)

    async def test_a_store_emptied_after_indexing_is_adopted(self, tmp_path: Path) -> None:
        """Eligibility is current emptiness, not history. Once a dim=0 store's
        chunks are deleted it holds no vectors or chunks, so the reset still
        has nothing to destroy or re-index."""
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path, chunks=2)
        storage = SqliteBackend(
            StorageConfig(sqlite_path=db_path),
            dimension=0,
            embedding_provider="none",
            embedding_model="",
            embedding_policy_fingerprint="none:v1",
        )
        await storage.initialize()
        try:
            assert await storage.delete_by_source(Path("/tmp/test.md")) == 2
        finally:
            await storage.close()

        storage = _onnx_backend(db_path, adopt=True)
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is None
            assert _meta(db_path)["embedding_dimension"] == "384"
        finally:
            await storage.close()

    async def test_an_existing_vector_table_blocks_adoption(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        db = sqlite3.connect(str(db_path))
        db.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(db)
        db.execute("CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[8])")
        db.commit()
        db.close()
        with pytest.raises(EmbeddingDimensionMismatchError):
            await _onnx_backend(db_path, adopt=True).initialize()
        assert _meta(db_path)["embedding_dimension"] == "0"

    async def test_a_meta_only_store_is_not_adopted(self, tmp_path: Path) -> None:
        """No ``chunks`` table is not proof of emptiness — the degraded-mode
        fixture has exactly this shape and must keep reaching degraded mode."""
        db_path = tmp_path / "m.db"
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE _memtomem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.executemany(
            "INSERT INTO _memtomem_meta VALUES (?, ?)",
            [("embedding_dimension", "0"), ("embedding_provider", "none")],
        )
        db.commit()
        db.close()
        with pytest.raises(EmbeddingDimensionMismatchError):
            await _onnx_backend(db_path, adopt=True).initialize()
        assert _meta(db_path)["embedding_dimension"] == "0"

    async def test_a_fresh_store_is_stamped_by_create_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        storage = _onnx_backend(db_path, adopt=True)
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is None
            assert _meta(db_path)["embedding_dimension"] == "384"
        finally:
            await storage.close()

    async def test_an_adopted_store_is_not_adopted_again(self, tmp_path: Path) -> None:
        """A second configuration meets the first one's stamp, not dim 0."""
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        first = _onnx_backend(db_path, adopt=True)
        await first.initialize()
        await first.close()

        second = _onnx_backend(db_path, adopt=True, dimension=768)
        await second.initialize()
        try:
            mismatch = second.embedding_mismatch
            assert mismatch is not None and mismatch["dimension_mismatch"]
            assert _meta(db_path)["embedding_dimension"] == "384"
        finally:
            await second.close()

    async def test_a_newer_schema_is_refused_before_adoption(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        db = sqlite3.connect(str(db_path))
        db.execute(
            "UPDATE _memtomem_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        assert db.total_changes == 1
        db.commit()
        db.close()
        with pytest.raises(SchemaDowngradeError):
            await _onnx_backend(db_path, adopt=True).initialize()
        assert _meta(db_path) == _STAMP_DIM0

    async def test_a_failed_adoption_rolls_everything_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path)
        # A leftover vec0 shadow table the adoption drops — it must come back.
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE chunks_vec_info (key TEXT PRIMARY KEY, value ANY)")
        db.commit()
        db.close()

        real = MetaManager.reset_embedding_meta

        def fail_after_writing(self, *args, **kwargs):
            real(self, *args, **kwargs)
            raise RuntimeError("injected")

        monkeypatch.setattr(MetaManager, "reset_embedding_meta", fail_after_writing)
        with pytest.raises(Exception) as excinfo:
            await _onnx_backend(db_path, adopt=True).initialize()
        # initialize classifies startup failures; the injected one is the cause.
        assert str(excinfo.value.__cause__) == "injected"
        assert _meta(db_path) == _STAMP_DIM0
        assert "chunks_vec_info" in _tables(db_path)
        assert "chunks_vec" not in _tables(db_path)

    def test_eligibility_is_read_under_the_write_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunk committed while the adopter waits for the lock blocks it.

        Read before the lock, the store would look empty (the writer's insert
        is not yet committed) and the chunk would land in a store stamped
        with an identity its content never had.
        """
        db_path = tmp_path / "m.db"
        asyncio.run(_stamp_dim0(db_path))

        writer = sqlite3.connect(str(db_path), check_same_thread=False)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO chunks (id, content, content_hash, source_file, created_at, updated_at) "
            "VALUES ('c1', 'late', 'h1', '/tmp/late.md', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        hold_s = 0.5
        committed = threading.Event()
        releasers: list[threading.Thread] = []
        read_inside_transaction: list[bool] = []

        def release() -> None:
            time.sleep(hold_s)
            writer.commit()
            committed.set()

        # Start the countdown only once the adopter is about to take the
        # lock, so the writer cannot have committed before adoption began.
        real_idle = SqliteBackend._require_transaction_idle

        def idle_then_release(self, operation: str) -> None:
            real_idle(self, operation)
            if operation == "adopt_unpopulated_embedding_stamp":
                releasers.append(threading.Thread(target=release))
                releasers[-1].start()

        real_get_meta = MetaManager.get_meta

        def recording_get_meta(self, key: str):
            if key == "embedding_dimension" and releasers:
                read_inside_transaction.append(self._get_db().in_transaction)
            return real_get_meta(self, key)

        monkeypatch.setattr(SqliteBackend, "_require_transaction_idle", idle_then_release)
        monkeypatch.setattr(MetaManager, "get_meta", recording_get_meta)
        try:
            with pytest.raises(EmbeddingDimensionMismatchError):
                asyncio.run(_onnx_backend(db_path, adopt=True).initialize())
        finally:
            for thread in releasers:
                thread.join()
            if not committed.is_set():
                writer.rollback()
            writer.close()
        # Witnesses: the adoption path ran, and its eligibility read happened
        # inside the adopter's BEGIN IMMEDIATE — deterministic, unlike timing
        # the writer's commit. Whether the writer's chunk landed before or
        # during the wait, the locked read sees it.
        assert len(releasers) == 1
        assert read_inside_transaction[:1] == [True]
        assert _meta(db_path)["embedding_dimension"] == "0"


class TestRecordedIdentityIsNotCompleted:
    async def test_stored_none_is_reported_as_recorded(self, tmp_path: Path) -> None:
        """#2416's second defect: the backfill wrote the configured provider
        and model over a stored ``none`` whose model row was absent."""
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path, chunks=1)
        for _ in range(2):  # reopening must not complete the identity either
            storage = _onnx_backend(db_path, adopt=True)
            storage._strict_dim_check = False
            await storage.initialize()
            try:
                mismatch = storage.embedding_mismatch
                assert mismatch is not None
                assert (mismatch["stored"]["provider"], mismatch["stored"]["model"]) == (
                    "none",
                    "",
                )
                info = storage.stored_embedding_info
                assert (info["provider"], info["model"]) == ("none", "")
            finally:
                await storage.close()
            assert "embedding_model" not in _meta(db_path)
            assert _meta(db_path)["embedding_provider"] == "none"

    async def test_an_explicit_empty_model_is_reported_as_recorded(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        await _stamp_dim0(db_path, chunks=1)
        db = sqlite3.connect(str(db_path))
        db.execute("INSERT INTO _memtomem_meta VALUES ('embedding_model', '')")
        db.commit()
        db.close()
        storage = _onnx_backend(db_path, adopt=False)
        storage._strict_dim_check = False
        await storage.initialize()
        try:
            info = storage.stored_embedding_info
            assert (info["provider"], info["model"]) == ("none", "")
        finally:
            await storage.close()

    async def test_other_partial_stamps_keep_the_legacy_backfill(self, tmp_path: Path) -> None:
        """Only the ``none`` stamp is read as complete. A missing half beside
        a real identity no writer produces keeps being filled from the config:
        reporting it as ``""`` would hand revert-to-stored an identity
        ``create_embedder`` rejects after the live config was already changed."""
        db_path = tmp_path / "m.db"
        storage = _onnx_backend(db_path, adopt=False)
        await storage.initialize()
        await storage.close()
        db = sqlite3.connect(str(db_path))
        db.execute("DELETE FROM _memtomem_meta WHERE key='embedding_provider'")
        db.commit()
        db.close()

        storage = _onnx_backend(db_path, adopt=False)
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is None
        finally:
            await storage.close()
        assert _meta(db_path)["embedding_provider"] == "onnx"

    async def test_a_store_with_neither_key_is_still_backfilled(self, tmp_path: Path) -> None:
        db_path = tmp_path / "m.db"
        storage = _onnx_backend(db_path, adopt=False)
        await storage.initialize()
        await storage.close()
        db = sqlite3.connect(str(db_path))
        db.execute(
            "DELETE FROM _memtomem_meta WHERE key IN ('embedding_provider', 'embedding_model')"
        )
        db.commit()
        db.close()

        storage = _onnx_backend(db_path, adopt=False)
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is None
        finally:
            await storage.close()
        meta = _meta(db_path)
        assert (meta["embedding_provider"], meta["embedding_model"]) == ("onnx", _E5)


def test_embedding_reset_status_does_not_adopt(tmp_path: Path, monkeypatch) -> None:
    """``mm embedding-reset`` (default ``--mode status``) is a recovery tool:
    it must report the recorded stamp, not rewrite it."""
    from memtomem.cli import _bootstrap, cli

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    (home / ".memtomem").mkdir(parents=True)
    monkeypatch.chdir(home)
    set_home(monkeypatch, home)
    config_path = home / ".memtomem" / "config.json"
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", config_path)
    db_path = home / ".memtomem" / "memtomem.db"
    config_path.write_text(
        '{"storage": {"sqlite_path": "%s"}, '
        '"embedding": {"provider": "onnx", "model": "%s"}}' % (db_path.as_posix(), _E5),
        encoding="utf-8",
    )
    asyncio.run(_stamp_dim0(db_path))

    result = CliRunner().invoke(cli, ["embedding-reset"])

    assert result.exit_code == 0, result.output
    assert _meta(db_path) == _STAMP_DIM0
