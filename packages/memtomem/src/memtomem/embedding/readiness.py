"""Cache-presence helper for the lazy-loaded fastembed models.

Used by the ``GET /api/system/model-readiness`` endpoint to distinguish
"download in flight" from "loaded" from "cold cache, no work started"
without forcing a load itself. The check is filesystem-only.

The fastembed cache layout follows HuggingFace conventions:

    cache_dir/
      models--<sanitized-repo>/
        snapshots/
          <commit-sha>/
            config.json, tokenizer.json, tokenizer_config.json,
            special_tokens_map.json, <model file(s)>

where ``<sanitized-repo>`` is the HuggingFace repository the files were
downloaded from, with ``/`` replaced by ``--``. That repository is *not*
always the model id: fastembed downloads many built-in models from its
``sources.hf`` mirror (``sentence-transformers/all-MiniLM-L6-v2`` caches
under ``models--qdrant--all-MiniLM-L6-v2-onnx``), and the model file name
comes from the catalog too (``BAAI/bge-small-en-v1.5`` ships
``model_optimized.onnx``) (#2585). ``Path.exists`` follows symlinks, so the
underlying blobs must be there too.

Approximate-size lookups for the readiness banner live in the sibling
``aliases`` module — see ``embedding/aliases.py:approx_size_mb``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

# Re-exported for backwards compatibility with the readiness endpoint
# import site. Source of truth is ``embedding/aliases.py``.
from memtomem.embedding.aliases import approx_size_mb as approx_size_mb

# Files fastembed's tokenizer loader opens from a snapshot. 0.8.0 (the
# locked version) raises when any of these is missing; 0.8.1 relaxes
# ``config.json`` and ``special_tokens_map.json``. Requiring the union keeps
# "present" true only when every supported fastembed can load it, and it is
# exactly what the pinned downloads in ``profiles.py`` fetch.
_MARKER_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)

# A model the catalog cannot describe may keep its weights in either place.
_FALLBACK_MODEL_FILES = ("model.onnx", "onnx/model.onnx")


class _CacheSpec(NamedTuple):
    repo: str
    # ``None`` accepts any snapshot; a pinned model loads only its revision.
    revision: str | None
    # ``None`` means "either fallback layout"; otherwise every file is needed.
    model_files: tuple[str, ...] | None


def _pinned_spec(model_id: str) -> _CacheSpec | None:
    # Same predicates the loader uses to decide whether to pin (onnx.py), so
    # readiness checks the revision the loader will actually open.
    from memtomem.embedding.profiles import (
        E5_MODEL,
        E5_REVISION,
        MINILM_REPO,
        MINILM_REVISION,
        is_e5,
        is_minilm,
    )

    if is_e5(model_id):
        return _CacheSpec(E5_MODEL, E5_REVISION, ("onnx/model.onnx",))
    if is_minilm(model_id):
        return _CacheSpec(MINILM_REPO, MINILM_REVISION, ("model.onnx",))
    return None


def _catalog_spec(model_id: str) -> _CacheSpec | None:
    """Look ``model_id`` up the way fastembed does when it downloads it."""
    try:
        from fastembed import TextEmbedding  # type: ignore[import-untyped]
        from fastembed.rerank.cross_encoder import (  # type: ignore[import-untyped]
            TextCrossEncoder,
        )

        from memtomem.embedding.onnx import _register_custom_models_if_needed
    except ImportError:
        return None

    # The embedder registers E5 and bge-m3 on first load; until then the
    # catalog lacks them, and bge-m3's ``model.onnx_data`` would go unchecked.
    _register_custom_models_if_needed()
    wanted = model_id.lower()
    for cls in (TextEmbedding, TextCrossEncoder):
        for desc in cls.list_supported_models():
            if str(desc.get("model", "")).lower() != wanted:
                continue
            hf = (desc.get("sources") or {}).get("hf")
            model_file = desc.get("model_file")
            if not hf or not model_file:
                return None
            extra = tuple(desc.get("additional_files") or ())
            return _CacheSpec(hf, None, (model_file, *extra))
    return None


def _cache_spec(model_id: str) -> _CacheSpec:
    return _pinned_spec(model_id) or _catalog_spec(model_id) or _CacheSpec(model_id, None, None)


def _snapshot_complete(snap: Path, model_files: tuple[str, ...] | None) -> bool:
    if not snap.is_dir():
        return False
    if not all((snap / name).exists() for name in _MARKER_FILES):
        return False
    if model_files is None:
        return any((snap / name).exists() for name in _FALLBACK_MODEL_FILES)
    return all((snap / name).exists() for name in model_files)


def model_snapshot_present(cache_dir: Path, model_id: str) -> bool:
    """Return True iff a complete fastembed snapshot for ``model_id`` exists.

    Resolves the repository, revision and model files the loader will use:
    the pinned ones for E5 and MiniLM (``embedding/profiles.py``), else
    fastembed's catalog entry (``sources.hf``, ``model_file``,
    ``additional_files``), else the model id with a flat or nested
    ``model.onnx``. Only the default fp32 load is described; an int8 variant
    loads from its local artifact path, which this probe does not see.
    """
    spec = _cache_spec(model_id)
    base = cache_dir / ("models--" + spec.repo.replace("/", "--")) / "snapshots"
    if spec.revision is not None:
        return _snapshot_complete(base / spec.revision, spec.model_files)
    if not base.is_dir():
        return False
    return any(_snapshot_complete(snap, spec.model_files) for snap in base.iterdir())
