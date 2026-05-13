"""Cache-presence helper for the lazy-loaded fastembed models.

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

Approximate-size lookups for the readiness banner live in the sibling
``aliases`` module — see ``embedding/aliases.py:approx_size_mb``.
"""

from __future__ import annotations

from pathlib import Path

# Re-exported for backwards compatibility with the readiness endpoint
# import site. Source of truth is ``embedding/aliases.py``.
from memtomem.embedding.aliases import approx_size_mb as approx_size_mb


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
