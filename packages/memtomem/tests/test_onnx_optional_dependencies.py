"""Missing tokenizer extras must be actionable before storage opens."""

import builtins
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from memtomem import __version__
from memtomem.chunking.bounded import _tokenizer
from memtomem.config import EmbeddingConfig, Mem2MemConfig
from memtomem.embedding.profiles import (
    E5_TOKENIZER,
    e5_snapshot,
    minilm_snapshot,
    resolve_tokenizer,
)
from memtomem.errors import ConfigError
from memtomem.server.context import AppContext
from memtomem.server.tools.status_config import mem_status


def block_import(monkeypatch, module, missing=None):
    original = builtins.__import__
    error = ModuleNotFoundError(f"No module named '{missing or module}'", name=missing or module)

    def importing(name, *args, **kwargs):
        if name == module:
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    return error


@pytest.mark.parametrize(
    "module,invoke",
    [
        ("huggingface_hub", lambda: resolve_tokenizer(E5_TOKENIZER)),
        ("huggingface_hub", e5_snapshot),
        ("huggingface_hub", minilm_snapshot),
        ("tokenizers", lambda: _tokenizer("missing-dependency-tokenizer.json", 0, 0)),
    ],
)
def test_missing_optional_module_has_server_environment_guidance(monkeypatch, module, invoke):
    error = block_import(monkeypatch, module)
    with pytest.raises(ConfigError) as caught:
        invoke()
    assert caught.value.__cause__ is error
    message = str(caught.value)
    assert module in message
    assert f"uvx --from 'memtomem[onnx]=={__version__}' memtomem-server" in message
    assert "environment that runs this server" in message
    assert "restart" in message


@pytest.mark.parametrize(
    "module,invoke",
    [
        ("huggingface_hub", lambda: resolve_tokenizer(E5_TOKENIZER)),
        ("huggingface_hub", e5_snapshot),
        ("huggingface_hub", minilm_snapshot),
        ("tokenizers", lambda: _tokenizer("unrelated-missing-tokenizer.json", 0, 0)),
    ],
)
def test_broken_transitive_import_is_not_misdiagnosed(monkeypatch, module, invoke):
    error = block_import(monkeypatch, module, missing="some_other_dependency")
    with pytest.raises(ModuleNotFoundError) as caught:
        invoke()
    assert caught.value is error


@pytest.mark.parametrize("module", ["huggingface_hub", "tokenizers"])
async def test_mem_status_missing_dependency_fails_before_storage_with_guidance(
    monkeypatch, tmp_path, module
):
    import memtomem.runtime.components as factory

    config = Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx"))
    app = AppContext(config, ambient_config_loaded=True)
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
    storage = Mock(side_effect=AssertionError("must not open storage"))
    monkeypatch.setattr(factory, "create_storage", storage)
    if module == "tokenizers":
        # Reach the tokenizer import without a Hub request or model download.
        path = tmp_path / "tokenizer.json"
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("memtomem.embedding.profiles.resolve_tokenizer", lambda _: path)
    block_import(monkeypatch, module)
    try:
        result = await mem_status(ctx=ctx)
        assert result.startswith(f"Error: Missing optional module '{module}'")
        assert "memtomem[onnx]" in result
        assert "internal error" not in result
        assert app._components is None
        storage.assert_not_called()
    finally:
        await app.close()


def test_local_tokenizer_resolution_does_not_require_hub(monkeypatch, tmp_path):
    block_import(monkeypatch, "huggingface_hub")
    path = tmp_path / "tokenizer.json"
    assert resolve_tokenizer(str(path)) == path.resolve()


@pytest.mark.parametrize("failure", ["download", "checksum"])
def test_tokenizer_artifact_errors_keep_their_original_meaning(monkeypatch, tmp_path, failure):
    import huggingface_hub

    path = tmp_path / "tokenizer.json"
    path.write_text("corrupted artifact", encoding="utf-8")
    error = RuntimeError("download unavailable")

    def download(*args, **kwargs):
        if failure == "download":
            raise error
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    expected = RuntimeError if failure == "download" else ValueError
    with pytest.raises(expected) as caught:
        resolve_tokenizer(E5_TOKENIZER)
    if failure == "download":
        assert caught.value is error
    else:
        assert "checksum mismatch" in str(caught.value)
