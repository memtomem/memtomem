"""Per-batch ``on_progress`` callback contract for embedding providers.

Each provider must:
  * accept ``on_progress`` as a keyword-only argument
  * fire it after each natural unit of work (one batch)
  * deliver a monotonically non-decreasing ``done`` count
  * end at ``done == total``
  * never fail embedding when the callback raises
  * tolerate omission (existing callers without the kwarg still work)

These guarantees are what the SSE indexing stream relies on to forward
``chunk_progress`` events to the web UI mid-file (see
``IndexEngine.index_path_stream``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memtomem.config import EmbeddingConfig
from memtomem.embedding.noop import NoopEmbedder
from memtomem.embedding.ollama import OllamaEmbedder
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.embedding.openai import OpenAIEmbedder


def _ollama_config(**kw) -> EmbeddingConfig:
    base = dict(provider="ollama", model="nomic-embed-text", dimension=768)
    base.update(kw)
    return EmbeddingConfig(**base)


def _openai_config(**kw) -> EmbeddingConfig:
    base = dict(
        provider="openai",
        model="text-embedding-3-small",
        dimension=1536,
        api_key="sk-test",
    )
    base.update(kw)
    return EmbeddingConfig(**base)


def _onnx_config(**kw) -> EmbeddingConfig:
    base = dict(provider="onnx", model="all-MiniLM-L6-v2", dimension=3)
    base.update(kw)
    return EmbeddingConfig(**base)


def _record() -> tuple[list[tuple[int, int]], callable]:
    calls: list[tuple[int, int]] = []

    def cb(done: int, total: int) -> None:
        calls.append((done, total))

    return calls, cb


def _assert_progress_contract(calls: list[tuple[int, int]], total: int, expected_calls: int):
    assert len(calls) == expected_calls, f"expected {expected_calls} calls, got {calls}"
    # All totals match
    assert all(t == total for _, t in calls), calls
    # Monotonic non-decreasing done count
    dones = [d for d, _ in calls]
    assert dones == sorted(dones), f"done counts not monotonic: {dones}"
    # Final done equals total — without this the SSE stream's "(N/N)"
    # final-tick render never fires, leaving the user staring at "(192/250)"
    # right before the next file's name pops in.
    assert dones[-1] == total, f"final done={dones[-1]} != total={total}"


# ---------------------------------------------------------------------------
# Noop
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_noop_accepts_on_progress_kwarg():
    """Protocol conformance — Noop must accept the kwarg even though it
    never fires (Noop returns instantly and the engine skips embedding
    when ``dimension == 0``)."""
    embedder = NoopEmbedder()
    calls, cb = _record()
    result = await embedder.embed_texts(["a", "b", "c"], on_progress=cb)
    assert result == [[], [], []]
    assert calls == []  # Noop never fires the callback


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_openai_fires_progress_per_batch():
    """5 texts at batch_size=2 → 3 batches, 3 callbacks, ending at (5, 5)."""
    config = _openai_config(batch_size=2, max_concurrent_batches=1)
    embedder = OpenAIEmbedder(config)
    # Patch the per-batch worker so we control batch boundaries without
    # touching httpx at all. Returning a vector per text in the batch
    # mirrors the real /v1/embeddings response shape.
    embedder._embed_batch_with_retry = AsyncMock(
        side_effect=lambda batch: [[0.0] * 3 for _ in batch]
    )
    calls, cb = _record()
    result = await embedder.embed_texts(["a", "b", "c", "d", "e"], on_progress=cb)
    assert len(result) == 5
    _assert_progress_contract(calls, total=5, expected_calls=3)
    # Specifically: batch_size=2 with concurrency=1 forces sequential
    # completion, so done counts must be exactly 2, 4, 5.
    assert [d for d, _ in calls] == [2, 4, 5]


@pytest.mark.anyio
async def test_openai_progress_kwarg_omitted_works():
    """Existing callers without the kwarg still embed normally."""
    config = _openai_config(batch_size=4)
    embedder = OpenAIEmbedder(config)
    embedder._embed_batch_with_retry = AsyncMock(side_effect=lambda batch: [[0.0] for _ in batch])
    result = await embedder.embed_texts(["a", "b"])
    assert len(result) == 2


@pytest.mark.anyio
async def test_openai_progress_callback_exception_swallowed():
    """A buggy callback must not break embedding — best-effort contract."""
    config = _openai_config(batch_size=2, max_concurrent_batches=1)
    embedder = OpenAIEmbedder(config)
    embedder._embed_batch_with_retry = AsyncMock(side_effect=lambda batch: [[0.0] for _ in batch])

    def bad_cb(done: int, total: int) -> None:
        raise RuntimeError("UI exploded")

    result = await embedder.embed_texts(["a", "b", "c"], on_progress=bad_cb)
    assert len(result) == 3  # embedding completed despite raising callback


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ollama_fires_progress_per_batch():
    config = _ollama_config(batch_size=2, max_concurrent_batches=1)
    embedder = OllamaEmbedder(config)
    embedder._embed_batch_with_retry = AsyncMock(
        side_effect=lambda batch: [[0.0] * 3 for _ in batch]
    )
    calls, cb = _record()
    result = await embedder.embed_texts(["a", "b", "c", "d", "e"], on_progress=cb)
    assert len(result) == 5
    _assert_progress_contract(calls, total=5, expected_calls=3)
    assert [d for d, _ in calls] == [2, 4, 5]


@pytest.mark.anyio
async def test_ollama_progress_callback_exception_swallowed():
    config = _ollama_config(batch_size=2, max_concurrent_batches=1)
    embedder = OllamaEmbedder(config)
    embedder._embed_batch_with_retry = AsyncMock(side_effect=lambda batch: [[0.0] for _ in batch])

    def bad_cb(done: int, total: int) -> None:
        raise RuntimeError("UI exploded")

    result = await embedder.embed_texts(["a", "b", "c"], on_progress=bad_cb)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# ONNX (mocked _embed_sync; no fastembed dependency at test time)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_onnx_fires_progress_per_batch():
    """5 texts at batch_size=2 → 3 batches via the new internal loop."""
    config = _onnx_config(batch_size=2)
    embedder = OnnxEmbedder(config)

    def fake_sync(texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.1, 0.2] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb = _record()
    result = await embedder.embed_texts(["a", "b", "c", "d", "e"], on_progress=cb)
    assert len(result) == 5
    # ONNX runs sequentially (one to_thread per batch), so done counts
    # are exactly 2, 4, 5 — no concurrent reordering possible.
    _assert_progress_contract(calls, total=5, expected_calls=3)
    assert [d for d, _ in calls] == [2, 4, 5]


@pytest.mark.anyio
async def test_onnx_single_batch_still_fires_once():
    """When all texts fit in one batch, callback fires once with (N, N)."""
    config = _onnx_config(batch_size=64)
    embedder = OnnxEmbedder(config)

    def fake_sync(texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb = _record()
    await embedder.embed_texts(["a", "b", "c"], on_progress=cb)
    assert calls == [(3, 3)]


@pytest.mark.anyio
async def test_onnx_progress_callback_exception_swallowed():
    config = _onnx_config(batch_size=2)
    embedder = OnnxEmbedder(config)

    def fake_sync(texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]

    def bad_cb(done: int, total: int) -> None:
        raise RuntimeError("UI exploded")

    result = await embedder.embed_texts(["a", "b", "c"], on_progress=bad_cb)
    assert len(result) == 3


@pytest.mark.anyio
async def test_onnx_progress_kwarg_omitted_works():
    """Regression: existing ``embed_texts(texts)`` call sites still work."""
    config = _onnx_config(batch_size=2)
    embedder = OnnxEmbedder(config)

    def fake_sync(texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    result = await embedder.embed_texts(["a", "b", "c"])
    assert len(result) == 3
