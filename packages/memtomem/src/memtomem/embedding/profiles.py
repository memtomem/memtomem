"""Pinned CPU model contracts, independent of inference/model allocation."""

from __future__ import annotations

import hashlib
import json
import platform
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig

E5_MODEL = "intfloat/multilingual-e5-small"
E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
E5_TOKENIZER = f"model:{E5_MODEL}@{E5_REVISION}"
E5_TOKENIZER_SHA256 = "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"
E5_MODEL_SHA256 = "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"
CPU_VARIANTS = ("fp32", "int8-arm64", "int8-avx2", "int8-avx512", "int8-avx512-vnni")


def is_e5(model: str) -> bool:
    return model in ("multilingual-e5-small", E5_MODEL)


@lru_cache(maxsize=8)
def _verify_file(path: Path, mtime: int, size: int, expected: str) -> None:
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("Pinned E5 artifact checksum mismatch")


def resolve_tokenizer(path: str) -> Path:
    if path != E5_TOKENIZER:
        return Path(path).expanduser().resolve()
    from huggingface_hub import hf_hub_download

    from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir

    resolved = Path(
        hf_hub_download(
            E5_MODEL,
            "tokenizer.json",
            revision=E5_REVISION,
            cache_dir=str(resolve_fastembed_cache_dir()),
        )
    )
    stat = resolved.stat()
    _verify_file(resolved, stat.st_mtime_ns, stat.st_size, E5_TOKENIZER_SHA256)
    return resolved


def e5_snapshot() -> Path:
    from huggingface_hub import snapshot_download

    from memtomem.embedding.fastembed_cache import resolve_fastembed_cache_dir

    snapshot = Path(
        snapshot_download(
            E5_MODEL,
            revision=E5_REVISION,
            cache_dir=str(resolve_fastembed_cache_dir()),
            allow_patterns=[
                "onnx/model.onnx",
                "config.json",
                "tokenizer.json",
                "tokenizer_config.json",
                "special_tokens_map.json",
            ],
        )
    )
    resolve_tokenizer(E5_TOKENIZER)
    model = snapshot / "onnx/model.onnx"
    stat = model.stat()
    _verify_file(model, stat.st_mtime_ns, stat.st_size, E5_MODEL_SHA256)
    return snapshot


def apply_e5_defaults(config: Mem2MemConfig) -> None:
    """Only fill unspecified fields; explicit existing budgets need migration."""
    if config.embedding.provider.lower() != "onnx" or not is_e5(config.embedding.model):
        # A later precedence layer may replace an automatically selected E5.
        # Remove only generated values; explicit budgets remain untouched.
        if (
            config.indexing.chunk_input_prefix == "passage: "
            and "chunk_input_prefix" not in config.indexing.model_fields_set
        ):
            from memtomem.config import IndexingConfig

            baseline = IndexingConfig()
            for key in (
                "hard_max_chunk_tokens",
                "chunk_context_tokens",
                "chunk_model_tokens",
                "chunk_tokenizer_path",
                "max_chunk_tokens",
                "target_chunk_tokens",
                "min_chunk_tokens",
                "chunk_overlap_tokens",
                "chunk_input_prefix",
            ):
                if key not in config.indexing.model_fields_set:
                    object.__setattr__(config.indexing, key, getattr(baseline, key))
        return
    tokenizer_path = E5_TOKENIZER
    if config.embedding.onnx_variant != "fp32":
        tokenizer_path = str(
            (Path(config.embedding.onnx_artifact_path).expanduser() / "tokenizer.json").resolve()
        )
    defaults = {
        "hard_max_chunk_tokens": 384,
        "chunk_context_tokens": 96,
        "chunk_model_tokens": 512,
        "chunk_tokenizer_path": tokenizer_path,
        "max_chunk_tokens": 384,
        "target_chunk_tokens": 320,
        "min_chunk_tokens": 96,
        "chunk_overlap_tokens": 0,
        "chunk_input_prefix": "passage: ",
    }
    for key, value in defaults.items():
        if key not in config.indexing.model_fields_set:
            object.__setattr__(config.indexing, key, value)
    if config.indexing.chunk_input_prefix != "passage: ":
        raise ValueError("E5 chunk_input_prefix must be 'passage: '")
    if config.indexing.chunk_model_tokens > 512 or not config.indexing.hard_max_chunk_tokens:
        raise ValueError("E5 requires exact chunk budgets with chunk_model_tokens <= 512")
    if config.indexing.chunk_tokenizer_path != tokenizer_path:
        raise ValueError("E5 requires its pinned model tokenizer; migrate the chunk configuration")
    # Attribute assignment intentionally avoids recursive Pydantic validation;
    # validate the completed profile once, including explicit soft overrides.
    type(config.indexing).model_validate(config.indexing.model_dump())


def artifact_manifest(directory: str, model: str, variant: str) -> dict[str, object]:
    """Validate a locally exported artifact before trusting its identity."""
    root = Path(directory).expanduser().resolve()
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if data.get("model") != model or data.get("variant") != variant:
        raise ValueError("ONNX artifact model/variant does not match configuration")
    if data.get("schema") != 1 or data.get("precision") != "int8":
        raise ValueError("Unsupported ONNX artifact manifest schema or precision")
    if model == E5_MODEL and data.get("revision") != E5_REVISION:
        raise ValueError("E5 artifact must use the pinned model revision")
    arch = platform.machine().lower()
    expected = "arm64" if variant == "int8-arm64" else "x86_64"
    actual = {"aarch64": "arm64", "amd64": "x86_64"}.get(arch, arch)
    if actual != expected:
        raise ValueError(f"ONNX variant {variant} is not approved for CPU architecture {arch}")
    hashes = data.get("files")
    if not isinstance(hashes, dict) or "model.onnx" not in hashes or "tokenizer.json" not in hashes:
        raise ValueError("ONNX artifact manifest is incomplete")
    for name, expected_hash in hashes.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("ONNX artifact file is missing or escapes its directory")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected_hash:
            raise ValueError(f"ONNX artifact checksum mismatch: {name}")
    if model == E5_MODEL:
        if hashes["tokenizer.json"] != E5_TOKENIZER_SHA256:
            raise ValueError("E5 artifact tokenizer differs from its chunk-budget tokenizer")
    return data


def variant_identity(directory: str) -> str:
    """Hash the declared manifest without loading or hashing model weights."""
    return hashlib.sha256((Path(directory).expanduser() / "manifest.json").read_bytes()).hexdigest()
