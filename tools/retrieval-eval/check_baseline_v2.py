#!/usr/bin/env python3
"""Check the language-separated retrieval benchmark against its v2 baseline."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_BASELINE = Path(__file__).with_name("baseline_v2.json")


def _load_benchmark() -> Any:
    path = Path(__file__).with_name("benchmark_v2.py")
    spec = importlib.util.spec_from_file_location("retrieval_check_benchmark_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def compare(
    report: dict[str, Any],
    baseline: dict[str, Any],
    *,
    check_performance: bool = False,
) -> list[str]:
    failures: list[str] = []
    for field in ("schema_version", "methodology"):
        if report.get(field) != baseline.get(field):
            failures.append(
                f"{field} changed: expected {baseline.get(field)!r}, observed {report.get(field)!r}"
            )
    for section, field in (
        ("portfolio", "query_sha256"),
        ("portfolio", "qrel_sha256"),
        ("corpus", "corpus_sha256"),
    ):
        expected = baseline[section][field]
        observed = report[section][field]
        if observed != expected:
            failures.append(f"{section}.{field} changed: expected {expected}, observed {observed}")

    for track_name, baseline_track in baseline["tracks"].items():
        report_track = report["tracks"].get(track_name)
        if report_track is None:
            failures.append(f"missing track: {track_name}")
            continue
        if report_track["embedding"] != baseline_track["embedding"]:
            failures.append(
                f"{track_name} embedding changed: expected {baseline_track['embedding']}, "
                f"observed {report_track['embedding']}"
            )
        for key, floor in baseline["quality_floors"][track_name].items():
            observed = report_track["aggregate"].get(key)
            if observed is None or float(observed) < float(floor):
                failures.append(
                    f"quality floor failed for {track_name}|{key}: "
                    f"floor {floor}, observed {observed}"
                )
        cap = int(baseline["zero_hit_caps"][track_name])
        observed_zero = int(report_track["zero_hit_count"])
        if observed_zero > cap:
            failures.append(
                f"zero-hit cap failed for {track_name}: cap {cap}, observed {observed_zero}"
            )
        if check_performance:
            ceiling = float(baseline_track["latency_ms"]["p95"]) * 1.20
            observed_p95 = float(report_track["latency_ms"]["p95"])
            if observed_p95 > ceiling:
                failures.append(
                    f"p95 latency failed for {track_name}: "
                    f"ceiling {ceiling:.3f}, observed {observed_p95:.3f}"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--check-performance", action="store_true")
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    weights = tuple(float(value) for value in baseline["search"]["rrf_weights"])
    report = asyncio.run(_load_benchmark().benchmark((weights[0], weights[1]), runs=args.runs))
    failures = compare(report, baseline, check_performance=args.check_performance)
    if failures:
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    print(
        "retrieval v2 baseline passed "
        f"({report['portfolio']['queries']} queries; English, Korean, cross-language)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
