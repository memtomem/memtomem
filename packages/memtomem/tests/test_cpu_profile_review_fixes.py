"""Regressions for the PR #2383 review round.

Each test pins a behaviour the first cut of the E5 CPU profile got wrong. They
are grouped by the surface they defend rather than by finding, so a future edit
that re-breaks one fails next to the code it broke.
"""

import asyncio
import hashlib
import json
import platform
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from memtomem.config import EmbeddingConfig, IndexingConfig, Mem2MemConfig
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.errors import EmbeddingError
from memtomem.indexing import source_receipt
from memtomem.indexing.watcher import _STOP_SENTINEL, FileWatcher
from memtomem.storage.sqlite_helpers import norm_path


# ASCII inputs shorter than the cap skip the exact preflight entirely (a
# byte-level tokenizer cannot exceed one token per ASCII byte), so an oversized
# case has to be non-ASCII to reach the truncation branch at all.
_LONG_INPUT = "한국어 검증 " * 200


class _FakeEncoding:
    """Mirrors the tokenizers Encoding shape the preflight actually reads."""

    def __init__(self, *, truncated: bool) -> None:
        self.overflowing = [object()] if truncated else []


class _AlwaysTruncatingTokenizer:
    def __init__(self) -> None:
        self.truncation = {"max_length": 512}
        self.encode_calls: list[str] = []

    def enable_truncation(self, **kwargs) -> None:
        self.truncation.update(kwargs)

    def encode(self, text):
        self.encode_calls.append(text)
        return _FakeEncoding(truncated=True)

    def encode_batch(self, texts):
        return [_FakeEncoding(truncated=True) for _ in texts]


def _fake_embedding_model(vectors: list[list[float]]) -> MagicMock:
    import numpy as np

    model = MagicMock()
    model.embed.side_effect = lambda texts, batch_size=None: iter(
        np.array(v) for v in vectors[: len(texts)]
    )
    return model


# --------------------------------------------------------------------------
# Execution provider — FP32 must not lose an existing GPU install
# --------------------------------------------------------------------------


def _capture_text_embedding_kwargs(monkeypatch, config: EmbeddingConfig) -> dict:
    """Load a model with fastembed's entry point replaced; return its kwargs.

    Only ``fastembed.TextEmbedding`` is swapped, not the ``fastembed`` module:
    the quantized branch imports ``fastembed.common.model_description``, which
    a whole-module stub would break.
    """
    captured: dict = {}

    class _FakeTextEmbedding:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        @staticmethod
        def list_supported_models():
            return [{"model": "intfloat/multilingual-e5-small"}, {"model": "BAAI/bge-m3"}]

        @staticmethod
        def add_custom_model(**_kwargs):
            return None

    import memtomem.embedding.onnx as onnx_module

    monkeypatch.setattr("fastembed.TextEmbedding", _FakeTextEmbedding)
    monkeypatch.setattr(onnx_module, "_register_custom_models_if_needed", lambda: None)
    monkeypatch.setattr(onnx_module, "_verify_cpu_mem_arena", lambda *_a, **_k: None)
    monkeypatch.setattr(
        onnx_module,
        "_configure_tokenizer_limit",
        lambda _model, limit: (_AlwaysTruncatingTokenizer(), limit or 512),
    )
    OnnxEmbedder(config)._get_model()
    return captured


def test_fp32_leaves_provider_selection_to_fastembed(monkeypatch):
    """FP32 must not pin CPU.

    fastembed only reaches its ``cuda == Device.AUTO and cuda_available``
    branch when ``providers`` is None, so passing one unconditionally silently
    demotes an existing onnxruntime-gpu install to CPU with no warning.
    """
    captured = _capture_text_embedding_kwargs(
        monkeypatch, EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1024)
    )
    assert "providers" not in captured


def test_quantized_variant_pins_cpu_provider(monkeypatch, tmp_path):
    """INT8 artifacts are architecture-gated, so they must stay on CPU."""
    variant = "int8-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "int8-avx2"
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "model.onnx").write_bytes(b"weights")
    (artifact / "tokenizer.json").write_bytes(b"tokenizer")
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "model": "BAAI/bge-m3",
                "variant": variant,
                "precision": "int8",
                "files": {
                    "model.onnx": hashlib.sha256(b"weights").hexdigest(),
                    "tokenizer.json": hashlib.sha256(b"tokenizer").hexdigest(),
                },
            }
        )
    )
    captured = _capture_text_embedding_kwargs(
        monkeypatch,
        EmbeddingConfig(
            provider="onnx",
            model="bge-m3",
            dimension=1024,
            onnx_variant=variant,
            onnx_artifact_path=str(artifact),
        ),
    )
    assert captured["providers"] == ["CPUExecutionProvider"]


# --------------------------------------------------------------------------
# Truncation policy is directional
# --------------------------------------------------------------------------


def _oversized_embedder(monkeypatch) -> OnnxEmbedder:
    embedder = OnnxEmbedder(EmbeddingConfig(provider="onnx", model="bge-m3", dimension=1))
    model = _fake_embedding_model([[0.5]])
    embedder._model = model
    embedder._tokenizer = _AlwaysTruncatingTokenizer()
    embedder._active_max_sequence_tokens = 512
    monkeypatch.setattr(embedder, "_get_model", lambda: model)
    return embedder


def test_ingress_refuses_an_oversized_input(monkeypatch):
    embedder = _oversized_embedder(monkeypatch)
    with pytest.raises(EmbeddingError, match="exceeds 512 tokens"):
        embedder._embed_sync([_LONG_INPUT], refuse_truncation=True)


def test_query_side_truncates_instead_of_refusing(monkeypatch, caplog):
    """A query must degrade visibly, not disappear.

    Every query caller wraps embedding in a broad ``except`` that falls back to
    BM25-only, so raising here converts a truncation warning into an invisible
    loss of the dense leg.
    """
    embedder = _oversized_embedder(monkeypatch)
    with caplog.at_level("WARNING"):
        vectors = embedder._embed_sync([_LONG_INPUT], refuse_truncation=False)
    assert vectors == [[0.5]]
    assert "truncated 1 input" in caplog.text


def test_embed_sync_defaults_to_permitting_truncation(monkeypatch):
    """The refusal is opt-in, so a direct caller keeps the old behaviour."""
    embedder = _oversized_embedder(monkeypatch)
    assert embedder._embed_sync([_LONG_INPUT]) == [[0.5]]


@pytest.mark.anyio
async def test_embed_query_does_not_refuse_a_long_query(monkeypatch):
    embedder = _oversized_embedder(monkeypatch)
    monkeypatch.setattr("memtomem.embedding.onnx.is_e5", lambda _model: True)
    assert await embedder.embed_query(_LONG_INPUT) == [0.5]


@pytest.mark.anyio
async def test_embed_texts_still_refuses_a_long_passage(monkeypatch):
    embedder = _oversized_embedder(monkeypatch)
    monkeypatch.setattr("memtomem.embedding.onnx.is_e5", lambda _model: True)
    with pytest.raises(EmbeddingError):
        await embedder.embed_texts([_LONG_INPUT])


# --------------------------------------------------------------------------
# Profile un-apply keys on the model, not on the prefix's set-ness
# --------------------------------------------------------------------------


def test_env_set_prefix_does_not_keep_e5_budgets_under_bge(monkeypatch):
    """An operator-set prefix must not smuggle the E5 budget onto bge-m3.

    Gating the reset on ``chunk_input_prefix`` still looking generated let
    ``MEMTOMEM_INDEXING__CHUNK_INPUT_PREFIX="passage: "`` mark that one field
    *set*, skip the whole reset, and leave a 384/512 E5 budget in front of a
    1024-token BGE-M3 embedder whose checksum guard does not run for non-E5.
    """
    from memtomem.embedding.profiles import apply_e5_defaults

    monkeypatch.setenv("MEMTOMEM_INDEXING__CHUNK_INPUT_PREFIX", "passage: ")
    config = Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx"))
    assert config.indexing.hard_max_chunk_tokens == 384  # E5 chosen by provider alone

    config.embedding.model = "bge-m3"
    config.embedding.dimension = 1024
    apply_e5_defaults(config)

    baseline = IndexingConfig()
    assert config.indexing.hard_max_chunk_tokens == baseline.hard_max_chunk_tokens
    assert config.indexing.chunk_model_tokens == baseline.chunk_model_tokens
    assert config.indexing.chunk_tokenizer_path == baseline.chunk_tokenizer_path
    # The explicitly-set field is the one thing that must survive.
    assert config.indexing.chunk_input_prefix == "passage: "


# --------------------------------------------------------------------------
# Receipt state: one statement, and no chunks_vec on a BM25-only database
# --------------------------------------------------------------------------

_CHUNKS_DDL = """CREATE TABLE chunks (
    id TEXT PRIMARY KEY, source_file TEXT, content TEXT, content_hash TEXT,
    heading_hierarchy TEXT, retrieval_context TEXT, namespace TEXT, tags TEXT,
    valid_from_unix INTEGER, valid_to_unix INTEGER, scope TEXT, project_root TEXT,
    start_line INTEGER, end_line INTEGER, source_read_only INTEGER,
    source_span_hash TEXT, redaction_count INTEGER
)"""


def _receipt_db(*, with_vec: bool) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(_CHUNKS_DDL)
    db.execute("CREATE TABLE chunks_fts (rowid INTEGER PRIMARY KEY, body TEXT)")
    if with_vec:
        db.execute("CREATE TABLE chunks_vec (rowid INTEGER PRIMARY KEY, v BLOB)")
    return db


def _insert_chunk(db: sqlite3.Connection, source: Path, chunk_id: str) -> int:
    cur = db.execute(
        "INSERT INTO chunks (id, source_file, content) VALUES (?, ?, ?)",
        (chunk_id, norm_path(source), "body"),
    )
    return int(cur.lastrowid)


def test_state_never_touches_chunks_vec_when_embeddings_are_off(tmp_path):
    """A BM25-only database has no ``chunks_vec`` at all (issue #298).

    SQLite resolves table names at prepare time, so a sub-select that is merely
    parameterized off still raises "no such table".
    """
    source = tmp_path / "a.md"
    db = _receipt_db(with_vec=False)
    rowid = _insert_chunk(db, source, "c1")
    db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (?, 'body')", (rowid,))

    state = source_receipt.state(db, source, 0)
    assert state is not None and state[1] == 1


def test_state_refuses_a_row_missing_its_fts_shadow(tmp_path):
    source = tmp_path / "a.md"
    db = _receipt_db(with_vec=False)
    _insert_chunk(db, source, "c1")  # no chunks_fts row
    assert source_receipt.state(db, source, 0) is None


def test_state_refuses_a_row_missing_its_vector(tmp_path):
    source = tmp_path / "a.md"
    db = _receipt_db(with_vec=True)
    rowid = _insert_chunk(db, source, "c1")
    db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (?, 'body')", (rowid,))
    assert source_receipt.state(db, source, 384) is None

    db.execute("INSERT INTO chunks_vec (rowid, v) VALUES (?, x'00')", (rowid,))
    assert source_receipt.state(db, source, 384) is not None


def test_state_hash_excludes_the_completeness_column(tmp_path):
    """The validity flag must not become part of the pinned state."""
    source = tmp_path / "a.md"
    db = _receipt_db(with_vec=False)
    rowid = _insert_chunk(db, source, "c1")
    db.execute("INSERT INTO chunks_fts (rowid, body) VALUES (?, 'body')", (rowid,))
    state = source_receipt.state(db, source, 0)
    assert state is not None
    rows = db.execute(
        "SELECT rowid, id, content, content_hash, heading_hierarchy, retrieval_context,"
        " namespace, tags, valid_from_unix, valid_to_unix, scope, project_root, start_line,"
        " end_line, source_read_only, source_span_hash, redaction_count"
        " FROM chunks WHERE source_file=? ORDER BY id",
        (norm_path(source),),
    ).fetchall()
    assert state[0] == source_receipt.digest([list(row) for row in rows])


def test_content_hash_matches_plain_sha256():
    text = "본문 with unicode\n"
    assert source_receipt.content_hash(text) == hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------
# Artifact manifest identity is a configuration error, not an OSError
# --------------------------------------------------------------------------


def test_variant_identity_reports_a_missing_manifest_as_config_error(tmp_path):
    from memtomem.embedding.profiles import variant_identity

    with pytest.raises(ValueError, match="unreadable"):
        variant_identity(str(tmp_path / "absent"))


def test_variant_identity_resolves_like_artifact_manifest(tmp_path):
    """Both helpers must name the same file for one configured path."""
    from memtomem.embedding.profiles import variant_identity

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "manifest.json").write_text("{}")
    (tmp_path / "link").mkdir()
    indirect = tmp_path / "link" / ".." / "artifact"
    assert variant_identity(str(indirect)) == hashlib.sha256(b"{}").hexdigest()


# --------------------------------------------------------------------------
# Receipt policy carries code identity
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upgrading_memtomem_invalidates_a_receipt(bm25_only_components, monkeypatch):
    """A chunker fix in a new release must reach an already-indexed source.

    A receipt hit returns before ``chunk_content`` runs, so without code
    identity in the policy an unchanged source would keep its old boundaries
    forever — where every pre-receipt index pass re-chunked and let
    ``compute_diff`` notice.
    """
    comp, directory = bm25_only_components
    source = directory / "upgrade.md"
    source.write_text("# Upgrade\n\nAn ordinary nonsecret memory.\n")
    assert not (await comp.index_engine.index_file(source)).errors

    chunker = Mock(wraps=comp.index_engine.chunk_content)
    monkeypatch.setattr(comp.index_engine, "chunk_content", chunker)
    # Same content, same config — only the shipped version moves.
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_not_called()

    monkeypatch.setattr("memtomem.indexing.engine._memtomem_version", "99.99.99")
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_called_once()


# --------------------------------------------------------------------------
# Dedup probes embed on the document side
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_is_duplicate_embeds_through_the_document_side(bm25_only_components):
    """``is_duplicate`` compares against stored passages, so it must embed as one.

    With an asymmetric model ``embed_query`` prefixes "query: " and lands the
    probe away from the stored rows, pushing cosine under the hard-coded
    threshold so an exact re-add stops reading as a duplicate.
    """
    comp, _ = bm25_only_components
    engine = comp.index_engine
    used: list[str] = []

    async def _texts(texts, **_kwargs):
        used.append("embed_texts")
        return [[0.0] * max(engine._embedder.dimension, 1) for _ in texts]

    async def _query(_text):
        used.append("embed_query")
        return [0.0] * max(engine._embedder.dimension, 1)

    engine._embedder.embed_texts = _texts
    engine._embedder.embed_query = _query

    await engine.is_duplicate("some indexed text")

    assert used == ["embed_texts"]


# --------------------------------------------------------------------------
# Watcher backoff scheduling
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fully_backed_off_window_does_not_call_flush_at_all():
    """A window with nothing ready must not wake up to flush an empty set.

    A ready path is never held back by a backed-off one — it lands in ``ready``
    and is flushed on its own window. The cost being pinned here is the empty
    window: while the only pending path sits in a multi-second backoff, the loop
    used to fire once per debounce interval and hand ``_flush_batch`` an empty
    set each time, rewriting the window bookkeeping on every no-op.
    """
    watcher = FileWatcher(Mock(), IndexingConfig(), debounce_ms=20)
    blocked = Path("/blocked.md")
    flushed: list[set[Path]] = []

    async def _flush(pending):
        flushed.append(set(pending))
        return {blocked} & set(pending)  # blocked always needs a retry

    watcher._flush_batch = _flush  # type: ignore[method-assign]
    task = asyncio.create_task(watcher._process_events())
    try:
        await watcher._queue.put(blocked)
        # One real flush, then ~1.6-2.4s of backoff at attempt 1.
        await asyncio.wait_for(_until(lambda: len(flushed) >= 1), 2)
        assert watcher._retry_after.get(blocked, 0) > 0

        # Several debounce intervals of backoff must add no further calls.
        await asyncio.sleep(0.2)
        assert flushed == [{blocked}], f"woke up to flush nothing: {flushed}"
    finally:
        watcher._retry_after.clear()
        watcher._retry_attempts.clear()
        await watcher._queue.put(_STOP_SENTINEL)
        await asyncio.wait_for(task, 2)


@pytest.mark.anyio
async def test_a_ready_path_is_flushed_while_another_is_backed_off():
    """The backoff is per path, so it must not gate an unrelated edit."""
    watcher = FileWatcher(Mock(), IndexingConfig(), debounce_ms=20)
    blocked, fresh = Path("/blocked.md"), Path("/fresh.md")
    flushed: list[set[Path]] = []

    async def _flush(pending):
        flushed.append(set(pending))
        return {blocked} & set(pending)

    watcher._flush_batch = _flush  # type: ignore[method-assign]
    task = asyncio.create_task(watcher._process_events())
    try:
        await watcher._queue.put(blocked)
        await asyncio.wait_for(_until(lambda: len(flushed) >= 1), 2)
        await watcher._queue.put(fresh)
        await asyncio.wait_for(_until(lambda: any(fresh in batch for batch in flushed)), 1)
        # ``blocked`` is still backed off, so it must not ride along.
        assert not any(blocked in batch for batch in flushed[1:])
    finally:
        watcher._retry_after.clear()
        watcher._retry_attempts.clear()
        await watcher._queue.put(_STOP_SENTINEL)
        await asyncio.wait_for(task, 2)


@pytest.mark.anyio
async def test_retry_deadlines_do_not_survive_a_restart():
    """Backoff is per-run scheduling state, not a durable fact."""
    watcher = FileWatcher(Mock(), IndexingConfig(), debounce_ms=20)
    stale = Path("/stale.md")
    watcher._retry_after[stale] = 1e12
    watcher._retry_attempts[stale] = 5

    task = asyncio.create_task(watcher._process_events())
    try:
        await asyncio.wait_for(_until(lambda: not watcher._retry_after), 1)
        assert watcher._retry_attempts == {}
    finally:
        await watcher._queue.put(_STOP_SENTINEL)
        await asyncio.wait_for(task, 2)


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)
