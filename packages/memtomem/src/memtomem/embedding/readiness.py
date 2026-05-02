"""Readiness helpers for the lazy-loaded fastembed models.

Used by the ``GET /api/system/model-readiness`` endpoint to distinguish
"download in flight" from "loaded" from "cold cache, no work started"
without forcing a load itself. The check is filesystem-only.

The fastembed cache layout follows HuggingFace conventions:

    cache_dir/
      models--<sanitized-id>/
        snapshots/
          <commit-sha>/
            config.json
            tokenizer.json
            (model.onnx OR onnx/model.onnx)

where ``<sanitized-id>`` = ``model_id.replace("/", "--")``. A snapshot
is considered complete when ``config.json``, ``tokenizer.json``, and
either flat or nested ``model.onnx`` are all present (``Path.exists``
follows symlinks, so the underlying blobs must be there too).
"""

from __future__ import annotations

from pathlib import Path

# Approximate sizes for the model identifiers documented in
# ``cli/init_cmd.py``. Used by the readiness endpoint to populate banner
# copy ("Downloading bge-m3 (~2.3 GB)…"). Unknown models map to None;
# the banner falls back to a size-less message.
_APPROX_SIZE_MB: dict[str, int] = {
    "BAAI/bge-m3": 2300,
    "BAAI/bge-small-en-v1.5": 130,
    "sentence-transformers/all-MiniLM-L6-v2": 90,
    "nomic-ai/nomic-embed-text-v1.5": 280,
    "jinaai/jina-reranker-v2-base-multilingual": 1100,
    "Xenova/ms-marco-MiniLM-L-6-v2": 80,
}


def model_snapshot_present(cache_dir: Path, model_id: str) -> bool:
    """Return True iff a complete fastembed snapshot for ``model_id`` exists.

    Walks ``cache_dir/models--<sanitized>/snapshots/`` and accepts the
    first subdirectory that contains ``config.json``, ``tokenizer.json``,
    and either ``model.onnx`` or ``onnx/model.onnx`` — fastembed uses the
    nested form for some packages (e.g. ``BAAI/bge-m3``) and the flat
    form for others.
    """
    sanitized = "models--" + model_id.replace("/", "--")
    base = cache_dir / sanitized / "snapshots"
    if not base.is_dir():
        return False
    for snap in base.iterdir():
        if not snap.is_dir():
            continue
        if not (snap / "config.json").exists():
            continue
        if not (snap / "tokenizer.json").exists():
            continue
        if (snap / "model.onnx").exists() or (snap / "onnx" / "model.onnx").exists():
            return True
    return False


def approx_size_mb(model_id: str) -> int | None:
    """Return the documented approximate size for ``model_id`` in MB, or ``None``."""
    return _APPROX_SIZE_MB.get(model_id)
