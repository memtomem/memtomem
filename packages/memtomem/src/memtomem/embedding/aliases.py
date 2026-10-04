"""Single source of truth for fastembed model aliases and approximate sizes.

Used by every surface that references a model name or has to tell the
user how big the download is:

* ``OnnxEmbedder._get_model`` — short-name → fastembed-id resolution.
* ``GET /api/system/model-readiness`` — banner annotation
  ("Downloading bge-m3 (~2300 MB)…").
* ``cli init`` wizard — size text shown to the user.

Sizes come from fastembed's own ``list_supported_models()`` /
``size_in_GB`` field (rounded to MB). Custom-registered E5 and BGE models (see
``embedding/onnx.py:_register_custom_models_if_needed``) carry the
size declared on their ``add_custom_model`` call. Verify with::

    from fastembed import TextEmbedding
    [(m['model'], m['size_in_GB']) for m in TextEmbedding.list_supported_models()]

Adding a new model is a one-file edit here; the readiness banner,
cache probe, embedder loader, and init wizard all read from these
tables.
"""

from __future__ import annotations

# Short alias → (fastembed model id, dimension, approximate size in MB).
# Sizes match fastembed metadata exactly so the readiness banner and
# the init wizard agree.
ONNX_EMBEDDER_MODELS: dict[str, tuple[str, int, int]] = {
    "multilingual-e5-small": ("intfloat/multilingual-e5-small", 384, 490),
    "all-MiniLM-L6-v2": ("sentence-transformers/all-MiniLM-L6-v2", 384, 90),
    "bge-small-en-v1.5": ("BAAI/bge-small-en-v1.5", 384, 67),
    # Custom-registered (see embedding/onnx.py:_register_custom_models_if_needed).
    # Size mirrors ``size_in_gb=2.3`` declared on add_custom_model — the
    # actual ``model.onnx`` + ``model.onnx_data`` blobs total ~2.3 GB on disk.
    "bge-m3": ("BAAI/bge-m3", 1024, 2300),
}


# Additional fastembed embedder ids (no short alias). Lookups by full id
# need an answer too — primarily for the nomic-ai variants in case a
# user pastes the raw id into ``config.embedding.model`` instead of
# picking a short alias.
_EXTRA_EMBEDDER_SIZES: dict[str, int] = {
    "nomic-ai/nomic-embed-text-v1.5": 520,
    "nomic-ai/nomic-embed-text-v1.5-Q": 130,
}


# Reranker fastembed id → MB. Reranker config stores the full fastembed
# id verbatim (no short-name resolution), so only size lookup is needed.
FASTEMBED_RERANKER_SIZES: dict[str, int] = {
    "Xenova/ms-marco-MiniLM-L-6-v2": 80,
    "Xenova/ms-marco-MiniLM-L-12-v2": 120,
    "BAAI/bge-reranker-base": 1040,
    "jinaai/jina-reranker-v1-tiny-en": 130,
    "jinaai/jina-reranker-v1-turbo-en": 150,
    "jinaai/jina-reranker-v2-base-multilingual": 1110,
    # Registered by memtomem, not fastembed: the INT8 file named in
    # ``search/reranker/fastembed.py:_CUSTOM_RERANKERS``.
    "onnx-community/gte-multilingual-reranker-base": 341,
}


# Lower-cased short alias or full id → canonical fastembed id. FastEmbed
# finds a model with ``str.lower()`` on both sides, but indexes its
# custom-model post-processing by the exact id it was given, so a differently
# cased id loads the catalog entry and then fails (#2602). Folding here, with
# FastEmbed's own rule, hands every consumer the one spelling it registered.
_CANONICAL_EMBEDDER_IDS: dict[str, str] = {
    **{alias.lower(): entry[0] for alias, entry in ONNX_EMBEDDER_MODELS.items()},
    **{entry[0].lower(): entry[0] for entry in ONNX_EMBEDDER_MODELS.values()},
}


def resolve_embedder_id(model: str) -> str:
    """Translate a memtomem short-name to its fastembed id, or pass through.

    A known short name or full id matches in any case and comes back spelled
    as fastembed registers it; an unknown id passes through verbatim.
    """
    return _CANONICAL_EMBEDDER_IDS.get(model.lower(), model)


def approx_size_mb(model_id: str) -> int | None:
    """Approximate on-disk download size in MB.

    Accepts a memtomem short-name (``bge-m3``), a fastembed embedder id
    (``BAAI/bge-m3``), or a fastembed reranker id
    (``jinaai/jina-reranker-v2-base-multilingual``). Returns ``None``
    for anything not in the documented set.
    """
    entry = ONNX_EMBEDDER_MODELS.get(model_id)
    if entry:
        return entry[2]
    for full, _dim, size in ONNX_EMBEDDER_MODELS.values():
        if full == model_id:
            return size
    if model_id in _EXTRA_EMBEDDER_SIZES:
        return _EXTRA_EMBEDDER_SIZES[model_id]
    return FASTEMBED_RERANKER_SIZES.get(model_id)


def format_size(mb: int) -> str:
    """Render a size in MB or GB depending on magnitude.

    Used by surfaces that show the size to humans (init wizard,
    eventually the banner if it ever needs to render directly).
    """
    if mb >= 1000:
        return f"{mb / 1000:.1f} GB"
    return f"{mb} MB"
