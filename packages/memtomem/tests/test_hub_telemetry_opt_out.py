"""memtomem's Hub downloads run with huggingface_hub telemetry off (#2550, #2552)."""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("huggingface_hub")

import huggingface_hub  # noqa: E402
from huggingface_hub import constants  # noqa: E402
from huggingface_hub.utils import _headers, build_hf_headers  # noqa: E402

from memtomem.config import EmbeddingConfig, RerankConfig  # noqa: E402
from memtomem.embedding import hub_telemetry, profiles  # noqa: E402
from memtomem.embedding.hub_telemetry import hub_telemetry_off  # noqa: E402
from memtomem.embedding.profiles import (  # noqa: E402
    E5_TOKENIZER,
    e5_snapshot,
    resolve_tokenizer,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_TELEMETRY", False)
    monkeypatch.setattr(hub_telemetry, "_hub_telemetry_depth", 0)
    monkeypatch.setattr(profiles, "_verify_file", lambda *args: None)
    monkeypatch.setenv("MEMTOMEM_FASTEMBED_CACHE", str(tmp_path / "cache"))
    for name in ("HF_HUB_DISABLE_TELEMETRY", "DISABLE_TELEMETRY", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)


def _fake_tokenizer_download(tmp_path, seen):
    path = tmp_path / "tokenizer.json"
    path.write_text("{}", encoding="utf-8")

    def download(*args, **kwargs):
        seen.append(constants.HF_HUB_DISABLE_TELEMETRY)
        return str(path)

    return download


def test_tokenizer_download_runs_with_telemetry_off(monkeypatch, tmp_path):
    seen: list[bool] = []
    monkeypatch.setattr(
        huggingface_hub, "hf_hub_download", _fake_tokenizer_download(tmp_path, seen)
    )
    resolve_tokenizer(E5_TOKENIZER)
    assert seen == [True]
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


@pytest.mark.parametrize("name", ["HF_HUB_DISABLE_TELEMETRY", "DISABLE_TELEMETRY", "DO_NOT_TRACK"])
def test_environment_opt_in_is_overridden(monkeypatch, tmp_path, name):
    monkeypatch.setenv(name, "0")
    seen: list[bool] = []
    monkeypatch.setattr(
        huggingface_hub, "hf_hub_download", _fake_tokenizer_download(tmp_path, seen)
    )
    resolve_tokenizer(E5_TOKENIZER)
    assert seen == [True]
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_existing_opt_out_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_TELEMETRY", True)
    seen: list[bool] = []
    monkeypatch.setattr(
        huggingface_hub, "hf_hub_download", _fake_tokenizer_download(tmp_path, seen)
    )
    resolve_tokenizer(E5_TOKENIZER)
    assert seen == [True]
    assert constants.HF_HUB_DISABLE_TELEMETRY is True


def test_a_false_written_during_the_window_is_not_clobbered(monkeypatch):
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_TELEMETRY", True)
    with hub_telemetry_off():
        constants.HF_HUB_DISABLE_TELEMETRY = False
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_a_true_written_during_the_window_is_reverted():
    # Documented limit: indistinguishable from the window's own write.
    with hub_telemetry_off():
        constants.HF_HUB_DISABLE_TELEMETRY = True
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_flag_is_restored_when_the_download_raises(monkeypatch):
    error = RuntimeError("download unavailable")

    def download(*args, **kwargs):
        assert constants.HF_HUB_DISABLE_TELEMETRY is True
        raise error

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    with pytest.raises(RuntimeError) as caught:
        resolve_tokenizer(E5_TOKENIZER)
    assert caught.value is error
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_snapshot_and_tokenizer_use_separate_windows(monkeypatch, tmp_path):
    snapshot = tmp_path / "snapshot"
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "onnx/model.onnx").write_bytes(b"model")
    seen: list[tuple[str, bool]] = []

    def snapshot_download(*args, **kwargs):
        seen.append(("snapshot", constants.HF_HUB_DISABLE_TELEMETRY))
        return str(snapshot)

    real_resolve = profiles.resolve_tokenizer

    def resolve(path):
        seen.append(("between", constants.HF_HUB_DISABLE_TELEMETRY))
        return real_resolve(path)

    def download(*args, **kwargs):
        seen.append(("tokenizer", constants.HF_HUB_DISABLE_TELEMETRY))
        return _fake_tokenizer_download(tmp_path, [])()

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    monkeypatch.setattr(profiles, "resolve_tokenizer", resolve)
    assert e5_snapshot() == snapshot
    assert seen == [("snapshot", True), ("between", False), ("tokenizer", True)]
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_nested_windows_restore_only_on_the_outer_exit():
    with hub_telemetry_off():
        with hub_telemetry_off():
            assert constants.HF_HUB_DISABLE_TELEMETRY is True
        assert constants.HF_HUB_DISABLE_TELEMETRY is True
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_overlapping_threads_keep_the_flag_until_the_last_exit():
    a_inside, b_inside, a_done = threading.Event(), threading.Event(), threading.Event()
    seen: dict[str, bool] = {}
    errors: list[BaseException] = []

    def first():
        try:
            with hub_telemetry_off():
                a_inside.set()
                assert b_inside.wait(5)
        except BaseException as exc:  # surfaced below
            errors.append(exc)
        finally:
            a_done.set()

    def second():
        try:
            assert a_inside.wait(5)
            with hub_telemetry_off():
                b_inside.set()
                assert a_done.wait(5)
                seen["after_first_exit"] = constants.HF_HUB_DISABLE_TELEMETRY
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    assert seen == {"after_first_exit": True}
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


def test_user_agent_drops_the_agent_tag_inside_the_window(monkeypatch):
    if not hasattr(_headers, "detect_agent"):
        pytest.skip("this huggingface-hub has no agent detection to tag the user agent")
    monkeypatch.setattr(_headers, "detect_agent", lambda: "probe")
    # Witness: the stub is live, so the negative below is not vacuous.
    assert "agent/probe" in build_hf_headers(token=False)["user-agent"]
    with hub_telemetry_off():
        assert "agent/" not in build_hf_headers(token=False)["user-agent"]
    assert "agent/probe" in build_hf_headers(token=False)["user-agent"]


def _onnx_embedder(monkeypatch, text_embedding):
    pytest.importorskip("fastembed")
    from memtomem.embedding import onnx

    monkeypatch.setattr("fastembed.TextEmbedding", text_embedding)
    monkeypatch.setattr(onnx, "_register_custom_models_if_needed", lambda: None)
    monkeypatch.setattr(onnx, "_verify_cpu_mem_arena", lambda *args: None)
    monkeypatch.setattr(onnx, "_configure_tokenizer_limit", lambda *args: (None, None))
    return onnx.OnnxEmbedder(
        EmbeddingConfig(provider="onnx", model="bge-small-en-v1.5", dimension=384)
    )


def _fastembed_reranker(monkeypatch, text_cross_encoder):
    pytest.importorskip("fastembed")
    from memtomem.search.reranker.fastembed import FastEmbedReranker

    monkeypatch.setattr("fastembed.rerank.cross_encoder.TextCrossEncoder", text_cross_encoder)
    return FastEmbedReranker(
        RerankConfig(enabled=True, provider="fastembed", model="Xenova/ms-marco-MiniLM-L-6-v2")
    )


@pytest.mark.parametrize("build", [_onnx_embedder, _fastembed_reranker])
def test_fastembed_constructor_runs_with_telemetry_off(monkeypatch, build):
    seen: list[bool] = []

    class _Model:
        def __init__(self, *args, **kwargs):
            seen.append(constants.HF_HUB_DISABLE_TELEMETRY)

    loaded = build(monkeypatch, _Model)._get_model()
    assert isinstance(loaded, _Model)
    assert seen == [True]
    assert constants.HF_HUB_DISABLE_TELEMETRY is False


@pytest.mark.parametrize("build", [_onnx_embedder, _fastembed_reranker])
def test_flag_is_restored_when_a_fastembed_constructor_raises(monkeypatch, build):
    error = RuntimeError("download unavailable")

    class _Model:
        def __init__(self, *args, **kwargs):
            assert constants.HF_HUB_DISABLE_TELEMETRY is True
            raise error

    with pytest.raises(RuntimeError) as caught:
        build(monkeypatch, _Model)._get_model()
    assert caught.value is error
    assert constants.HF_HUB_DISABLE_TELEMETRY is False
