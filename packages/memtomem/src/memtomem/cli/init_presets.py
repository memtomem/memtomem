"""Preset specs for the ``mm init`` quick-setup flow.

The preset picker (new default path in ``mm init``) lets users pick a
pre-configured bundle of embedding / reranker / tokenizer / namespace
values instead of stepping through the full 10-step wizard. The advanced
path remains available via the picker's ``Advanced`` entry or
``mm init --advanced``.

Presets are branded product defaults — not user-injected fragments — so
they live in source (typed, mypy-checkable, pinned via ``_VALID_PRESETS``)
rather than in ``~/.memtomem/config.d/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

PresetName = Literal["minimal", "english", "korean"]

# Derived from ``PresetName`` — do NOT edit independently. Pattern mirrors
# ``_VALID_PROVIDER_CATEGORIES`` in ``config.py:1062``. Adding a preset
# means adding it to the ``Literal`` above; the frozenset and the pin test
# ``test_valid_presets_frozenset_matches_literal`` pick it up for free.
_VALID_PRESETS: frozenset[str] = frozenset(get_args(PresetName))


@dataclass(frozen=True)
class PresetSpec:
    """Bundled defaults applied by a single preset selection.

    Every field maps directly onto the flat state keys written by the
    existing ``_step_*`` functions, so ``_apply_preset`` is a simple
    dict-populate pass through the same ``_write_config_and_summary``
    merge path the interactive wizard uses.
    """

    label: str
    description: str
    provider: str
    model: str
    dimension: int
    rerank_enabled: bool
    rerank_model: str | None
    tokenizer: str
    default_top_k: int
    enable_auto_ns: bool
    default_namespace: str
    decay_enabled: bool
    autodetect_providers: bool


PRESETS: dict[PresetName, PresetSpec] = {
    "minimal": PresetSpec(
        label="Minimal",
        description="BM25 keyword search — no downloads, no dependencies",
        provider="none",
        model="",
        dimension=0,
        rerank_enabled=False,
        rerank_model=None,
        tokenizer="unicode61",
        default_top_k=10,
        enable_auto_ns=False,
        default_namespace="default",
        decay_enabled=False,
        autodetect_providers=False,
    ),
    "english": PresetSpec(
        label="English (Recommended)",
        description="ONNX bge-small-en-v1.5 + English rerank + auto-discover providers",
        provider="onnx",
        model="bge-small-en-v1.5",
        dimension=384,
        rerank_enabled=True,
        rerank_model="Xenova/ms-marco-MiniLM-L-6-v2",
        tokenizer="unicode61",
        default_top_k=10,
        enable_auto_ns=True,
        default_namespace="default",
        decay_enabled=False,
        autodetect_providers=True,
    ),
    "korean": PresetSpec(
        label="Korean-optimized",
        description="ONNX bge-m3 + kiwipiepy tokenizer + multilingual rerank",
        provider="onnx",
        model="bge-m3",
        dimension=1024,
        rerank_enabled=True,
        rerank_model="jinaai/jina-reranker-v2-base-multilingual",
        tokenizer="kiwipiepy",
        default_top_k=10,
        enable_auto_ns=True,
        default_namespace="default",
        decay_enabled=False,
        autodetect_providers=True,
    ),
}


def get_preset(name: str) -> PresetSpec:
    """Return the ``PresetSpec`` for ``name`` or raise ``ValueError``.

    ``click.Choice`` already blocks invalid CLI input; this wrapper gives
    programmatic callers (tests, future reuse) an explicit error listing
    the valid names instead of a bare ``KeyError``.
    """
    try:
        return PRESETS[name]  # type: ignore[index]
    except KeyError as e:
        raise ValueError(f"unknown preset: {name!r}; valid: {sorted(_VALID_PRESETS)}") from e
