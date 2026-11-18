"""``put`` latency, the measurement ADR-0042 asks for. Numbers go to ``docs/protocol.md``."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore

N = 50


async def _measure(store: MemtomemPipelineStore) -> float:
    start = time.perf_counter()
    for n in range(N):
        await store.aput(("bench",), f"k{n}", {"text": f"note {n} about apples and travel plans"})
    return (time.perf_counter() - start) * 1000 / N


async def test_put_latency_with_no_embedder(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    try:
        mean_ms = await _measure(store)
    finally:
        await store.aclose()
    print(f"\nput latency, provider=none: {mean_ms:.1f} ms mean over {N}")
    assert mean_ms < 500, mean_ms


@pytest.mark.skipif(
    not os.environ.get("LANGCHAIN_MEMTOMEM_E5"),
    reason="downloads the E5 model; set LANGCHAIN_MEMTOMEM_E5=1",
)
async def test_put_latency_with_e5(root: Path) -> None:
    store = await MemtomemPipelineStore.open(
        root, config={"embedding": {"provider": "onnx", "model": "multilingual-e5-small"}}
    )
    try:
        mean_ms = await _measure(store)
    finally:
        await store.aclose()
    print(f"\nput latency, E5 profile: {mean_ms:.1f} ms mean over {N}")
