"""Tests for the cache-presence helper used by the model-readiness endpoint."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from memtomem.embedding.profiles import E5_REVISION, MINILM_REVISION
from memtomem.embedding.readiness import approx_size_mb, model_snapshot_present

MARKERS = ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json")

MINILM_ID = "sentence-transformers/all-MiniLM-L6-v2"
BGE_SMALL_ID = "BAAI/bge-small-en-v1.5"
E5_ID = "intfloat/multilingual-e5-small"


def _make_snapshot(
    cache_dir: Path,
    repo: str,
    model_files: tuple[str, ...],
    *,
    revision: str = "deadbeef",
    drop: str | None = None,
) -> Path:
    """Lay down a HuggingFace-style snapshot the way fastembed caches it.

    ``repo`` is the repository the files were downloaded from, which is not
    always the model id (#2585). ``drop`` omits one file so a test can assert
    the snapshot then reads as incomplete.
    """
    snap = cache_dir / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
    snap.mkdir(parents=True)
    for name in (*MARKERS, *model_files):
        if name == drop:
            continue
        (snap / name).parent.mkdir(parents=True, exist_ok=True)
        (snap / name).write_text("")
    return snap


# -- models fastembed downloads from a different repository (#2585) ----------


def test_minilm_is_found_under_the_qdrant_repo(tmp_path: Path) -> None:
    _make_snapshot(
        tmp_path, "qdrant/all-MiniLM-L6-v2-onnx", ("model.onnx",), revision=MINILM_REVISION
    )
    assert model_snapshot_present(tmp_path, MINILM_ID)


def test_minilm_under_its_model_id_is_not_what_the_loader_opens(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, MINILM_ID, ("model.onnx",), revision=MINILM_REVISION)
    assert not model_snapshot_present(tmp_path, MINILM_ID)


def test_minilm_needs_the_pinned_revision(tmp_path: Path) -> None:
    """A newer, unused revision alone must not read as cached."""
    _make_snapshot(tmp_path, "qdrant/all-MiniLM-L6-v2-onnx", ("model.onnx",), revision="0" * 40)
    assert not model_snapshot_present(tmp_path, MINILM_ID)


def test_minilm_case_variant_resolves_to_the_pin(tmp_path: Path) -> None:
    _make_snapshot(
        tmp_path, "qdrant/all-MiniLM-L6-v2-onnx", ("model.onnx",), revision=MINILM_REVISION
    )
    assert model_snapshot_present(tmp_path, MINILM_ID.upper())


def test_bge_small_is_found_under_the_qdrant_repo_with_its_model_file(tmp_path: Path) -> None:
    pytest.importorskip("fastembed")
    _make_snapshot(tmp_path, "qdrant/bge-small-en-v1.5-onnx-q", ("model_optimized.onnx",))
    assert model_snapshot_present(tmp_path, BGE_SMALL_ID)


def test_bge_small_under_its_model_id_is_not_what_the_loader_opens(tmp_path: Path) -> None:
    pytest.importorskip("fastembed")
    _make_snapshot(tmp_path, BGE_SMALL_ID, ("model.onnx",))
    assert not model_snapshot_present(tmp_path, BGE_SMALL_ID)


# -- pinned E5 ---------------------------------------------------------------


def test_e5_pinned_revision_is_present(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, E5_ID, ("onnx/model.onnx",), revision=E5_REVISION)
    assert model_snapshot_present(tmp_path, E5_ID)
    assert model_snapshot_present(tmp_path, "multilingual-e5-small")


def test_e5_other_revision_is_not_present(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, E5_ID, ("onnx/model.onnx",), revision="0" * 40)
    assert not model_snapshot_present(tmp_path, E5_ID)


# -- catalog files: custom-registered bge-m3 and a reranker -------------------


def test_bge_m3_needs_its_external_data_file(tmp_path: Path) -> None:
    """bge-m3 is registered by the embedder on first load; readiness must see
    its ``model.onnx_data`` requirement before that load has happened."""
    pytest.importorskip("fastembed")
    files = ("onnx/model.onnx", "onnx/model.onnx_data")
    _make_snapshot(tmp_path, "BAAI/bge-m3", files, drop="onnx/model.onnx_data")
    assert not model_snapshot_present(tmp_path, "BAAI/bge-m3")


def test_bge_m3_complete(tmp_path: Path) -> None:
    pytest.importorskip("fastembed")
    _make_snapshot(tmp_path, "BAAI/bge-m3", ("onnx/model.onnx", "onnx/model.onnx_data"))
    assert model_snapshot_present(tmp_path, "BAAI/bge-m3")


def test_reranker_is_resolved_from_the_cross_encoder_catalog(tmp_path: Path) -> None:
    fastembed = pytest.importorskip("fastembed")
    del fastembed
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = "Xenova/ms-marco-MiniLM-L-6-v2"
    desc = next(d for d in TextCrossEncoder.list_supported_models() if d["model"] == model)
    _make_snapshot(tmp_path, desc["sources"]["hf"], (desc["model_file"],))
    assert model_snapshot_present(tmp_path, model)


# -- completeness markers ----------------------------------------------------


@pytest.mark.parametrize("missing", [*MARKERS, "onnx/model.onnx"])
def test_missing_file_reads_as_incomplete(tmp_path: Path, missing: str) -> None:
    _make_snapshot(tmp_path, E5_ID, ("onnx/model.onnx",), revision=E5_REVISION, drop=missing)
    assert not model_snapshot_present(tmp_path, E5_ID)


# -- models the catalog cannot describe ---------------------------------------


@pytest.mark.parametrize("model_file", ["model.onnx", "onnx/model.onnx"])
def test_uncatalogued_model_accepts_either_layout(tmp_path: Path, model_file: str) -> None:
    _make_snapshot(tmp_path, "custom/model", (model_file,))
    assert model_snapshot_present(tmp_path, "custom/model")


def test_without_fastembed_falls_back_to_the_model_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def no_fastembed(name: str, *args: object, **kwargs: object) -> object:
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fastembed)
    _make_snapshot(tmp_path, BGE_SMALL_ID, ("model.onnx",))
    assert model_snapshot_present(tmp_path, BGE_SMALL_ID)


def test_empty_cache(tmp_path: Path) -> None:
    assert not model_snapshot_present(tmp_path, "BAAI/bge-m3")
    assert not model_snapshot_present(tmp_path, E5_ID)


def test_only_sibling_model(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, E5_ID, ("onnx/model.onnx",), revision=E5_REVISION)
    assert not model_snapshot_present(tmp_path, "custom/model")


def test_snapshots_dir_missing(tmp_path: Path) -> None:
    """Cache dir exists but has no ``snapshots/`` subdirectory."""
    (tmp_path / "models--custom--model").mkdir(parents=True)
    assert not model_snapshot_present(tmp_path, "custom/model")


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
