"""Deterministic fingerprints for retrieval profiles, corpus, and index (#1802).

The Quality Lab compares two replay reports only when they describe the *same*
retrieval conditions. These fingerprints make "same conditions" checkable:

- :func:`profile_fingerprint` — the ranking knobs (what the profile *is*).
- :func:`corpus_fingerprint` — the retrieval-visible state of every chunk.
- :func:`index_fingerprint` — corpus state plus the derived index artifacts
  (vectors, FTS rows, link topology, access counters) that ranking also reads.
- :func:`case_set_fingerprint` — the identity of the labeled cases themselves.

Two invariants shape this module:

1. **Secrets never enter a fingerprint.** The profile fingerprint is built from
   an explicit allowlist of non-secret fields, never a generic ``model_dump``:
   ``api_key`` (Embedding/Rerank/LLM) and other credentials are simply never
   read. A sentinel-key test scans the returned dict to enforce this.
2. **Multiplicity is preserved.** The corpus fingerprint hashes a sorted
   *sequence* of per-chunk tuples, not a set — two chunks sharing a
   ``content_hash`` (identical content in different files) both count, so
   losing or duplicating a chunk registers as drift.

All hashing goes through :func:`_sha256_json`: ``json.dumps`` with
``sort_keys=True``/``ensure_ascii=False``/compact separators, the same canonical
shape the pipeline uses for ``profile_id`` and ``tools/retrieval-eval`` uses for
its ``*_sha256`` manifest keys. O(corpus) reads are acceptable — fingerprints are
computed only inside explicit ``mm quality`` commands, never on the search path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from memtomem.config import Mem2MemConfig, embedding_policy_fingerprint

__all__ = [
    "profile_fingerprint",
    "corpus_fingerprint",
    "index_fingerprint",
    "case_set_fingerprint",
]


def _sha256_json(value: Any) -> str:
    """Canonical sha256 over a JSON-serializable value."""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tags_hash(tags_json: str | None) -> str:
    """Order-independent hash of a chunk's tags (stored as a JSON list)."""
    try:
        tags = json.loads(tags_json) if tags_json else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    if not isinstance(tags, list):
        tags = []
    return _sha256_json(sorted(str(t) for t in tags))


def _stage(active: bool, params: dict[str, Any]) -> dict[str, Any]:
    """A ranking-stage block: its enable flag plus its tuning params *only when
    active*.

    A disabled stage does not affect ranking, so its unused parameters must not
    enter the fingerprint — otherwise two profiles that both disable the stage
    but differ in its dead parameters (e.g. a different reranker model while
    rerank is off) would be flagged as different retrieval conditions. The
    ``enabled`` flag itself is always present so on↔off is always drift.
    """
    return {"enabled": active, **(params if active else {})}


def profile_fingerprint(config: Mem2MemConfig) -> tuple[str, dict[str, Any]]:
    """Return ``(sha256, knob_dict)`` for the ranking-affecting configuration.

    ``knob_dict`` is what reports display for knob-level diffs. It is built from
    an explicit allowlist so no secret (``api_key`` and friends) can reach it —
    LLM/reranker identity is provider+model only, never credentials or base_url —
    and inactive stages contribute only their enable flag (see :func:`_stage`).
    """
    search = config.search
    qe = config.query_expansion
    qe_params: dict[str, Any] = {"max_terms": qe.max_terms, "strategy": qe.strategy}
    # LLM identity matters only when expansion is on AND actually calls an LLM.
    if qe.strategy == "llm":
        qe_params["llm"] = {"provider": config.llm.provider, "model": config.llm.model}

    ss = config.session_summary
    rescue_active = ss.expansion_lookup_top_k > 0
    knobs: dict[str, Any] = {
        "search": {
            "default_top_k": search.default_top_k,
            "bm25_candidates": search.bm25_candidates,
            "dense_candidates": search.dense_candidates,
            "rrf_k": search.rrf_k,
            "enable_bm25": search.enable_bm25,
            "enable_dense": search.enable_dense,
            "tokenizer": search.tokenizer,
            "rrf_weights": list(search.rrf_weights),
            "system_namespace_prefixes": list(search.system_namespace_prefixes),
            # cache_ttl deliberately excluded: caching never changes ranking.
        },
        "decay": _stage(config.decay.enabled, {"half_life_days": config.decay.half_life_days}),
        # MMR diversifies over dense vectors; the pipeline skips it entirely when
        # dense retrieval is off (search/pipeline.py), so lambda_param is dead
        # then — activity is `mmr.enabled AND enable_dense`.
        "mmr": _stage(
            config.mmr.enabled and search.enable_dense,
            {"lambda_param": config.mmr.lambda_param},
        ),
        "access": _stage(config.access.enabled, {"max_boost": config.access.max_boost}),
        "importance": _stage(
            config.importance.enabled,
            {"max_boost": config.importance.max_boost, "weights": list(config.importance.weights)},
        ),
        "context_window": _stage(
            config.context_window.enabled, {"window_size": config.context_window.window_size}
        ),
        "rerank": _stage(
            config.rerank.enabled,
            {
                "provider": config.rerank.provider,
                "model": config.rerank.model,
                "oversample": config.rerank.oversample,
                "min_pool": config.rerank.min_pool,
                "max_pool": config.rerank.max_pool,
                # api_key and legacy top_k excluded.
            },
        ),
        "query_expansion": _stage(qe.enabled, qe_params),
        # The rescue leg is gated by expansion_lookup_top_k>0 (search/pipeline.py),
        # not by session_summary.auto (which governs summary *generation* at
        # session end — a write-time concern whose output chunks the corpus
        # fingerprint already captures). Include the gate always; its tuning
        # params only when the leg is live.
        "session_summary": {
            "expansion_lookup_top_k": ss.expansion_lookup_top_k,
            **(
                {
                    "expansion_score_threshold": ss.expansion_score_threshold,
                    "expansion_rescue_weight": ss.expansion_rescue_weight,
                }
                if rescue_active
                else {}
            ),
        },
        "embedding": {
            "provider": config.embedding.provider,
            "model": config.embedding.model,
            "dimension": config.embedding.dimension,
            "policy_fingerprint": embedding_policy_fingerprint(config.embedding),
            # base_url and api_key excluded.
        },
    }
    return _sha256_json(knobs), knobs


def _chunk_identity(
    content_hash: Any, namespace: Any, source_file: Any, start_line: Any
) -> list[Any]:
    """Durable, privacy-safe identity for one chunk.

    ``chunks.id`` is a fresh uuid4 per index run and the sqlite ``rowid`` is
    storage-local, so neither can key derived-index artifacts without producing
    false drift on a rebuild. This tuple — content hash plus namespace plus
    hashed source path plus start line — is stable across re-indexing and
    disambiguates chunks that share a ``content_hash`` (identical content in
    different files/positions). ``namespace`` is part of the key because the same
    ``(content_hash, source_file, start_line)`` can legitimately recur under two
    namespaces; without it a vector/FTS swap between those rows would not
    register as drift.
    """
    return [content_hash, namespace, _sha256_text(str(source_file or "")), start_line]


def _corpus_tuple(row: Sequence[Any]) -> list[Any]:
    """One chunk's retrieval-state tuple from a ``read_corpus_fingerprint_rows`` row.

    Row order: content_hash, heading_hierarchy, namespace, scope, created_at,
    updated_at, valid_from_unix, valid_to_unix, importance_score, tags,
    source_file, start_line, project_root.
    """
    (
        content_hash,
        heading_hierarchy,
        namespace,
        scope,
        created_at,
        updated_at,
        valid_from_unix,
        valid_to_unix,
        importance_score,
        tags,
        source_file,
        start_line,
        project_root,
    ) = row
    return [
        content_hash,
        # retrieval_text identity: content_hash covers the body; headings change
        # the embedded retrieval text without changing content_hash, so fold them in.
        _sha256_text(f"{heading_hierarchy or ''}\x00{content_hash}"),
        namespace,
        scope,
        created_at,
        updated_at,
        valid_from_unix,
        valid_to_unix,
        importance_score,
        _tags_hash(tags),
        # normalized path is privacy-sensitive: hash it, never store it raw.
        _sha256_text(source_file or ""),
        start_line,
        # scope_context_sql gates project-tier retrievability on project_root, so
        # the same row in a different project is a different retrieval state.
        _sha256_text(str(project_root or "")),
    ]


def corpus_fingerprint(corpus_rows: Sequence[Sequence[Any]]) -> str:
    """Multiplicity-preserving hash of every chunk's retrieval-visible state."""
    tuples = sorted(_corpus_tuple(row) for row in corpus_rows)
    return _sha256_json(tuples)


def index_fingerprint(
    corpus_rows: Sequence[Sequence[Any]],
    vector_rows: Sequence[Sequence[Any]],
    fts_rows: Sequence[Sequence[Any]],
    embedding_info: dict[str, Any],
    *,
    link_rows: Sequence[Sequence[Any]] | None = None,
    access_rows: Sequence[Sequence[Any]] | None = None,
) -> str:
    """Hash the corpus state plus the derived index artifacts ranking reads.

    Each vector and FTS row is keyed by the **durable** chunk identity
    (:func:`_chunk_identity` — content hash + namespace + hashed source + start
    line), not the storage-local rowid/uuid. So a vector that detaches from its
    content (a vector swap between two rows) registers as drift, while a rebuilt
    index whose rows carry the same content and vectors under new rowids/uuids
    does not. Both are multiplicity-preserving.

    ``vector_rows`` = ``(content_hash, namespace, source_file, start_line,
    embedding_blob)``; ``fts_rows`` = ``(content_hash, namespace, source_file,
    start_line, fts_content)``.

    ``link_rows`` should be passed only when session-summary rescue is enabled,
    and ``access_rows`` only when access or importance boost is enabled — those
    are retrieval inputs only under those configs, so folding them in
    unconditionally would flag drift the ranking never sees.
    """
    vectors = sorted(
        _chunk_identity(content_hash, namespace, source, start_line) + [_sha256_text_bytes(blob)]
        for content_hash, namespace, source, start_line, blob in vector_rows
    )
    fts = sorted(
        _chunk_identity(content_hash, namespace, source, start_line) + [_sha256_text(content or "")]
        for content_hash, namespace, source, start_line, content in fts_rows
    )
    payload: dict[str, Any] = {
        "corpus": corpus_fingerprint(corpus_rows),
        "embedding": {
            "provider": embedding_info.get("provider"),
            "model": embedding_info.get("model"),
            "dimension": embedding_info.get("dimension"),
            "policy_fingerprint": embedding_info.get("policy_fingerprint"),
            "max_sequence_tokens": embedding_info.get("max_sequence_tokens"),
        },
        "vectors": vectors,
        "fts": fts,
    }
    if link_rows is not None:
        # Endpoints arrive as durable identities (nullable source columns); coerce
        # every field to str so None never breaks the sort (None vs str is
        # unorderable in Python 3) while a null-vs-present source still differs.
        payload["links"] = sorted(["" if v is None else str(v) for v in row] for row in link_rows)
    if access_rows is not None:
        # Rows are (content_hash, namespace, source_file, start_line, count) —
        # the full durable identity, so a count swap between duplicate-content
        # chunks is drift. Coerce to str for a total, None-safe sort.
        payload["access"] = sorted(
            ["" if v is None else str(v) for v in row] for row in access_rows
        )
    return _sha256_json(payload)


def _sha256_text_bytes(blob: Any) -> str:
    """sha256 of a raw vector blob (bytes / memoryview / None)."""
    if blob is None:
        data = b""
    elif isinstance(blob, memoryview):
        data = blob.tobytes()
    elif isinstance(blob, (bytes, bytearray)):
        data = bytes(blob)
    else:
        data = str(blob).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def case_set_fingerprint(cases: Sequence[dict[str, Any]]) -> str:
    """Hash the identity of a set of eval cases.

    A label edit or a query/top_k/filter change bumps this, so a comparison can
    tell "the profile moved the numbers" apart from "the cases changed".
    """

    # Coerce every ordering field to a None-safe string: stored cases always
    # carry a case_id, but a hand-built dict (or a partially-populated one from a
    # future caller) could mix None and str at the same position, which is
    # unorderable in Python 3 and would TypeError the outer sort. A dict is
    # likewise unorderable, so filters is pre-hashed to a stable string.
    def _ns(x: Any) -> str:
        return "" if x is None else str(x)

    canonical = sorted(
        [
            _ns(case.get("case_id")),
            _ns(case.get("version")),
            _ns(case.get("query_text")),
            _ns(case.get("top_k")),
            _sha256_json(case.get("filters") or {}),
            sorted(
                [_ns(lab.get("content_hash")), _ns(lab.get("judgment"))]
                for lab in case.get("labels", [])
            ),
        ]
        for case in cases
    )
    return _sha256_json(canonical)
