#!/usr/bin/env python3
"""Evaluate the preregistered RRF grid and select a v2 benchmark baseline."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

CANDIDATES = ((1.0, 0.0), (2.0, 1.0), (1.5, 1.0), (1.0, 1.0), (1.0, 1.5), (0.0, 1.0))
CONTROL = (1.0, 1.0)
WEAK_METRICS = (
    ("genre_primary", "genre_hit@1"),
    ("negation", "constraint_success@10"),
    ("paraphrase", "ndcg@10"),
)
GENERIC_METRICS = ("recall@10", "mrr@10", "ndcg@10")


def _load_benchmark() -> Any:
    path = Path(__file__).with_name("benchmark_v2.py")
    spec = importlib.util.spec_from_file_location("retrieval_benchmark_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _matching_values(report: dict[str, Any], query_type: str, metric: str) -> list[float]:
    needle = f"|{query_type}|{metric}"
    return [
        float(value)
        for track in report["tracks"].values()
        for key, value in track["aggregate"].items()
        if key.endswith(needle)
    ]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _macro(report: dict[str, Any], metric: str) -> float:
    return _mean(
        [
            float(value)
            for track in report["tracks"].values()
            for key, value in track["aggregate"].items()
            if key.endswith(f"|{metric}")
        ]
    )


def _zero_hits(report: dict[str, Any]) -> int:
    return sum(int(track["zero_hit_count"]) for track in report["tracks"].values())


def assess_candidate(candidate: dict[str, Any], control: dict[str, Any]) -> dict[str, Any]:
    weak_deltas = {
        f"{query_type}|{metric}": round(
            _mean(_matching_values(candidate, query_type, metric))
            - _mean(_matching_values(control, query_type, metric)),
            6,
        )
        for query_type, metric in WEAK_METRICS
    }
    slice_regressions: list[str] = []
    for track_name, control_track in control["tracks"].items():
        candidate_track = candidate["tracks"][track_name]
        for key, control_value in control_track["aggregate"].items():
            if not key.endswith(tuple(f"|{metric}" for metric in GENERIC_METRICS)):
                continue
            observed = candidate_track["aggregate"].get(key)
            if observed is None or float(observed) < float(control_value) - 0.02:
                slice_regressions.append(
                    f"{track_name}|{key}: {observed} < {float(control_value) - 0.02:.6f}"
                )

    macro_deltas = {
        metric: round(_macro(candidate, metric) - _macro(control, metric), 6)
        for metric in GENERIC_METRICS
    }
    latency_regressions = [
        track_name
        for track_name, control_track in control["tracks"].items()
        if candidate["tracks"][track_name]["latency_ms"]["p95"]
        > control_track["latency_ms"]["p95"] * 1.20
    ]
    zero_hit_cap = math.floor(_zero_hits(control) * 0.80)
    failures: list[str] = []
    if not all(delta > 0 for delta in weak_deltas.values()):
        failures.append("every weak metric must improve")
    if _mean(list(weak_deltas.values())) < 0.03:
        failures.append("mean weak-metric improvement is below 0.03")
    if slice_regressions:
        failures.append("one or more language/type slices regress by more than 0.02")
    if any(delta < 0 for delta in macro_deltas.values()):
        failures.append("macro Recall/MRR/nDCG regression")
    if _zero_hits(candidate) > zero_hit_cap:
        failures.append(f"zero-hit count exceeds {zero_hit_cap}")
    if latency_regressions:
        failures.append("p95 latency regression exceeds 20%")
    return {
        "eligible": not failures,
        "failures": failures,
        "weak_deltas": weak_deltas,
        "minimum_weak_delta": min(weak_deltas.values()),
        "mean_weak_delta": round(_mean(list(weak_deltas.values())), 6),
        "macro_deltas": macro_deltas,
        "zero_hit_count": _zero_hits(candidate),
        "zero_hit_cap": zero_hit_cap,
        "slice_regressions": slice_regressions,
        "latency_regressions": latency_regressions,
    }


def _selection_key(item: tuple[tuple[float, float], dict[str, Any], dict[str, Any]]) -> tuple:
    weights, report, assessment = item
    ndcg = _macro(report, "ndcg@10")
    p95 = max(track["latency_ms"]["p95"] for track in report["tracks"].values())
    distance = abs(weights[0] - 1.0) + abs(weights[1] - 1.0)
    return (assessment["minimum_weak_delta"], ndcg, -p95, -distance)


def _compact(report: dict[str, Any]) -> dict[str, Any]:
    compact = json.loads(json.dumps(report))
    for track in compact["tracks"].values():
        for row in track["per_query"]:
            row.pop("retrieved", None)
    return compact


async def tune(*, baseline_runs: int = 10) -> dict[str, Any]:
    benchmark_module = _load_benchmark()
    reports: dict[tuple[float, float], dict[str, Any]] = {}
    for weights in CANDIDATES:
        reports[weights] = await benchmark_module.benchmark(weights, runs=1)
    control = reports[CONTROL]
    assessed = [
        (weights, report, assess_candidate(report, control))
        for weights, report in reports.items()
        if weights != CONTROL
    ]
    eligible = [item for item in assessed if item[2]["eligible"]]
    winner = max(eligible, key=_selection_key) if eligible else (CONTROL, control, None)
    selected_weights, selected_report, selected_assessment = winner
    calibrated_report = await benchmark_module.benchmark(selected_weights, runs=baseline_runs)
    selected = _compact(calibrated_report)
    selected["selection"] = {
        "control_weights": list(CONTROL),
        "selected_weights": list(selected_weights),
        "changed_default": selected_weights != CONTROL,
        "selected_assessment": selected_assessment,
        "candidates": {
            f"{weights[0]:g},{weights[1]:g}": assessment
            for weights, _report, assessment in assessed
        },
    }
    selected["quality_floors"] = {
        track_name: {
            # 90% of the multi-run mean, then subtract the track's worst
            # run-to-run spread so a single-run CI check (check_baseline_v2 runs
            # --runs 1) cannot dip below the floor on a high-variance track:
            # Korean MiniLM spreads ~0.07 from tie-break nondeterminism while
            # English bge-small spreads 0.0. Clamped at 0.
            key: max(
                0.0,
                round(float(value) * 0.90 - float(track.get("max_run_spread", 0.0) or 0.0), 6),
            )
            for key, value in track["aggregate"].items()
        }
        for track_name, track in selected["tracks"].items()
    }
    selected["zero_hit_caps"] = {
        track_name: int(track["zero_hit_count"]) for track_name, track in selected["tracks"].items()
    }
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-runs", type=int, default=10)
    args = parser.parse_args()
    report = asyncio.run(tune(baseline_runs=args.baseline_runs))
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    selected = report["selection"]["selected_weights"]
    changed = report["selection"]["changed_default"]
    print(f"selected RRF weights {selected} (default changed: {changed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
