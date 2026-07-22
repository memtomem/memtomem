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

import asyncio
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
    # Most progress tests replace ``_embed_sync`` and do not construct a real
    # FastEmbed tokenizer. Keep the safety cap disabled in this helper; the
    # provider tests exercise the production default and tokenizer contract.
    base = dict(
        provider="onnx",
        model="all-MiniLM-L6-v2",
        dimension=3,
        max_sequence_tokens=0,
    )
    base.update(kw)
    return EmbeddingConfig(**base)


def _record() -> tuple[list[tuple[int, int]], callable]:
    calls: list[tuple[int, int]] = []

    def cb(done: int, total: int) -> None:
        calls.append((done, total))

    return calls, cb


def _record_with_done_event() -> tuple[list[tuple[int, int]], callable, asyncio.Event]:
    """Like :func:`_record`, but also returns an ``asyncio.Event`` set
    when a callback fires with ``done == total`` — i.e. the final tick.

    Used by the ONNX tests to wait for trailing
    ``loop.call_soon_threadsafe`` callbacks to drain off the loop's
    ready queue before asserting on call count. ``asyncio.to_thread``
    can resume its awaiter before the worker thread's queued
    callbacks dispatch — a single ``await asyncio.sleep(0)`` yields
    only one loop iteration and the macOS GH runner under Python 3.14
    has been observed to need more (#663). Awaiting an Event set by
    the final callback is robust regardless of how many iterations
    the loop needs to fully drain the FIFO queue: once the
    ``done == total`` callback runs we know every earlier callback
    has already run (FIFO ordering of ``call_soon_threadsafe``).
    """
    calls: list[tuple[int, int]] = []
    done_event = asyncio.Event()

    def cb(done: int, total: int) -> None:
        calls.append((done, total))
        if done == total:
            done_event.set()

    return calls, cb, done_event


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
# ONNX (mostly mocked _embed_sync; the two numerical-parity tests below use
# the real model and skip without fastembed)
#
# Note on ONNX progress shape: ``embed_texts`` submits the inference in
# sub-batches (``_SUBBATCH_TARGET_TEXTS`` rounded to a whole multiple of
# ``onnx_batch_size``) so a queued ``embed_query`` gets the single worker
# between slices — the #1804 search-priority lane. Within each slice,
# ``_embed_sync`` makes one ``model.embed(texts, batch_size=...)`` call:
# FastEmbed owns the internal streaming loop while memtomem caps each ORT
# batch at the memory-safe ONNX setting, and per-yield progress is surfaced
# via a thread-safe callback whose ``done`` count is translated to the
# file-global value across slices. History: the very first version did
# Python-side chunking at ``config.batch_size`` (=64), which benchmarking
# caught as a +20% wall-clock regression against the then-default single
# fused call — the cost was the ORT ``session.run`` count (#653). #1809
# later capped the run size everywhere (``onnx_batch_size``), so today's
# multiple-of-batch slicing adds only the per-call ``model.embed`` re-entry
# (~+1.6%, #653 variant B vs C) and keeps batch boundaries — and therefore
# float results — identical. ``on_progress`` still fires per-text (throttled
# to ~20 ticks per file), NOT per batch or per slice.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_onnx_streams_progress_per_yield():
    """5 texts → callback fires per yielded vector ending at (5, 5).

    Mocks ``_embed_sync`` so we can directly verify the per-yield
    contract without touching fastembed/ORT.
    """
    config = _onnx_config()
    embedder = OnnxEmbedder(config)

    def fake_sync(
        texts, on_progress=None, source_path=None, chunk_indices=None, *, batch_size=None
    ):
        # Mimic the real _embed_sync's per-yield progress fire
        out = []
        total = len(texts)
        for t in texts:
            out.append([0.0, 0.1, 0.2])
            if on_progress is not None:
                on_progress(len(out), total)
        return out

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb, done_event = _record_with_done_event()
    result = await embedder.embed_texts(["a", "b", "c", "d", "e"], on_progress=cb)
    assert len(result) == 5
    # Flush ``call_soon_threadsafe`` callbacks the worker scheduled but
    # the loop hasn't dequeued yet by the time ``to_thread`` resumed.
    # Awaiting the final-tick Event drains the loop's FIFO queue
    # however many iterations it takes — macOS Py3.14 has been
    # observed to need more than a single ``sleep(0)`` yield (#663).
    # Production behavior is fire-and-forget; tightening that contract
    # would be a separate PR.
    await asyncio.wait_for(done_event.wait(), timeout=5.0)
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

    def fake_sync(
        texts, on_progress=None, source_path=None, chunk_indices=None, *, batch_size=None
    ):
        out = []
        total = len(texts)
        for t in texts:
            out.append([0.0])
            if on_progress is not None:
                on_progress(len(out), total)
        return out

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb, done_event = _record_with_done_event()
    await embedder.embed_texts(["x"] * n, on_progress=cb)
    # Wait for the final-tick (``done == n``) callback to drain off the
    # loop's queue — same race as test_onnx_streams_progress_per_yield,
    # but more visible at n=200 because the throttled run has more
    # in-flight ``call_soon_threadsafe`` items pending when ``to_thread``
    # resumes (#663).
    await asyncio.wait_for(done_event.wait(), timeout=5.0)
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

    def fake_sync(
        texts, on_progress=None, source_path=None, chunk_indices=None, *, batch_size=None
    ):
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

    def fake_sync(
        texts, on_progress=None, source_path=None, chunk_indices=None, *, batch_size=None
    ):
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

    def fake_sync(
        texts, on_progress=None, source_path=None, chunk_indices=None, *, batch_size=None
    ):
        return [[0.0] for _ in texts]

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    result = await embedder.embed_texts(["a", "b", "c"])
    assert len(result) == 3


@pytest.mark.anyio
async def test_onnx_progress_spans_subbatches_globally():
    """#1804: with the bulk call split into sub-batches, ``on_progress``
    still reports file-global counts — monotonic across slice boundaries,
    ending with the unthrottled final ``(n, n)`` tick."""
    config = _onnx_config()
    embedder = OnnxEmbedder(config)
    embedder._subbatch_for = lambda bs: 2  # type: ignore[method-assign]

    def fake_sync(
        texts, on_progress=None, source_path=None, chunk_indices=None, *, batch_size=None
    ):
        out = []
        total = len(texts)
        for t in texts:
            out.append([0.0])
            if on_progress is not None:
                on_progress(len(out), total)
        return out

    embedder._embed_sync = fake_sync  # type: ignore[method-assign]
    calls, cb, done_event = _record_with_done_event()
    result = await embedder.embed_texts(["a", "b", "c", "d", "e"], on_progress=cb)
    assert len(result) == 5
    await asyncio.wait_for(done_event.wait(), timeout=5.0)
    _assert_progress_contract(calls, total=5, expected_calls=5)
    # Global, monotonic ``done`` across the 2+2+1 slices — never slice-local.
    assert [d for d, _ in calls] == [1, 2, 3, 4, 5]
    assert calls[-1] == (5, 5)


@pytest.mark.anyio
async def test_onnx_numerical_parity_split_vs_unsplit():
    """#1804: sub-batch submission must not change embeddings. Slice widths
    are whole multiples of ``onnx_batch_size``, so ORT batch boundaries —
    and therefore padding shapes and float results — are identical whether
    a call is split or not. Regular test matrix; skipped without fastembed.
    """
    pytest.importorskip("fastembed")
    pytest.importorskip("numpy")
    import numpy as np

    config = _onnx_config(model="all-MiniLM-L6-v2", dimension=384)
    emb_unsplit = OnnxEmbedder(config)
    emb_split = OnnxEmbedder(config)
    # One slice for the whole input vs the smallest boundary-preserving
    # slice (= one onnx_batch_size per executor task).
    emb_unsplit._subbatch_for = lambda bs: 4096  # type: ignore[method-assign]
    emb_split._subbatch_for = lambda bs: bs  # type: ignore[method-assign]

    def _spy_widths(embedder):
        widths: list[int] = []
        real = embedder._embed_sync

        def wrapper(texts, *args, **kwargs):
            widths.append(len(texts))
            return real(texts, *args, **kwargs)

        embedder._embed_sync = wrapper  # type: ignore[method-assign]
        return widths

    unsplit_widths = _spy_widths(emb_unsplit)
    split_widths = _spy_widths(emb_split)
    # Mixed lengths crossing several slice boundaries, including an input
    # far past the model's 256-token sequence limit (truncation path).
    texts = [
        f"sentence number {i} " + ("with deliberately repeated padding-skew content " * (i % 7))
        for i in range(43)
    ]
    texts.append("near max sequence probe " + ("token filler well beyond the model limit " * 80))
    try:
        vecs_unsplit = await emb_unsplit.embed_texts(texts)
        vecs_split = await emb_split.embed_texts(texts)
    finally:
        await emb_unsplit.close()
        await emb_split.close()

    # The forced slicing must actually have fired, or this test compares
    # identical execution against itself and proves nothing.
    assert unsplit_widths == [44]
    assert split_widths == [8, 8, 8, 8, 8, 4]
    assert len(vecs_unsplit) == len(vecs_split) == len(texts)
    assert np.allclose(
        np.array(vecs_unsplit),
        np.array(vecs_split),
        rtol=1e-7,
        atol=1e-8,
    ), "split and unsplit submission must produce identical embeddings"


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
