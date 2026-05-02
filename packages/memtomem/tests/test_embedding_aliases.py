"""Tests for the shared model alias / size table.

Locks the contract three independent surfaces depend on:

* ``OnnxEmbedder._get_model`` — short-name → fastembed-id resolution.
* ``GET /api/system/model-readiness`` — banner annotation lookup.
* ``cli init`` wizard — size text shown to the user.

Adding a new short alias means a new row in ``ONNX_EMBEDDER_MODELS``
plus reading the wizard text below to confirm the format helper still
renders sanely.
"""

from __future__ import annotations

import pytest

from memtomem.embedding.aliases import (
    FASTEMBED_RERANKER_SIZES,
    ONNX_EMBEDDER_MODELS,
    approx_size_mb,
    format_size,
    resolve_embedder_id,
)


def test_resolve_short_alias() -> None:
    assert resolve_embedder_id("bge-m3") == "BAAI/bge-m3"
    assert resolve_embedder_id("bge-small-en-v1.5") == "BAAI/bge-small-en-v1.5"
    assert resolve_embedder_id("all-MiniLM-L6-v2") == "sentence-transformers/all-MiniLM-L6-v2"


def test_resolve_passthrough_on_unknown() -> None:
    """Raw fastembed ids that aren't a documented short alias pass through."""
    raw = "nomic-ai/nomic-embed-text-v1.5"
    assert resolve_embedder_id(raw) == raw


def test_approx_size_by_short_alias() -> None:
    for short, (_full, _dim, expected_mb) in ONNX_EMBEDDER_MODELS.items():
        assert approx_size_mb(short) == expected_mb, short


def test_approx_size_by_full_id() -> None:
    for _short, (full, _dim, expected_mb) in ONNX_EMBEDDER_MODELS.items():
        assert approx_size_mb(full) == expected_mb, full


def test_approx_size_for_reranker_ids() -> None:
    for full, expected_mb in FASTEMBED_RERANKER_SIZES.items():
        assert approx_size_mb(full) == expected_mb, full


def test_approx_size_for_extra_embedder_ids() -> None:
    """Lookup by raw fastembed id covers models without a short alias."""
    assert approx_size_mb("nomic-ai/nomic-embed-text-v1.5") == 520


def test_approx_size_unknown_returns_none() -> None:
    assert approx_size_mb("custom/unknown-model") is None


def test_resolve_embedder_id_matches_legacy_resolver() -> None:
    """Drift guard: every short alias must agree with the legacy ``_resolve_model``.

    Until #696 the alias map lived inside ``embedding/onnx.py`` and was
    duplicated by a hand-rolled ``_resolve_fastembed_model_id`` in the
    web routes module. Both sites now read from this shared module.
    Snapshot the contract so a future refactor that re-introduces a
    private duplicate fails this test instead of silently drifting.
    """
    expected = {
        "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
        "bge-small-en-v1.5": "BAAI/bge-small-en-v1.5",
        "bge-m3": "BAAI/bge-m3",
    }
    for short, full in expected.items():
        assert resolve_embedder_id(short) == full


@pytest.mark.parametrize(
    "mb,expected",
    [
        (67, "67 MB"),
        (90, "90 MB"),
        (999, "999 MB"),
        (1000, "1.0 GB"),
        (1110, "1.1 GB"),
        (2300, "2.3 GB"),
    ],
)
def test_format_size(mb: int, expected: str) -> None:
    assert format_size(mb) == expected
