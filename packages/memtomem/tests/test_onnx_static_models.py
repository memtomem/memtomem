"""FastEmbed static models (potion) behind memtomem's ONNX embedder.

FastEmbed 0.9 computes these from ``model.safetensors`` with numpy, where
0.8 ran an ONNX export of the same model. There is no ORT session, so the
arena check cannot apply, and inference tokenizes without special tokens.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from memtomem.config import EmbeddingConfig
from memtomem.embedding import onnx
from memtomem.embedding.onnx import OnnxEmbedder, _truncated_input_indexes
from memtomem.errors import EmbeddingError

pytest.importorskip("fastembed")

POTION = "minishlab/potion-base-8M"


class _RecordingTokenizer:
    """Tokenizer double that records how ``encode`` was called."""

    def __init__(self, limit: int = 512) -> None:
        self.truncation = {
            "max_length": limit,
            "stride": 0,
            "strategy": "longest_first",
            "direction": "right",
        }
        self.encode_kwargs: list[dict[str, object]] = []

    def enable_truncation(self, **kwargs: object) -> None:
        self.truncation.update(kwargs)

    def encode(self, text: str, **kwargs: object) -> SimpleNamespace:
        self.encode_kwargs.append(kwargs)
        return SimpleNamespace(overflowing=[])


def _static_model(tokenizer: object) -> SimpleNamespace:
    """A ``TextEmbedding`` stand-in whose implementation is FastEmbed's own
    static class, built without loading any weights."""
    from fastembed.text.static_embedding import StaticTextEmbedding

    inner = object.__new__(StaticTextEmbedding)
    inner.tokenizer = tokenizer
    return SimpleNamespace(
        model=inner,
        embed=lambda texts, batch_size=None: iter(np.array([0.1, 0.2, 0.3]) for _ in texts),
    )


def _config(model: str, **overrides: object) -> EmbeddingConfig:
    values: dict[str, object] = dict(provider="onnx", model=model, dimension=3)
    values.update(overrides)
    return EmbeddingConfig(**values)  # type: ignore[arg-type]


def test_every_static_model_fastembed_ships_was_compared() -> None:
    """A static model FastEmbed adds must be compared with what loaded under
    that id before, then listed; until then the embedder refuses it."""
    assert onnx._static_catalog_ids() == onnx._VERIFIED_STATIC_MODELS


def test_static_ids_are_read_from_the_class_text_embedding_routes_by() -> None:
    from fastembed import TextEmbedding
    from fastembed.text.static_embedding import StaticTextEmbedding

    assert StaticTextEmbedding in TextEmbedding.EMBEDDINGS_REGISTRY
    assert POTION.lower() in onnx._static_catalog_ids()


@pytest.mark.parametrize("arena", [False, True])
async def test_verified_static_model_loads_without_an_ort_session(arena: bool) -> None:
    tokenizer = _RecordingTokenizer()
    embedder = OnnxEmbedder(_config(POTION, onnx_cpu_mem_arena=arena))
    with patch("fastembed.TextEmbedding", return_value=_static_model(tokenizer)):
        vectors = await embedder.embed_texts(["hello"])
    await embedder.close()
    assert vectors == [[0.1, 0.2, 0.3]]


async def test_an_onnx_model_without_a_session_is_still_refused() -> None:
    """Only FastEmbed's static class is exempt, not any layout without a session."""
    model = SimpleNamespace(model=SimpleNamespace(tokenizer=_RecordingTokenizer()))
    embedder = OnnxEmbedder(_config("BAAI/bge-small-en-v1.5"))
    with (
        patch("fastembed.TextEmbedding", return_value=model),
        pytest.raises(EmbeddingError, match="refusing unsafe ONNX fallback"),
    ):
        await embedder.embed_texts(["hello"])
    await embedder.close()


async def test_unlisted_static_model_is_refused_before_it_is_downloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        onnx, "_static_catalog_ids", lambda: frozenset({"example/new-static-model"})
    )
    embedder = OnnxEmbedder(_config("Example/New-Static-Model"))
    with (
        patch("fastembed.TextEmbedding") as constructor,
        pytest.raises(EmbeddingError) as raised,
    ):
        await embedder.embed_texts(["hello"])
    await embedder.close()
    constructor.assert_not_called()
    message = str(raised.value)
    assert "Example/New-Static-Model is a FastEmbed static model" in message
    assert "onnx_cpu_mem_arena" not in message


async def test_unlisted_static_model_is_refused_when_the_catalog_did_not_name_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog lookup reads a private FastEmbed method. If it comes back
    empty, the loaded class still decides."""
    monkeypatch.setattr(onnx, "_static_catalog_ids", lambda: frozenset())
    embedder = OnnxEmbedder(_config("example/new-static-model", onnx_cpu_mem_arena=True))
    with (
        patch("fastembed.TextEmbedding", return_value=_static_model(_RecordingTokenizer())),
        pytest.raises(EmbeddingError, match="is a FastEmbed static model"),
    ):
        await embedder.embed_texts(["hello"])
    await embedder.close()


async def test_static_model_preflight_counts_without_special_tokens() -> None:
    tokenizer = _RecordingTokenizer()
    embedder = OnnxEmbedder(_config(POTION))
    with patch("fastembed.TextEmbedding", return_value=_static_model(tokenizer)):
        await embedder.embed_texts(["한국어 입력"])
    await embedder.close()
    assert tokenizer.encode_kwargs == [{"add_special_tokens": False}]


def _word_tokenizer(limit: int) -> object:
    """A real ``tokenizers`` tokenizer that wraps every input in two special
    tokens and truncates at ``limit``, as potion's ``tokenizer.json`` does."""
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from tokenizers.processors import TemplateProcessing

    vocab = {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "word": 3}
    tokenizer = tokenizers.Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 1), ("[SEP]", 2)]
    )
    tokenizer.enable_truncation(max_length=limit)
    return tokenizer


@pytest.mark.parametrize(
    ("words", "with_special", "without_special"),
    [
        (6, [], []),
        # 7 and 8 words fit the 8-token limit as a static model tokenizes them;
        # with the two special tokens counted they read as truncated.
        (7, [0], []),
        (8, [0], []),
        (9, [0], [0]),
    ],
)
def test_preflight_at_the_limit_depends_on_special_tokens(
    words: int, with_special: list[int], without_special: list[int]
) -> None:
    tokenizer = _word_tokenizer(8)
    texts = ["word " * words]
    assert _truncated_input_indexes(tokenizer, texts) == with_special
    assert _truncated_input_indexes(tokenizer, texts, add_special_tokens=False) == without_special
