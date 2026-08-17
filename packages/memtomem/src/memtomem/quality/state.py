"""Live retrieval-state fingerprints + determinism classification (#1802).

This bridges the storage fingerprint *readers* (SQL) to the pure hashing policy
in :mod:`memtomem.quality.fingerprints`, applying the per-dependency gating the
``index_fingerprint`` contract requires (link rows only when session-summary
rescue is live; access rows only when an access/importance boost reads them).

It also classifies whether a profile can replay *deterministically*. Replay
(:func:`memtomem.search.pipeline.SearchPipeline.search` with ``record=False``)
bypasses the expansion cache and re-runs every stage, so a stage backed by a
remote/nondeterministic service produces a different ranking each run. Such a
profile still replays, but its report is flagged and excluded from the
byte-determinism acceptance criterion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memtomem.quality.fingerprints import (
    corpus_fingerprint,
    index_fingerprint,
    profile_fingerprint,
)

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.sqlite_backend import SqliteBackend

__all__ = ["current_fingerprints", "nondeterministic_stages"]

#: Embedding providers whose vectors come from a network service — a replay
#: leg that calls one is not byte-reproducible. ``none``/``onnx`` are local.
_REMOTE_EMBEDDING_PROVIDERS = frozenset({"ollama", "openai"})

#: Stand-in written over an entity row's confidence once it has passed this
#: config's ``min_confidence`` gate. The entity boost is presence-only, so two
#: rows that both clear the floor rank identically whatever their stored values.
_GATED_CONFIDENCE = "gated"


def current_fingerprints(
    storage: SqliteBackend, config: Mem2MemConfig
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return ``({"profile","corpus","index"}, profile_knobs)`` for live state.

    Synchronous: the readers are plain SQL over the corpus and this runs only
    from explicit quality commands, never the interactive hot path. The knob
    dict is the allowlisted, secret-free display view from
    :func:`profile_fingerprint`.
    """
    profile_fp, knobs = profile_fingerprint(config)

    corpus_rows = storage.read_corpus_fingerprint_rows()
    vector_rows = storage.read_vector_fingerprint_rows()
    fts_rows = storage.read_fts_fingerprint_rows()
    embedding_info = storage.stored_embedding_info

    # Gating per the index_fingerprint contract: fold in a dependency only when
    # the ranking actually reads it, else an equivalent rebuild would false-drift.
    link_rows = (
        storage.read_link_topology_rows()
        if config.session_summary.expansion_lookup_top_k > 0
        else None
    )
    access_rows = (
        storage.read_access_counts()
        if (config.access.enabled or config.importance.enabled)
        else None
    )
    # Narrow to the rows this config's boost can actually read: the stage only
    # matches the active ``query_entity_types`` and ignores anything below
    # ``min_confidence``, so folding the rest in would report drift the ranking
    # never sees (the same rule as the presence gating above, one level finer).
    entity_rows = None
    if config.entity_boost.enabled:
        active_types = set(config.entity_boost.query_entity_types)
        floor = config.entity_boost.min_confidence
        # Rows that survive the gate are equivalent to this config regardless of
        # how far above the floor they sit — the boost is presence-only — so the
        # value is normalized away here rather than in the hasher, which stays
        # honest for callers (``experiment._shared_snapshot``) that need to see
        # a raw confidence change.
        entity_rows = [
            (*row[:6], _GATED_CONFIDENCE)
            for row in storage.read_entity_rows()
            if row[4] in active_types and (row[6] or 0.0) >= floor
        ]

    index_fp = index_fingerprint(
        corpus_rows,
        vector_rows,
        fts_rows,
        embedding_info,
        link_rows=link_rows,
        access_rows=access_rows,
        entity_rows=entity_rows,
    )
    return (
        {"profile": profile_fp, "corpus": corpus_fingerprint(corpus_rows), "index": index_fp},
        knobs,
    )


def nondeterministic_stages(config: Mem2MemConfig, pipeline: SearchPipeline) -> list[str]:
    """Return the sorted list of *effectively active* nondeterministic stages.

    "Effectively active" reads the pipeline's actual runtime components, not
    config knobs alone: an ``strategy="llm"`` expansion with no LLM provider
    wired never calls an LLM, and a ``cohere`` reranker only runs when a reranker
    is actually attached. An empty list means the profile replays deterministically.
    """
    stages: list[str] = []

    qe = config.query_expansion
    qe_enabled = bool(getattr(qe, "enabled", False))
    strategy = getattr(qe, "strategy", "tags")

    # LLM expansion re-calls the model on every replay (record=False bypasses the
    # expansion cache), but only when a provider is actually wired.
    if qe_enabled and strategy == "llm" and pipeline.llm_provider is not None:
        stages.append("query_expansion_llm")

    # A remote reranker (Cohere) is a network call; only counts when a reranker
    # is actually attached to the active generation (``rerank_active``).
    rerank = config.rerank
    if rerank.enabled and rerank.provider == "cohere" and pipeline.rerank_active:
        stages.append("rerank_remote")

    # A remote embedder is reached by ANY effectively-active embedding consumer:
    # the primary dense leg (config + no embedding mismatch) OR heading/both
    # query expansion, which embeds independently of the primary leg.
    if config.embedding.provider in _REMOTE_EMBEDDING_PROVIDERS:
        mismatch = getattr(pipeline.storage, "embedding_mismatch", None)
        primary_dense = config.search.enable_dense and not isinstance(mismatch, dict)
        heading_expansion = qe_enabled and strategy in ("headings", "both")
        if primary_dense or heading_expansion:
            stages.append("embedding_remote")

    return sorted(stages)
