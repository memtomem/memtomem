#!/usr/bin/env python3
"""Compare the public synthetic retrieval benchmark with a committed baseline."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

DEFAULT_BASELINE = Path(__file__).with_name("baseline_v0.3.8.json")


def _load_calibrator() -> ModuleType:
    path = Path(__file__).with_name("calibrate_portfolio.py")
    spec = importlib.util.spec_from_file_location("retrieval_baseline_calibrator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def compare(
    report: dict,
    baseline: dict,
    *,
    check_performance: bool = False,
    check_fingerprint: bool = False,
) -> list[str]:
    failures: list[str] = []
    for field in ("corpus_sha256", "query_sha256"):
        expected = baseline["corpus"][field]
        observed = report["corpus"][field]
        if observed != expected:
            failures.append(f"{field} changed: expected {expected}, observed {observed}")
    if check_fingerprint:
        expected_fingerprint = baseline["embedding"]["vector_fingerprint_sha256"]
        observed_fingerprint = report["embedding"]["vector_fingerprint_sha256"]
        if observed_fingerprint != expected_fingerprint:
            failures.append(
                "embedding behavior fingerprint changed: "
                f"expected {expected_fingerprint}, observed {observed_fingerprint}"
            )
    for key, floor in baseline["floors"].items():
        observed = report["aggregate_means"][key]
        if observed < floor:
            failures.append(f"quality floor failed for {key}: floor {floor}, observed {observed}")
    if check_performance:
        for key in ("p50", "p95"):
            ceiling = baseline["latency_ms"][key] * 1.20
            observed = report["latency_ms"][key]
            if observed > ceiling:
                failures.append(
                    f"latency regression for {key}: ceiling {ceiling:.3f}, observed {observed}"
                )
        size_ceiling = baseline["index_stats"]["db_size_bytes"] * 1.20
        observed_size = report["index_stats"]["db_size_bytes"]
        if observed_size > size_ceiling:
            failures.append(
                f"DB size regression: ceiling {size_ceiling:.0f}, observed {observed_size}"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--check-performance", action="store_true")
    parser.add_argument(
        "--check-fingerprint",
        action="store_true",
        help="require the platform-specific embedding vector fingerprint to match",
    )
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    report = asyncio.run(_load_calibrator().calibrate(runs=args.runs, factor=baseline["factor"]))
    failures = compare(
        report,
        baseline,
        check_performance=args.check_performance,
        check_fingerprint=args.check_fingerprint,
    )
    if failures:
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    print(
        f"retrieval baseline passed ({args.runs} run(s), "
        f"{report['corpus']['files']} files/{report['corpus']['chunks']} chunks)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
