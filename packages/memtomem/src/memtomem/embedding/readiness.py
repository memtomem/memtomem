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

A catalog entry that also has a ``sources.url`` tarball can be cached a
second way. When the Hugging Face download fails, fastembed downloads the
tarball instead and unpacks it flat into one directory:

    cache_dir/
      fast-<name>/   (entries marked ``_deprecated_tar_struct``)
      <name>/        (other entries)
        config.json, tokenizer.json, tokenizer_config.json,
        special_tokens_map.json, <model file(s)>

where ``<name>`` is the last ``/`` segment of the catalog's model id
(``BAAI/bge-base-en-v1.5`` unpacks to ``fast-bge-base-en-v1.5``).

fastembed only reads that directory when no Hugging Face snapshot is found
locally. Online it then downloads from Hugging Face again, and uses the
directory only if that fails, so the next load is a download and readiness
does not count the directory. With ``HF_HUB_OFFLINE`` set it goes straight to
the directory, for every catalog entry (including ones with no
``sources.url``, such as the custom ``BAAI/bge-m3``, or a reranker), and
readiness counts a complete one (#2594).

This is an approximation of fastembed's loader, kept small on purpose: the
answer only picks the ``downloading`` or ``loading`` label while a load runs.
Offline it can say ``loading`` for a load that then fails, when fastembed
picks a cached snapshot other than the complete copy found here: an
incomplete snapshot that ``refs/main`` names (a failed download can leave
one) next to a complete tarball directory or an older complete snapshot, or
a ``files_metadata.json`` that fastembed cannot parse. The failed load then
shows as an error.

Approximate-size lookups for the readiness banner live in the sibling
``aliases`` module — see ``embedding/aliases.py:approx_size_mb``.
"""

from __future__ import annotations

import os
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
    # Directory under ``cache_dir`` where fastembed looks for a model unpacked
    # from a GCS tarball, or ``None`` for a pinned or uncatalogued model.
    gcs_dir: str | None = None


def _gcs_dir_name(catalog_model: str, deprecated_tar_struct: bool) -> str:
    """Name of the directory fastembed unpacks a model's GCS tarball to.

    fastembed exposes no side-effect-free way to ask for it:
    ``ModelManagement.retrieve_model_gcs`` computes it inline and then creates,
    downloads or deletes files, even with ``local_files_only=True``. This
    mirrors that computation (``fastembed/common/model_management.py``,
    0.8.0 and 0.8.1)::

        fast_model_name = f"{'fast-' if deprecated_tar_struct else ''}{model_name.split('/')[-1]}"
        model_dir = Path(cache_dir) / fast_model_name

    ``test_gcs_dir_name_matches_fastembed`` pins those lines, so an upstream
    change fails CI instead of drifting silently.
    """
    return f"{'fast-' if deprecated_tar_struct else ''}{catalog_model.split('/')[-1]}"


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
            sources = desc.get("sources") or {}
            hf = sources.get("hf")
            model_file = desc.get("model_file")
            # Every fastembed 0.8 entry with a ``url`` also has ``hf``, so a
            # URL-only entry is not handled separately.
            if not hf or not model_file:
                return None
            extra = tuple(desc.get("additional_files") or ())
            # Offline, fastembed looks in this directory for every catalog
            # entry, with or without a ``url`` (``download_model``: ``if
            # url_source or local_files_only``). It names the directory after
            # the catalog's own model id, not the (case-insensitively matched)
            # id asked for.
            gcs_dir = _gcs_dir_name(str(desc["model"]), bool(sources.get("_deprecated_tar_struct")))
            return _CacheSpec(hf, None, (model_file, *extra), gcs_dir)
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
    ``model.onnx``. With ``HF_HUB_OFFLINE`` set, a catalog model is also
    present when the directory fastembed unpacks its GCS tarball to is
    complete (#2594); pinned models never reach it. Only the default fp32
    load is described; an int8 variant loads from its local artifact path,
    which this probe does not see. See the module docstring for the cases
    where this differs from what fastembed loads.

    Filesystem and catalog errors propagate; the readiness endpoint catches
    them and reports the cache as absent.
    """
    spec = _cache_spec(model_id)
    if _hf_snapshot_present(cache_dir, spec):
        return True
    if spec.gcs_dir is None or not _hf_offline():
        return False
    # fastembed unpacks into ``tmp/`` and renames the finished directory into
    # place, so a partial download never shows up under this name.
    return _snapshot_complete(cache_dir / spec.gcs_dir, spec.model_files)


def _hf_offline() -> bool:
    """``HF_HUB_OFFLINE`` parsed the way ``ModelManagement.download_model`` does."""
    return os.environ.get("HF_HUB_OFFLINE", "").strip().upper() in {"1", "TRUE", "YES", "ON"}


def _hf_snapshot_present(cache_dir: Path, spec: _CacheSpec) -> bool:
    base = cache_dir / ("models--" + spec.repo.replace("/", "--")) / "snapshots"
    if spec.revision is not None:
        return _snapshot_complete(base / spec.revision, spec.model_files)
    if not base.is_dir():
        return False
    return any(_snapshot_complete(snap, spec.model_files) for snap in base.iterdir())
