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
#
# Note on ONNX progress shape: the implementation uses a SINGLE
# ``model.embed(texts)`` call (so fastembed's default internal batching
# stays in effect — a 250-text run = 1 ORT session.run instead of 4
# with our config.batch_size=64). Per-yield progress is then surfaced
# via a thread-safe callback. Earlier versions did Python-side chunking
# at ``config.batch_size``; benchmarking caught a +20% wall-clock
# regression vs the single-call path, so the implementation switched
# to streaming. As a result, ``on_progress`` fires per-text (throttled
# to ~20 ticks per file), NOT per-config.batch_size batch.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_onnx_streams_progress_per_yield():
    """5 texts → callback fires per yielded vector ending at (5, 5).

    Mocks ``_embed_sync`` so we can directly verify the per-yield
    contract without touching fastembed/ORT.
    """
    config = _onnx_config()
    embedder = OnnxEmbedder(config)

    def fake_sync(texts, on_progress=None):
        # Mimic the real _embed_sync's per-yield progress fire
        out = []
        total = len(texts)
        for t in texts:
            out.append([0.0, 0.1, 0.2])
            if on_progress is not None:
                on_progress(len(out), total)
        return out

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb = _record()
    result = await embedder.embed_texts(["a", "b", "c", "d", "e"], on_progress=cb)
    assert len(result) == 5
    _assert_progress_contract(calls, total=5, expected_calls=5)
    assert [d for d, _ in calls] == [1, 2, 3, 4, 5]


@pytest.mark.anyio
async def test_onnx_throttles_thread_hops_for_large_input():
    """A 200-text input must NOT fire 200 cross-thread hops — the
    ONNX path throttles to ~20 ticks per file (``total // 20``), with
    the final tick (``done == total``) always emitted so the UI's
    final-render contract holds.
    """
    config = _onnx_config()
    embedder = OnnxEmbedder(config)
    n = 200

    def fake_sync(texts, on_progress=None):
        out = []
        total = len(texts)
        for t in texts:
            out.append([0.0])
            if on_progress is not None:
                on_progress(len(out), total)
        return out

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb = _record()
    await embedder.embed_texts(["x"] * n, on_progress=cb)
    # Throttle step = max(1, n // 20) = 10 → ~20 ticks + final
    # (final-tick bypass guarantees one extra if the last-step boundary
    # doesn't land exactly on N).
    assert 15 <= len(calls) <= 25, (
        f"expected ~20 throttled ticks for n={n}, got {len(calls)}: {calls}"
    )
    # Final tick must equal n — UI render contract.
    assert calls[-1] == (n, n)
    # Monotonic
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)


@pytest.mark.anyio
async def test_onnx_no_progress_skips_callback_plumbing():
    """When ``on_progress`` is omitted, ``_embed_sync`` is called with
    ``on_progress=None`` — the fast path. Pin this so a future refactor
    that always wraps the callback (paying ``call_soon_threadsafe``
    cost on every yield) is caught.
    """
    config = _onnx_config()
    embedder = OnnxEmbedder(config)
    received_cb_arg: list = []

    def fake_sync(texts, on_progress=None):
        received_cb_arg.append(on_progress)
        return [[0.0] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    await embedder.embed_texts(["a", "b", "c"])
    assert received_cb_arg == [None], (
        "fast path must pass on_progress=None to _embed_sync to skip the per-yield callback wrap"
    )


@pytest.mark.anyio
async def test_onnx_progress_callback_exception_swallowed():
    config = _onnx_config()
    embedder = OnnxEmbedder(config)

    def fake_sync(texts, on_progress=None):
        out = []
        total = len(texts)
        for t in texts:
            out.append([0.0])
            if on_progress is not None:
                on_progress(len(out), total)
        return out

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]

    def bad_cb(done: int, total: int) -> None:
        raise RuntimeError("UI exploded")

    result = await embedder.embed_texts(["a", "b", "c"], on_progress=bad_cb)
    assert len(result) == 3


@pytest.mark.anyio
async def test_onnx_progress_kwarg_omitted_works():
    """Regression: existing ``embed_texts(texts)`` call sites still work."""
    config = _onnx_config()
    embedder = OnnxEmbedder(config)

    def fake_sync(texts, on_progress=None):
        return [[0.0] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    result = await embedder.embed_texts(["a", "b", "c"])
    assert len(result) == 3


@pytest.mark.anyio
async def test_onnx_numerical_parity_with_and_without_progress():
    """The progress-streaming code path must produce identical vectors
    to the no-progress fast path on the same input. Catches accidental
    drift if a future refactor reorders the per-yield iteration or
    introduces an inadvertent transformation in the callback wrap.

    Skipped without fastembed; runs in the ``golden-path (ONNX bge-m3)``
    CI job and any local env with fastembed installed.
    """
    pytest.importorskip("fastembed")
    pytest.importorskip("numpy")
    import numpy as np

    config = _onnx_config(model="all-MiniLM-L6-v2", dimension=384)
    emb_a = OnnxEmbedder(config)
    emb_b = OnnxEmbedder(config)
    texts = [
        "short one",
        "this is a slightly longer sentence to skew padding shape",
        "tiny",
        "another medium length sentence with some content to embed",
        "x",
        "a final reasonably long sentence ending the parity probe set",
    ]
    try:
        vecs_no_progress = await emb_a.embed_texts(texts)
        vecs_with_progress = await emb_b.embed_texts(texts, on_progress=lambda d, t: None)
    finally:
        await emb_a.close()
        await emb_b.close()

    assert len(vecs_no_progress) == len(vecs_with_progress) == len(texts)
    # Bit-exact: both paths call the same underlying model.embed() in
    # the same order; the only difference is whether each yield triggers
    # a callback. No floating-point reordering involved.
    assert np.allclose(
        np.array(vecs_no_progress),
        np.array(vecs_with_progress),
        rtol=1e-7,
        atol=1e-8,
    ), "fast path and progress path must produce identical embeddings"
