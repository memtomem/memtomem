#!/usr/bin/env python3
"""Compare reranking and BGE-M3 on the language-separated v2 holdout."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import Any

MULTILINGUAL_RERANKER = "jinaai/jina-reranker-v2-base-multilingual"
BGE_M3_MODELS = {
    "english": ("BAAI/bge-m3", 1024),
    "korean": ("BAAI/bge-m3", 1024),
    "cross_language": ("BAAI/bge-m3", 1024),
}


def _load_benchmark() -> Any:
    path = Path(__file__).with_name("benchmark_v2.py")
    spec = importlib.util.spec_from_file_location("retrieval_model_compare_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _macro(track: dict[str, Any], metric: str) -> float:
    values = [
        float(value) for key, value in track["aggregate"].items() if key.endswith(f"|{metric}")
    ]
    return statistics.fmean(values)


def _profile_summary(report: dict[str, Any]) -> dict[str, Any]:
    tracks: dict[str, Any] = {}
    for name, track in report["tracks"].items():
        tracks[name] = {
            "embedding": track["embedding"],
            "reranker": track["reranker"],
            "zero_hit_count": track["zero_hit_count"],
            "latency_ms": track["latency_ms"],
            "macro": {
                metric: round(_macro(track, metric), 6)
                for metric in ("recall@10", "mrr@10", "ndcg@10")
            },
            "aggregate": track["aggregate"],
        }
    return {"search": report["search"], "tracks": tracks}


def _delta(candidate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
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
                for metric in ("recall@10", "mrr@10", "ndcg@10")
            },
            "zero_hit_count": (candidate_track["zero_hit_count"] - control_track["zero_hit_count"]),
            "p95_latency_ms": round(
                candidate_track["latency_ms"]["p95"] - control_track["latency_ms"]["p95"],
                3,
            ),
            "slices": {
                key: round(float(value) - float(control_track["aggregate"].get(key, 0.0)), 6)
                for key, value in candidate_track["aggregate"].items()
                if key in control_track["aggregate"]
            },
        }
    return result


async def compare(*, runs: int = 1, reranker_pool: int = 20) -> dict[str, Any]:
    benchmark = _load_benchmark()
    raw = {
        "language_specific": await benchmark.benchmark(runs=runs),
        "language_specific_reranked": await benchmark.benchmark(
            runs=runs,
            reranker_model=MULTILINGUAL_RERANKER,
            reranker_pool=reranker_pool,
        ),
        "bge_m3": await benchmark.benchmark(
            runs=runs,
            embedding_models=BGE_M3_MODELS,
        ),
        "bge_m3_reranked": await benchmark.benchmark(
            runs=runs,
            embedding_models=BGE_M3_MODELS,
            reranker_model=MULTILINGUAL_RERANKER,
            reranker_pool=reranker_pool,
        ),
    }
    profiles = {name: _profile_summary(report) for name, report in raw.items()}
    return {
        "schema_version": 1,
        "methodology": "retrieval-v2-model-reranker-comparison",
        "runs": runs,
        "queries": raw["language_specific"]["portfolio"]["queries"],
        "reranker": {"model": MULTILINGUAL_RERANKER, "pool": reranker_pool},
        "profiles": profiles,
        "deltas": {
            "reranker_on_language_specific": _delta(
                profiles["language_specific_reranked"],
                profiles["language_specific"],
            ),
            "bge_m3_vs_language_specific": _delta(
                profiles["bge_m3"], profiles["language_specific"]
            ),
            "reranker_on_bge_m3": _delta(profiles["bge_m3_reranked"], profiles["bge_m3"]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--reranker-pool", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(compare(runs=args.runs, reranker_pool=args.reranker_pool))
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({report['queries']} queries, {report['runs']} run(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
