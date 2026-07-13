"""Contracts for the language-separated retrieval benchmark v2."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_holdout_has_sixty_bilingual_pairs_and_balanced_types():
    portfolio = _load("query_holdout_v2_test", "tools/retrieval-eval/query_holdout_v2.py")
    assert len(portfolio.QUERIES) == 120
    pair_counts = Counter(query.pair_id for query in portfolio.QUERIES)
    assert len(pair_counts) == 60
    assert set(pair_counts.values()) == {2}
    assert Counter((query.lang, query.type) for query in portfolio.QUERIES) == {
        (lang, query_type): 10
        for lang in ("en", "ko")
        for query_type in (
            "direct",
            "paraphrase",
            "underspecified",
            "multi_topic",
            "negation",
            "genre_primary",
        )
    }


def test_every_v2_query_has_explicit_primary_qrels():
    portfolio = _load("query_holdout_v2_qrels", "tools/retrieval-eval/query_holdout_v2.py")
    benchmark = _load("benchmark_v2_qrels", "tools/retrieval-eval/benchmark_v2.py")
    chunks = benchmark.collect_tagged_chunks()
    for query in portfolio.QUERIES:
        qrels = benchmark.build_qrels(query, chunks)
        assert qrels["primary"], query.query_id
        if query.type == "negation":
            assert qrels["hard_negative"], query.query_id


def test_v2_baseline_compare_checks_hashes_models_floors_and_zero_hits():
    checker = _load("check_baseline_v2_test", "tools/retrieval-eval/check_baseline_v2.py")
    track = {
        "embedding": {"provider": "onnx", "model": "model", "dimension": 384},
        "aggregate": {"en|direct|recall@10": 0.8},
        "zero_hit_count": 1,
        "latency_ms": {"p95": 10.0},
    }
    report = {
        "schema_version": 2,
        "methodology": "v2",
        "portfolio": {"query_sha256": "q", "qrel_sha256": "r"},
        "corpus": {"corpus_sha256": "c"},
        "tracks": {"english": track},
    }
    baseline = deepcopy(report)
    baseline["quality_floors"] = {"english": {"en|direct|recall@10": 0.7}}
    baseline["quality_ceilings"] = {"english": {}}
    baseline["zero_hit_caps"] = {"english": 1}
    assert checker.compare(report, baseline) == []
    report["portfolio"]["qrel_sha256"] = "changed"
    report["tracks"]["english"]["zero_hit_count"] = 2
    assert len(checker.compare(report, baseline)) == 2


def test_v2_run_spreads_and_directional_quality_bounds():
    benchmark = _load("benchmark_v2_spreads", "tools/retrieval-eval/benchmark_v2.py")
    tuner = _load("tune_rrf_v2_bounds", "tools/retrieval-eval/tune_rrf_v2.py")
    reports = [
        {
            "aggregate": {
                "ko|direct|recall@10": 0.6,
                "ko|negation|hard_negative_hits@10": 0.8,
            },
            "zero_hit_count": 2,
            "latency_ms": {"p50": 3.0, "p95": 4.0},
        },
        {
            "aggregate": {
                "ko|direct|recall@10": 0.5,
                "ko|negation|hard_negative_hits@10": 1.2,
            },
            "zero_hit_count": 1,
            "latency_ms": {"p50": 2.0, "p95": 3.0},
        },
    ]
    combined = benchmark._combine_track_runs(reports)
    assert combined["run_spreads"] == {
        "ko|direct|recall@10": 0.1,
        "ko|negation|hard_negative_hits@10": 0.4,
    }
    floors, ceilings = tuner._quality_bounds(combined)
    assert floors == {"ko|direct|recall@10": 0.395}
    assert ceilings == {"ko|negation|hard_negative_hits@10": 1.5}


def test_v2_baseline_compare_treats_hard_negative_hits_as_a_ceiling():
    checker = _load("check_baseline_v2_ceiling", "tools/retrieval-eval/check_baseline_v2.py")
    track = {
        "embedding": {"provider": "onnx", "model": "model", "dimension": 384},
        "aggregate": {"ko|negation|hard_negative_hits@10": 0.5},
        "zero_hit_count": 0,
        "latency_ms": {"p95": 10.0},
    }
    report = {
        "schema_version": 2,
        "methodology": "v2",
        "portfolio": {"query_sha256": "q", "qrel_sha256": "r"},
        "corpus": {"corpus_sha256": "c"},
        "tracks": {"korean": track},
    }
    baseline = deepcopy(report)
    baseline["quality_floors"] = {"korean": {}}
    baseline["quality_ceilings"] = {"korean": {"ko|negation|hard_negative_hits@10": 1.0}}
    baseline["zero_hit_caps"] = {"korean": 0}
    assert checker.compare(report, baseline) == []
    report["tracks"]["korean"]["aggregate"]["ko|negation|hard_negative_hits@10"] = 1.1
    assert checker.compare(report, baseline) == [
        "quality ceiling failed for korean|ko|negation|hard_negative_hits@10: "
        "ceiling 1.0, observed 1.1"
    ]


def test_v2_committed_quality_bounds_match_generation_formula():
    tuner = _load("tune_rrf_v2_parity", "tools/retrieval-eval/tune_rrf_v2.py")
    baseline = json.loads(
        (ROOT / "tools/retrieval-eval/baseline_v2.json").read_text(encoding="utf-8")
    )
    for track_name, track in baseline["tracks"].items():
        floors, ceilings = tuner._quality_bounds(track)
        assert baseline["quality_floors"][track_name] == floors
        assert baseline["quality_ceilings"][track_name] == ceilings


def test_model_comparison_uses_multilingual_reranker_and_1024_dim_bge_m3():
    comparison = _load("compare_models_v2_contract", "tools/retrieval-eval/compare_models_v2.py")
    assert "multilingual" in comparison.MULTILINGUAL_RERANKER
    assert set(comparison.BGE_M3_MODELS) == {
        "english",
        "korean",
        "cross_language",
    }
    assert all(
        model == "BAAI/bge-m3" and dimension == 1024
        for model, dimension in comparison.BGE_M3_MODELS.values()
    )


def test_model_comparison_delta_reports_quality_zero_hits_and_latency():
    comparison = _load("compare_models_v2_delta", "tools/retrieval-eval/compare_models_v2.py")
    control = {
        "tracks": {
            "english": {
                "macro": {"recall@10": 0.5, "mrr@10": 0.4, "ndcg@10": 0.3},
                "zero_hit_count": 3,
                "latency_ms": {"p95": 10.0},
                "aggregate": {"en|direct|recall@10": 0.5},
            }
        }
    }
    candidate = deepcopy(control)
    candidate["tracks"]["english"]["macro"]["recall@10"] = 0.6
    candidate["tracks"]["english"]["zero_hit_count"] = 1
    candidate["tracks"]["english"]["latency_ms"]["p95"] = 25.0
    candidate["tracks"]["english"]["aggregate"]["en|direct|recall@10"] = 0.6
    delta = comparison._delta(candidate, control)["english"]
    assert delta["macro"]["recall@10"] == 0.1
    assert delta["zero_hit_count"] == -2
    assert delta["p95_latency_ms"] == 15.0
    assert delta["slices"]["en|direct|recall@10"] == 0.1
