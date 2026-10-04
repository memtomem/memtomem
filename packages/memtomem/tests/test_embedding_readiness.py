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


def _catalog_hf_repo(model_id: str) -> str:
    """The ``sources.hf`` repository fastembed's catalog downloads ``model_id`` from.

    Read from the installed catalog rather than written out, because its
    casing is fastembed's to change: 0.8.1 renamed
    ``qdrant/bge-small-en-v1.5-onnx-q`` to ``Qdrant/bge-small-en-v1.5-onnx-Q``,
    and on a case-sensitive filesystem the old spelling is another directory.
    """
    from fastembed import TextEmbedding  # type: ignore[import-untyped]

    for desc in TextEmbedding.list_supported_models():
        if desc["model"] == model_id:
            return str(desc["sources"]["hf"])
    raise AssertionError(f"{model_id} is not in fastembed's catalog")


def test_bge_small_is_found_under_the_qdrant_repo_with_its_model_file(tmp_path: Path) -> None:
    pytest.importorskip("fastembed")
    repo = _catalog_hf_repo(BGE_SMALL_ID)
    assert repo.lower() == "qdrant/bge-small-en-v1.5-onnx-q"
    _make_snapshot(tmp_path, repo, ("model_optimized.onnx",))
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


GTE_RERANKER = "onnx-community/gte-multilingual-reranker-base"


@pytest.fixture
def empty_custom_rerankers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start with no custom rerankers, so only the probe can register gte."""
    pytest.importorskip("fastembed")
    from fastembed.rerank.cross_encoder.custom_text_cross_encoder import (
        CustomTextCrossEncoder,
    )

    monkeypatch.setattr(CustomTextCrossEncoder, "SUPPORTED_MODELS", [])


@pytest.mark.usefixtures("empty_custom_rerankers")
def test_memtomem_registered_reranker_is_present_with_its_int8_file(tmp_path: Path) -> None:
    """The probe registers gte itself and looks for the INT8 file it loads (#2650)."""
    _make_snapshot(tmp_path, GTE_RERANKER, ("onnx/model_int8.onnx",))
    assert model_snapshot_present(tmp_path, GTE_RERANKER)


@pytest.mark.usefixtures("empty_custom_rerankers")
def test_memtomem_registered_reranker_is_absent_without_its_int8_file(tmp_path: Path) -> None:
    """An fp32 ``onnx/model.onnx`` is not what the reranker loads."""
    _make_snapshot(tmp_path, GTE_RERANKER, ("onnx/model.onnx",))
    assert not model_snapshot_present(tmp_path, GTE_RERANKER)


# -- fastembed's GCS tarball fallback (#2594) ---------------------------------

BGE_BASE_ID = "BAAI/bge-base-en-v1.5"


@pytest.fixture
def hf_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """fastembed reads the tarball directory straight away only offline;
    online it downloads from Hugging Face first."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


@pytest.fixture
def hf_online(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)


def _make_gcs_dir(
    cache_dir: Path, name: str, model_files: tuple[str, ...], *, drop: str | None = None
) -> Path:
    """Lay down a model the way fastembed unpacks its GCS tarball: flat, in
    ``cache_dir/<name>``, with no ``models--`` or ``snapshots`` level."""
    model_dir = cache_dir / name
    model_dir.mkdir(parents=True)
    for file_name in (*MARKERS, *model_files):
        if file_name == drop:
            continue
        (model_dir / file_name).parent.mkdir(parents=True, exist_ok=True)
        (model_dir / file_name).write_text("")
    return model_dir


def _catalog_entry(model: str) -> dict[str, object]:
    fastembed = pytest.importorskip("fastembed")
    entry: dict[str, object] = next(
        d for d in fastembed.TextEmbedding.list_supported_models() if d["model"] == model
    )
    return entry


def _gcs_layout(model: str) -> tuple[str, tuple[str, ...]]:
    """The directory and files the catalog says the tarball unpacks to."""
    entry = _catalog_entry(model)
    sources = entry["sources"]
    assert isinstance(sources, dict) and sources.get("url"), f"{model} has no GCS fallback"
    prefix = "fast-" if sources.get("_deprecated_tar_struct") else ""
    extra = tuple(entry.get("additional_files") or ())  # type: ignore[call-overload]
    return prefix + model.split("/")[-1], (str(entry["model_file"]), *extra)


def test_model_unpacked_from_the_gcs_tarball_is_present(tmp_path: Path, hf_offline: None) -> None:
    name, files = _gcs_layout(BGE_BASE_ID)
    assert name == "fast-bge-base-en-v1.5"
    _make_gcs_dir(tmp_path, name, files)
    assert model_snapshot_present(tmp_path, BGE_BASE_ID)


def test_gcs_dir_is_found_for_a_lowercased_id(tmp_path: Path, hf_offline: None) -> None:
    name, files = _gcs_layout(BGE_BASE_ID)
    _make_gcs_dir(tmp_path, name, files)
    assert model_snapshot_present(tmp_path, BGE_BASE_ID.lower())


@pytest.mark.parametrize("missing", [*MARKERS, "model_optimized.onnx"])
def test_incomplete_gcs_dir_is_not_present(tmp_path: Path, hf_offline: None, missing: str) -> None:
    name, files = _gcs_layout(BGE_BASE_ID)
    assert "model_optimized.onnx" in files
    _make_gcs_dir(tmp_path, name, files, drop=missing)
    assert not model_snapshot_present(tmp_path, BGE_BASE_ID)


def test_gcs_dir_needs_additional_files(tmp_path: Path, hf_offline: None) -> None:
    model = "intfloat/multilingual-e5-large"
    name, files = _gcs_layout(model)
    assert len(files) > 1, "expected model.onnx_data alongside model.onnx"
    _make_gcs_dir(tmp_path, name, files, drop=files[-1])
    assert not model_snapshot_present(tmp_path, model)


def test_gcs_dir_without_the_prefix_is_not_the_deprecated_layout(
    tmp_path: Path, hf_offline: None
) -> None:
    name, files = _gcs_layout(BGE_BASE_ID)
    _make_gcs_dir(tmp_path, name.removeprefix("fast-"), files)
    assert not model_snapshot_present(tmp_path, BGE_BASE_ID)


def test_gcs_dir_is_not_cached_while_online(tmp_path: Path, hf_online: None) -> None:
    """Online, fastembed downloads from Hugging Face again before it would
    read the tarball directory, so the next load is a download."""
    name, files = _gcs_layout(BGE_BASE_ID)
    _make_gcs_dir(tmp_path, name, files)
    assert not model_snapshot_present(tmp_path, BGE_BASE_ID)


@pytest.mark.parametrize("offline", ["1", "true", " ON ", "yes"])
def test_offline_values_fastembed_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: str
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", offline)
    name, files = _gcs_layout(BGE_BASE_ID)
    _make_gcs_dir(tmp_path, name, files)
    assert model_snapshot_present(tmp_path, BGE_BASE_ID)


def test_offline_order_matches_fastembed() -> None:
    """Readiness copies how fastembed's loader parses ``HF_HUB_OFFLINE``,
    that a local Hugging Face snapshot is tried before the tarball
    directory, and that offline the directory is tried for an entry without
    a url too. Pin those lines so a change there fails here."""
    pytest.importorskip("fastembed")
    import inspect

    from fastembed.common.model_management import ModelManagement

    load_source = inspect.getsource(ModelManagement.download_model)
    assert 'os.environ.get("HF_HUB_OFFLINE", "").strip().upper()' in load_source
    assert 'hf_offline in {"1", "TRUE", "YES", "ON"}' in load_source
    assert load_source.index('cache_kwargs["local_files_only"] = True') < load_source.index(
        "retrieve_model_gcs("
    )
    assert "if url_source or local_files_only:" in load_source


@pytest.fixture(params=["with-url", "without-url"])
def plain_tarball_catalog(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> str:
    """A catalog holding one tarball entry without ``_deprecated_tar_struct``.

    fastembed 0.8 marks every tarball entry with the flag, so the plain
    ``<name>`` layout needs a fake entry. Its id is mixed-case so a test can
    tell the catalog's id from a lowercased one on any filesystem. Offline,
    fastembed looks for the directory whether or not the entry has a ``url``,
    so the fixture runs both ways.
    """
    fastembed = pytest.importorskip("fastembed")
    from memtomem.embedding import onnx

    entry = {
        "model": "Example/Plain-Tarball-Model",
        "sources": {
            "hf": "example/plain-tarball-model-onnx",
            "url": "https://example.invalid/plain-tarball-model.tar.gz",
            "_deprecated_tar_struct": False,
        },
        "model_file": "model.onnx",
        "additional_files": [],
    }
    if request.param == "without-url":
        entry["sources"] = {"hf": "example/plain-tarball-model-onnx", "url": None}

    class FakeTextEmbedding:
        @staticmethod
        def list_supported_models() -> list[dict[str, object]]:
            return [entry]

        @staticmethod
        def add_custom_model(model: str, **_: object) -> None:
            return None

    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTextEmbedding)
    monkeypatch.setattr(onnx, "_register_custom_models_if_needed", lambda: None)
    return str(entry["model"])


def test_gcs_dir_of_an_entry_without_the_deprecated_flag(
    tmp_path: Path, plain_tarball_catalog: str, hf_offline: None
) -> None:
    _make_gcs_dir(tmp_path, "fast-Plain-Tarball-Model", ("model.onnx",))
    assert not model_snapshot_present(tmp_path, plain_tarball_catalog)
    _make_gcs_dir(tmp_path, "Plain-Tarball-Model", ("model.onnx",))
    assert model_snapshot_present(tmp_path, plain_tarball_catalog)


def test_gcs_dir_takes_the_catalog_ids_case(plain_tarball_catalog: str) -> None:
    """fastembed names the directory from its own entry, so a lowercased
    request must still resolve to ``Plain-Tarball-Model``. Checked on the
    resolved name, because a case-insensitive filesystem would find the
    directory under either spelling."""
    from memtomem.embedding.readiness import _cache_spec

    spec = _cache_spec(plain_tarball_catalog.lower())
    assert spec.gcs_dir == "Plain-Tarball-Model"


@pytest.mark.parametrize(
    ("model_id", "gcs_name", "model_file"),
    [
        (MINILM_ID, "fast-all-MiniLM-L6-v2", "model.onnx"),
        (E5_ID, "fast-multilingual-e5-small", "onnx/model.onnx"),
    ],
)
def test_pinned_models_ignore_a_gcs_dir(
    tmp_path: Path, model_id: str, gcs_name: str, model_file: str
) -> None:
    """memtomem loads E5 and MiniLM from a pinned Hugging Face snapshot, and
    fastembed returns that path before trying any source."""
    _make_gcs_dir(tmp_path, gcs_name, (model_file,))
    assert not model_snapshot_present(tmp_path, model_id)


def test_gcs_dir_name_matches_fastembed() -> None:
    """``readiness._gcs_dir_name`` copies an inline computation from
    fastembed. Pin the upstream lines it copies, including where the
    directory sits, so a change there fails here."""
    pytest.importorskip("fastembed")
    import inspect

    from fastembed.common.model_management import ModelManagement

    from memtomem.embedding.readiness import _gcs_dir_name

    source = inspect.getsource(ModelManagement.retrieve_model_gcs)
    assert (
        "fast_model_name = f\"{'fast-' if deprecated_tar_struct else ''}"
        "{model_name.split('/')[-1]}\"" in source
    )
    assert "model_dir = Path(cache_dir) / fast_model_name" in source
    assert _gcs_dir_name("BAAI/bge-base-en-v1.5", True) == "fast-bge-base-en-v1.5"
    assert _gcs_dir_name("Org/Plain", False) == "Plain"


def test_registration_reads_and_adds_under_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness poll and the embedder's first load (a worker thread) both
    register the custom models, and FastEmbed raises on a second registration
    (#2585 review). The catalog check and the adds must share one lock."""
    fastembed = pytest.importorskip("fastembed")

    from memtomem.embedding import onnx

    held: list[bool] = []

    class FakeTextEmbedding:
        @staticmethod
        def list_supported_models() -> list[dict[str, object]]:
            held.append(onnx._CUSTOM_MODELS_LOCK.locked())
            return []

        @staticmethod
        def add_custom_model(model: str, **_: object) -> None:
            held.append(onnx._CUSTOM_MODELS_LOCK.locked())

    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTextEmbedding)
    onnx._register_custom_models_if_needed()

    assert held == [True, True, True]  # one catalog read, E5 and bge-m3 adds
    assert not onnx._CUSTOM_MODELS_LOCK.locked()


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
