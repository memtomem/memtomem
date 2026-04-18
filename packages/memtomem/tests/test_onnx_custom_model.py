"""Tests for custom-model registration in the ONNX embedder.

fastembed 0.8.0's built-in ``TextEmbedding`` catalog no longer ships
``BAAI/bge-m3`` (the embedding classes were split per model family, and none
of them currently host bge-m3). ``_register_custom_models_if_needed`` in
``memtomem.embedding.onnx`` re-registers the model from its official HF ONNX
export so existing installs keep working.

These tests only verify the *registration* is wired up correctly — they do
not download or load the model (the full ONNX export is ~2.3 GB; an
Ollama-gated end-to-end test covers actual inference).
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "fastembed",
    reason="fastembed not installed — install with `pip install memtomem[onnx]`",
)


def _registered_model_ids() -> set[str]:
    from fastembed import TextEmbedding  # type: ignore[import-untyped]

    return {m.get("model") for m in TextEmbedding.list_supported_models()}


def test_registration_adds_bge_m3() -> None:
    """bge-m3 must appear in the supported-model list after registration."""
    from memtomem.embedding.onnx import _register_custom_models_if_needed

    _register_custom_models_if_needed()
    assert "BAAI/bge-m3" in _registered_model_ids()


def test_registration_is_idempotent() -> None:
    """Calling the helper twice must not raise (guards against fastembed's
    strict ValueError on duplicate ``add_custom_model``)."""
    from memtomem.embedding.onnx import _register_custom_models_if_needed

    _register_custom_models_if_needed()
    _register_custom_models_if_needed()  # must not raise
    assert "BAAI/bge-m3" in _registered_model_ids()


def test_bge_m3_registration_has_expected_shape() -> None:
    """Sanity-check the registered model metadata — dim 1024 CLS pooling."""
    from fastembed import TextEmbedding  # type: ignore[import-untyped]

    from memtomem.embedding.onnx import _register_custom_models_if_needed

    _register_custom_models_if_needed()
    entry = next(
        m for m in TextEmbedding.list_supported_models() if m.get("model") == "BAAI/bge-m3"
    )
    assert entry.get("dim") == 1024
    assert entry.get("model_file") == "onnx/model.onnx"
