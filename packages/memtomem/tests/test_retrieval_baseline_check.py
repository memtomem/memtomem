"""Pure comparison tests for the committed public retrieval baseline gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[3] / "tools/retrieval-eval/check_baseline.py"
    spec = importlib.util.spec_from_file_location("check_retrieval_baseline", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _report() -> dict:
    return {
        "corpus": {"corpus_sha256": "a", "query_sha256": "b"},
        "embedding": {"vector_fingerprint_sha256": "c"},
        "aggregate_means": {"en|direct|recall@10": 0.8},
        "latency_ms": {"p50": 10.0, "p95": 20.0},
        "index_stats": {"db_size_bytes": 100},
    }


def _baseline() -> dict:
    return {
        "corpus": {"corpus_sha256": "a", "query_sha256": "b"},
        "embedding": {"vector_fingerprint_sha256": "c"},
        "floors": {"en|direct|recall@10": 0.7},
        "latency_ms": {"p50": 10.0, "p95": 20.0},
        "index_stats": {"db_size_bytes": 100},
    }


def test_matching_quality_baseline_passes():
    assert _module().compare(_report(), _baseline()) == []


def test_hash_fingerprint_and_floor_drift_fail():
    report = _report()
    report["corpus"]["corpus_sha256"] = "changed"
    report["embedding"]["vector_fingerprint_sha256"] = "changed"
    report["aggregate_means"]["en|direct|recall@10"] = 0.6
    failures = _module().compare(report, _baseline(), check_fingerprint=True)
    assert len(failures) == 3


def test_performance_gate_uses_twenty_percent_ceiling():
    report = _report()
    report["latency_ms"]["p95"] = 24.1
    report["index_stats"]["db_size_bytes"] = 121
    failures = _module().compare(report, _baseline(), check_performance=True)
    assert len(failures) == 2
