#!/usr/bin/env python3
"""Run the staged retrieval-v2 k sweep without changing product defaults."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import Any

RRF_K_VALUES = (10, 30, 60, 100)
TOP_K_VALUES = (5, 10, 20)
CANDIDATE_K_VALUES = (20, 50, 100)
RERANKER_POOL_VALUES = (10, 20, 50)
CONTROL_RRF_K = 60
CONTROL_CANDIDATE_K = 50
MULTILINGUAL_RERANKER = "jinaai/jina-reranker-v2-base-multilingual"
BGE_M3_MODELS = {
    "english": ("BAAI/bge-m3", 1024),
    "korean": ("BAAI/bge-m3", 1024),
    "cross_language": ("BAAI/bge-m3", 1024),
}


def _load_benchmark() -> Any:
    path = Path(__file__).with_name("benchmark_v2.py")
    spec = importlib.util.spec_from_file_location("retrieval_k_sweep_benchmark_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _macro(track: dict[str, Any], metric: str, top_k: int) -> float:
    suffix = f"|{metric}@{top_k}"
    return _mean(
        [float(value) for key, value in track["aggregate"].items() if key.endswith(suffix)]
    )


def _slice_mean(report: dict[str, Any], suffix: str) -> float:
    return _mean(
        [
            float(value)
            for track in report["tracks"].values()
            for key, value in track["aggregate"].items()
            if key.endswith(suffix)
        ]
    )


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    top_k = int(report["search"]["top_k"])
    return {
        "search": report["search"],
        "tracks": {
            name: {
                "embedding": track["embedding"],
                "reranker": track["reranker"],
                "macro": {
                    metric: round(_macro(track, metric, top_k), 6)
                    for metric in ("recall", "mrr", "ndcg")
                },
                "zero_hit_count": track["zero_hit_count"],
                "latency_ms": track["latency_ms"],
                "aggregate": track["aggregate"],
            }
            for name, track in report["tracks"].items()
        },
    }


def assess(candidate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    """Apply the preregistered language and weak-slice selection gates."""
    top_k = int(candidate["search"]["top_k"])
    if top_k != int(control["search"]["top_k"]):
        raise ValueError("candidate and control top_k must match")
    macro_deltas = {
        track_name: {
            metric: round(
                _macro(candidate["tracks"][track_name], metric, top_k)
                - _macro(control["tracks"][track_name], metric, top_k),
                6,
            )
            for metric in ("recall", "mrr", "ndcg")
        }
        for track_name in control["tracks"]
    }
    zero_hit_deltas = {
        track_name: int(candidate["tracks"][track_name]["zero_hit_count"])
        - int(control["tracks"][track_name]["zero_hit_count"])
        for track_name in control["tracks"]
    }
    weak_suffixes = (
        "|genre_primary|genre_hit@1",
        f"|negation|constraint_success@{top_k}",
        f"|multi_topic|intent_coverage@{top_k}",
    )
    weak_deltas = {
        suffix.lstrip("|"): round(_slice_mean(candidate, suffix) - _slice_mean(control, suffix), 6)
        for suffix in weak_suffixes
    }
    failures: list[str] = []
    if any(macro_deltas["english"][metric] < -0.01 for metric in ("mrr", "ndcg")):
        failures.append("English MRR/nDCG regression exceeds 0.01")
    for track_name in ("korean", "cross_language"):
        if not (macro_deltas[track_name]["ndcg"] > 0 or macro_deltas[track_name]["recall"] > 0):
            failures.append(f"{track_name} does not improve nDCG or Recall")
        if zero_hit_deltas[track_name] > 0:
            failures.append(f"{track_name} worsens zero-hit")
    if any(delta < -0.05 for delta in weak_deltas.values()):
        failures.append("weak-slice regression exceeds 0.05")
    quality_gain = sum(
        macro_deltas[track][metric]
        for track in ("korean", "cross_language")
        for metric in ("recall", "ndcg")
    )
    p95 = max(float(track["latency_ms"]["p95"]) for track in candidate["tracks"].values())
    return {
        "eligible": not failures,
        "failures": failures,
        "macro_deltas": macro_deltas,
        "zero_hit_deltas": zero_hit_deltas,
        "weak_deltas": weak_deltas,
        "quality_gain": round(quality_gain, 6),
        "max_p95_ms": p95,
    }


def select(
    profiles: dict[str, dict[str, Any]], control_name: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    control = profiles[control_name]
    assessments = {
        name: assess(profile, control) for name, profile in profiles.items() if name != control_name
    }
    eligible = [(name, result) for name, result in assessments.items() if result["eligible"]]
    if not eligible:
        return control_name, assessments
    selected = max(
        eligible,
        key=lambda item: (item[1]["quality_gain"], -item[1]["max_p95_ms"]),
    )[0]
    return selected, assessments


async def _run(benchmark: Any, **kwargs: Any) -> dict[str, Any]:
    return _summary(await benchmark.benchmark(**kwargs))


async def sweep(*, runs: int = 1, stage: str = "all") -> dict[str, Any]:
    benchmark = _load_benchmark()
    rrf_profiles = {
        f"rrf_{rrf_k}": await _run(
            benchmark,
            runs=runs,
            top_k=10,
            rrf_k=rrf_k,
            candidate_k=50,
        )
        for rrf_k in RRF_K_VALUES
    }
    selected_rrf_name, rrf_assessments = select(rrf_profiles, f"rrf_{CONTROL_RRF_K}")
    selected_rrf_k = int(selected_rrf_name.rsplit("_", 1)[1])
    result: dict[str, Any] = {
        "schema_version": 1,
        "methodology": "retrieval-v2-staged-k-sweep",
        "runs": runs,
        "stage_1_rrf": {
            "profiles": rrf_profiles,
            "control": f"rrf_{CONTROL_RRF_K}",
            "selected": selected_rrf_name,
            "assessments": rrf_assessments,
        },
    }
    if stage == "rrf":
        return result

    depth_profiles: dict[str, dict[str, Any]] = {}
    depth_selection: dict[str, Any] = {}
    for top_k in TOP_K_VALUES:
        profiles = {
            f"top_{top_k}_candidate_{candidate_k}": await _run(
                benchmark,
                runs=runs,
                top_k=top_k,
                rrf_k=selected_rrf_k,
                candidate_k=candidate_k,
            )
            for candidate_k in CANDIDATE_K_VALUES
        }
        depth_profiles.update(profiles)
        control_name = f"top_{top_k}_candidate_{CONTROL_CANDIDATE_K}"
        selected_name, assessments = select(profiles, control_name)
        depth_selection[str(top_k)] = {
            "control": control_name,
            "selected": selected_name,
            "assessments": assessments,
        }
    result["stage_2_depth_candidates"] = {
        "selected_rrf_k": selected_rrf_k,
        "profiles": depth_profiles,
        "selection_by_top_k": depth_selection,
    }
    if stage == "depth":
        return result

    top_10_selected = depth_selection["10"]["selected"]
    selected_candidate_k = int(top_10_selected.rsplit("_", 1)[1])
    reranker_profiles: dict[str, dict[str, Any]] = {}
    for model_name, embedding_models in (
        ("language_specific", None),
        ("bge_m3", BGE_M3_MODELS),
    ):
        for pool in RERANKER_POOL_VALUES:
            name = f"{model_name}_pool_{pool}"
            reranker_profiles[name] = await _run(
                benchmark,
                runs=runs,
                top_k=10,
                rrf_k=selected_rrf_k,
                candidate_k=selected_candidate_k,
                embedding_models=embedding_models,
                reranker_model=MULTILINGUAL_RERANKER,
                reranker_pool=pool,
            )
    reranker_selection: dict[str, Any] = {}
    for model_name in ("language_specific", "bge_m3"):
        profiles = {
            name: profile
            for name, profile in reranker_profiles.items()
            if name.startswith(f"{model_name}_")
        }
        control_name = f"{model_name}_pool_20"
        selected_name, assessments = select(profiles, control_name)
        reranker_selection[model_name] = {
            "control": control_name,
            "selected": selected_name,
            "assessments": assessments,
        }
    result["stage_3_reranker_pool"] = {
        "selected_rrf_k": selected_rrf_k,
        "selected_candidate_k": selected_candidate_k,
        "profiles": reranker_profiles,
        "selection_by_embedding": reranker_selection,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--stage", choices=("rrf", "depth", "all"), default="all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")
    report = asyncio.run(sweep(runs=args.runs, stage=args.stage))
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output} (stage={args.stage}, runs={args.runs})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
