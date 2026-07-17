#!/usr/bin/env python3
"""Sweep dense-vector dimensions (Matryoshka-style truncation) on the v2 holdout.

Issue #1787 asks whether the default 1024-dim bge-m3 vectors can be truncated to
512/256/128 dims (plus L2-renorm) without materially hurting retrieval quality,
*before* any config surface is designed. bge-m3 is not documented as MRL-trained,
so naive prefix-truncation may degrade recall — this offline benchmark measures it
on the same bilingual v2 holdout the rest of ``tools/retrieval-eval`` uses.

The sweep reuses ``benchmark_v2.benchmark`` unchanged and intercepts the embedder
via a monkeypatch on ``component_factory.create_embedder`` (the same seam
``benchmark_v2._evaluate_track`` uses for ``load_config_overrides``). Each track's
embedder is wrapped in ``TruncatingEmbedder``, which slices every vector to the
configured dimension and re-normalizes to unit L2 (sqlite-vec ranks by L2 distance,
so a raw prefix of a unit vector — which is no longer unit length — would distort
ranking). Native full-dimension vectors are cached across tracks so each unique
text is embedded exactly once for the whole sweep (the bge-m3 model still loads
once per language track before its texts are cached); every later dimension is a
pure truncate-and-renorm over the cache.

Dense-only tracks use RRF weights ``(0.0, 1.0)`` — BM25 documents then score
``0 / (k + rank) = 0`` in fusion, and with ``candidate_k >= top_k`` on this corpus
the top-k is purely dense-ranked, isolating dense-quality loss that the fused
(BM25 + RRF) pipeline may otherwise mask.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import statistics
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# Native output dimensions, used as the per-family control (delta baseline).
BGE_M3_ID = "BAAI/bge-m3"
BGE_M3_NATIVE_DIM = 1024
NOMIC_ID = "nomic-ai/nomic-embed-text-v1.5"
NOMIC_NATIVE_DIM = 768
NOMIC_DIMS = (768, 256)
TRACKS = ("english", "korean", "cross_language")
MODE_WEIGHTS: dict[str, tuple[float, float]] = {
    "fused": (1.0, 1.0),
    "dense": (0.0, 1.0),
}

# Module-level cache of native full-dimension vectors, shared across every track
# and dimension of one sweep. Keyed by ``(resolved_model_id, text)``.
_VECTOR_CACHE: dict[tuple[str, str], list[float]] = {}
_CACHE_STATS: dict[str, int] = {"hits": 0, "misses": 0}


def _load_benchmark() -> Any:
    path = Path(__file__).with_name("benchmark_v2.py")
    spec = importlib.util.spec_from_file_location("retrieval_dimension_sweep_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _truncate_and_renorm(vec: list[float], target_dim: int) -> list[float]:
    """Slice ``vec`` to ``target_dim`` and re-normalize to unit L2.

    A prefix of a unit vector is not itself unit length, so renormalization is
    required for sqlite-vec's L2 ranking to approximate cosine. When ``target_dim``
    is at least the native width this is an exact passthrough, keeping the control
    (native-dimension) run bit-identical to the unwrapped embedder.
    """
    if len(vec) <= target_dim:
        return vec
    head = vec[:target_dim]
    norm = math.sqrt(sum(value * value for value in head))
    if norm == 0.0:
        return head
    return [value / norm for value in head]


class TruncatingEmbedder:
    """Wrap a real embedder; cache native vectors and truncate+renorm to a dim.

    Only ``embed_texts`` / ``embed_query`` are exercised by the benchmark. The
    wrapper deliberately does NOT expose ``supports_input_context`` so the index
    engine uses the plain embed path (path/index metadata is only used for
    truncation-warning logging, irrelevant here).
    """

    def __init__(self, inner: Any, target_dim: int, cache_key: str) -> None:
        self._inner = inner
        self._target_dim = target_dim
        self._cache_key = cache_key

    @property
    def preferred_concurrency(self) -> int:
        # Forward the provider's hint so the index engine keeps sizing its embed
        # semaphore correctly; a dropped hint would allow concurrent ONNX runs
        # and reintroduce the #1783 activation-memory blowup.
        hint = getattr(self._inner, "preferred_concurrency", 1)
        return hint if isinstance(hint, int) and not isinstance(hint, bool) else 1

    @property
    def dimension(self) -> int:
        return self._target_dim

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        **_ignored: Any,
    ) -> list[list[float]]:
        text_list = list(texts)
        uncached = [
            text
            for text in dict.fromkeys(text_list)
            if (self._cache_key, text) not in _VECTOR_CACHE
        ]
        if uncached:
            fresh = await self._inner.embed_texts(uncached)
            for text, vector in zip(uncached, fresh):
                _VECTOR_CACHE[(self._cache_key, text)] = vector
            _CACHE_STATS["misses"] += len(uncached)
        freshly = set(uncached)
        _CACHE_STATS["hits"] += sum(1 for text in text_list if text not in freshly)
        return [
            _truncate_and_renorm(_VECTOR_CACHE[(self._cache_key, text)], self._target_dim)
            for text in text_list
        ]

    async def embed_query(self, query: str) -> list[float]:
        embeddings = await self.embed_texts([query])
        return embeddings[0]

    async def close(self) -> None:
        await self._inner.close()


def _install_embedder_patch() -> Callable[[], None]:
    """Wrap every ``create_embedder`` result in ``TruncatingEmbedder``.

    Returns a restore callable; install once per sweep under ``try/finally``.
    """
    import memtomem.server.component_factory as factory
    from memtomem.embedding.aliases import resolve_embedder_id

    original = factory.create_embedder

    def patched(embedding_config: Any) -> Any:
        inner = original(embedding_config)
        return TruncatingEmbedder(
            inner,
            embedding_config.dimension,
            resolve_embedder_id(embedding_config.model),
        )

    factory.create_embedder = patched
    return lambda: setattr(factory, "create_embedder", original)


def _macro(track: dict[str, Any], metric: str) -> float:
    values = [
        float(value) for key, value in track["aggregate"].items() if key.endswith(f"|{metric}")
    ]
    return statistics.fmean(values)


def _profile_summary(report: dict[str, Any], metrics: tuple[str, ...]) -> dict[str, Any]:
    tracks: dict[str, Any] = {}
    for name, track in report["tracks"].items():
        tracks[name] = {
            "embedding": track["embedding"],
            "reranker": track["reranker"],
            "zero_hit_count": track["zero_hit_count"],
            "latency_ms": track["latency_ms"],
            "macro": {metric: round(_macro(track, metric), 6) for metric in metrics},
            "aggregate": track["aggregate"],
        }
    return {"search": report["search"], "tracks": tracks}


def _delta(
    candidate: dict[str, Any], control: dict[str, Any], metrics: tuple[str, ...]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in control["tracks"]:
        candidate_track = candidate["tracks"][name]
        control_track = control["tracks"][name]
        result[name] = {
            "macro": {
                metric: round(
                    candidate_track["macro"][metric] - control_track["macro"][metric],
                    6,
                )
                for metric in metrics
            },
            "zero_hit_count": (candidate_track["zero_hit_count"] - control_track["zero_hit_count"]),
        }
    return result


async def sweep(
    *,
    dims: tuple[int, ...],
    modes: tuple[str, ...],
    runs: int,
    include_nomic: bool,
    top_k: int,
    rrf_k: int,
    candidate_k: int,
) -> dict[str, Any]:
    benchmark = _load_benchmark()
    metrics = tuple(f"{name}@{top_k}" for name in ("recall", "mrr", "ndcg"))
    families: list[tuple[str, str, tuple[int, ...], int]] = [
        ("bge_m3", BGE_M3_ID, dims, BGE_M3_NATIVE_DIM),
    ]
    if include_nomic:
        families.append(("nomic", NOMIC_ID, NOMIC_DIMS, NOMIC_NATIVE_DIM))

    # Scope the shared native-vector cache to this invocation so a second
    # sweep() in the same process starts clean (no cumulative stats, no reuse
    # of a prior run's vectors).
    _VECTOR_CACHE.clear()
    _CACHE_STATS.update(hits=0, misses=0)

    profiles: dict[str, Any] = {}
    queries = 0
    restore = _install_embedder_patch()
    try:
        for family, model_id, family_dims, _control in families:
            for mode in modes:
                for dim in family_dims:
                    report = await benchmark.benchmark(
                        MODE_WEIGHTS[mode],
                        runs=runs,
                        embedding_models={track: (model_id, dim) for track in TRACKS},
                        top_k=top_k,
                        rrf_k=rrf_k,
                        candidate_k=candidate_k,
                    )
                    queries = report["portfolio"]["queries"]
                    profiles[f"{family}_{mode}_{dim}"] = _profile_summary(report, metrics)
    finally:
        restore()

    deltas: dict[str, Any] = {mode: {} for mode in modes}
    for family, _model_id, family_dims, control in families:
        for mode in modes:
            control_profile = profiles[f"{family}_{mode}_{control}"]
            for dim in family_dims:
                if dim == control:
                    continue
                deltas[mode][f"{family}_{dim}"] = _delta(
                    profiles[f"{family}_{mode}_{dim}"], control_profile, metrics
                )

    return {
        "schema_version": 1,
        "methodology": "retrieval-v2-dimension-sweep",
        "runs": runs,
        "queries": queries,
        "dims": list(dims),
        "modes": list(modes),
        "families": [family for family, *_ in families],
        "search": {"top_k": top_k, "rrf_k": rrf_k, "candidate_k": candidate_k},
        "cache_stats": dict(_CACHE_STATS),
        "profiles": profiles,
        "deltas": deltas,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dims", default="1024,512,256,128", help="bge-m3 dimensions to sweep")
    parser.add_argument("--modes", default="fused,dense", help="fused,dense (RRF weight presets)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--include-nomic",
        action="store_true",
        help="add MRL-native nomic-embed-text-v1.5 tracks (lower bound: no task prefixes)",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    dims = tuple(dict.fromkeys(int(value.strip()) for value in args.dims.split(",")))
    if not dims or any(value <= 0 for value in dims):
        parser.error("--dims requires positive integers")
    if any(value > BGE_M3_NATIVE_DIM for value in dims):
        # Truncation only removes dimensions; a request wider than the model's
        # native output would size a float[N] vec table the embedder cannot fill.
        parser.error(f"--dims cannot exceed the {BGE_M3_NATIVE_DIM}-dim bge-m3 native width")
    if BGE_M3_NATIVE_DIM not in dims:
        parser.error(f"--dims must include the {BGE_M3_NATIVE_DIM}-dim control")
    modes = tuple(value.strip() for value in args.modes.split(","))
    if not modes or any(mode not in MODE_WEIGHTS for mode in modes):
        parser.error(f"--modes must be a subset of {sorted(MODE_WEIGHTS)}")

    report = asyncio.run(
        sweep(
            dims=dims,
            modes=modes,
            runs=args.runs,
            include_nomic=args.include_nomic,
            top_k=args.top_k,
            rrf_k=args.rrf_k,
            candidate_k=args.candidate_k,
        )
    )
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {args.output} ({report['queries']} queries, {report['runs']} run(s), "
        f"cache hits={report['cache_stats']['hits']} misses={report['cache_stats']['misses']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
