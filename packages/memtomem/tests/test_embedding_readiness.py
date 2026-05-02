"""Tests for the cache-presence helper used by the model-readiness endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.embedding.readiness import approx_size_mb, model_snapshot_present


def _make_snapshot(
    cache_dir: Path,
    model_id: str,
    *,
    layout: str = "flat",
    drop: str | None = None,
) -> Path:
    """Lay down a fastembed-style snapshot directory for ``model_id``.

    ``layout="flat"`` puts ``model.onnx`` directly under the snapshot dir;
    ``layout="nested"`` puts it under ``onnx/model.onnx`` (the form
    ``BAAI/bge-m3`` and the multilingual reranker use). ``drop`` lets a
    test omit one of the marker files to assert it then reads as
    incomplete.
    """
    sanitized = "models--" + model_id.replace("/", "--")
    snap = cache_dir / sanitized / "snapshots" / "deadbeef"
    snap.mkdir(parents=True)

    files = ["config.json", "tokenizer.json"]
    if layout == "flat":
        files.append("model.onnx")
    else:
        (snap / "onnx").mkdir()
        if drop != "model.onnx":
            (snap / "onnx" / "model.onnx").write_text("")
    for f in files:
        if f == drop:
            continue
        (snap / f).write_text("")
    return snap


def test_present_flat(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "BAAI/bge-small-en-v1.5", layout="flat")
    assert model_snapshot_present(tmp_path, "BAAI/bge-small-en-v1.5")


def test_present_nested(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "BAAI/bge-m3", layout="nested")
    assert model_snapshot_present(tmp_path, "BAAI/bge-m3")


@pytest.mark.parametrize("missing", ["config.json", "tokenizer.json", "model.onnx"])
def test_missing_marker_flat(tmp_path: Path, missing: str) -> None:
    _make_snapshot(tmp_path, "BAAI/bge-small-en-v1.5", layout="flat", drop=missing)
    assert not model_snapshot_present(tmp_path, "BAAI/bge-small-en-v1.5")


def test_missing_model_onnx_nested(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "BAAI/bge-m3", layout="nested", drop="model.onnx")
    assert not model_snapshot_present(tmp_path, "BAAI/bge-m3")


def test_empty_cache(tmp_path: Path) -> None:
    assert not model_snapshot_present(tmp_path, "BAAI/bge-m3")


def test_only_sibling_model(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "BAAI/bge-small-en-v1.5", layout="flat")
    assert not model_snapshot_present(tmp_path, "BAAI/bge-m3")


def test_snapshots_dir_missing(tmp_path: Path) -> None:
    """Cache dir exists but has no ``snapshots/`` subdirectory."""
    (tmp_path / "models--BAAI--bge-m3").mkdir(parents=True)
    assert not model_snapshot_present(tmp_path, "BAAI/bge-m3")


def test_approx_size_known() -> None:
    # Sizes match fastembed's own ``list_supported_models()`` /
    # ``size_in_GB`` field (and ``size_in_gb=2.3`` declared on bge-m3's
    # ``add_custom_model`` call). Bumping these here without bumping
    # ``embedding/aliases.py`` will fail the assertion.
    assert approx_size_mb("BAAI/bge-m3") == 2300
    assert approx_size_mb("bge-m3") == 2300  # short-name alias also works
    assert approx_size_mb("jinaai/jina-reranker-v2-base-multilingual") == 1110


def test_approx_size_unknown() -> None:
    assert approx_size_mb("custom/unknown-model") is None
