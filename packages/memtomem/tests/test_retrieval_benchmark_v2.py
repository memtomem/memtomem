"""Contracts for the language-separated retrieval benchmark v2."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

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
            "reranker": {"model": "m", "searches": 60, "searches_reranked": 60},
        },
        {
            "aggregate": {
                "ko|direct|recall@10": 0.5,
                "ko|negation|hard_negative_hits@10": 1.2,
            },
            "zero_hit_count": 1,
            "latency_ms": {"p50": 2.0, "p95": 3.0},
            "reranker": {"model": "m", "searches": 60, "searches_reranked": 60},
        },
    ]
    combined = benchmark._combine_track_runs(reports)
    assert combined["reranker"] == {"model": "m", "searches": 120, "searches_reranked": 120}
    assert combined["latency_runs_ms"] == [
        {"p50": 3.0, "p95": 4.0},
        {"p50": 2.0, "p95": 3.0},
    ]
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


def test_v2_baseline_benchmarks_the_product_rrf_weights():
    """The required check must validate the configuration the product ships.

    ``check_baseline_v2`` benchmarks with ``baseline["search"]["rrf_weights"]``,
    and the file is produced by ``tune_rrf_v2.py``, whose job is to *select* a
    candidate from a grid. Nothing else connects the two: a refresh that picked
    a non-control candidate would quietly make a blocking CI check measure a
    profile no user runs, and would also be retuning against the published
    frozen holdout. If this ever fails, the refresh selected different weights —
    which is a decision to make deliberately (change the product default, or
    keep the tuning as a separate versioned experiment), not to absorb.
    """
    from memtomem.config import SearchConfig

    baseline = json.loads(
        (ROOT / "tools/retrieval-eval/baseline_v2.json").read_text(encoding="utf-8")
    )
    assert baseline["search"]["rrf_weights"] == SearchConfig().rrf_weights


def test_v2_committed_quality_bounds_match_generation_formula():
    tuner = _load("tune_rrf_v2_parity", "tools/retrieval-eval/tune_rrf_v2.py")
    baseline = json.loads(
        (ROOT / "tools/retrieval-eval/baseline_v2.json").read_text(encoding="utf-8")
    )
    for track_name, track in baseline["tracks"].items():
        floors, ceilings = tuner._quality_bounds(track)
        assert baseline["quality_floors"][track_name] == floors
        assert baseline["quality_ceilings"][track_name] == ceilings


def test_v2_benchmark_rejects_invalid_k_parameters():
    benchmark = _load("benchmark_v2_invalid_k", "tools/retrieval-eval/benchmark_v2.py")
    with pytest.raises(ValueError, match="must be positive"):
        asyncio.run(benchmark.benchmark(top_k=0))
    with pytest.raises(ValueError, match="at least top_k"):
        asyncio.run(
            benchmark.benchmark(
                top_k=10,
                reranker_model="reranker",
                reranker_pool=5,
            )
        )


def test_k_sweep_assessment_applies_language_and_weak_slice_gates():
    sweep = _load("sweep_k_v2_assessment", "tools/retrieval-eval/sweep_k_v2.py")

    def track(lang: str, value: float, zero_hits: int) -> dict:
        return {
            "aggregate": {
                f"{lang}|direct|recall@10": value,
                f"{lang}|direct|mrr@10": value,
                f"{lang}|direct|ndcg@10": value,
                f"{lang}|genre_primary|genre_hit@1": value,
                f"{lang}|negation|constraint_success@10": value,
                f"{lang}|multi_topic|intent_coverage@10": value,
            },
            "zero_hit_count": zero_hits,
            "latency_ms": {"p95": 10.0},
        }

    control = {
        "search": {"top_k": 10},
        "tracks": {
            "english": track("en", 0.5, 2),
            "korean": track("ko", 0.5, 2),
            "cross_language": track("ko", 0.5, 3),
        },
    }
    candidate = deepcopy(control)
    candidate["tracks"]["korean"] = track("ko", 0.55, 1)
    candidate["tracks"]["cross_language"] = track("ko", 0.55, 2)
    assessment = sweep.assess(candidate, control)
    assert assessment["eligible"] is True
    assert assessment["quality_gain"] == 0.2

    candidate["tracks"]["english"] = track("en", 0.48, 2)
    assessment = sweep.assess(candidate, control)
    assert assessment["eligible"] is False
    assert "English MRR/nDCG regression exceeds 0.01" in assessment["failures"]


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


def _benchmark_config(benchmark, tmp_path: Path, model: str, dimension: int, tokenizer: str):
    return benchmark._build_config(
        tmp=tmp_path,
        memory_root=tmp_path / "memories",
        embedding_model=model,
        embedding_dimension=dimension,
        tokenizer=tokenizer,
        reranker_model=None,
        reranker_pool=20,
        rrf_k=60,
        candidate_k=50,
    )


def test_v2_benchmark_config_runs_the_e5_profile_and_ignores_ambient_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Assigning fields onto a built config skipped the E5 validators (#2651)."""
    benchmark = _load("benchmark_v2_config", "tools/retrieval-eval/benchmark_v2.py")
    monkeypatch.setenv("MEMTOMEM_EMBEDDING__THREADS", "7")
    monkeypatch.setenv("MEMTOMEM_DECAY__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_SEARCH__TOKENIZER", "unicode61")

    e5 = _benchmark_config(benchmark, tmp_path, "intfloat/multilingual-e5-small", 384, "kiwipiepy")
    assert (e5.embedding.dimension, e5.embedding.max_sequence_tokens) == (384, 512)
    assert benchmark._resolved_settings(e5) == {
        "max_sequence_tokens": 512,
        "threads": 2,
        "chunk_input_prefix": "passage: ",
        "max_chunk_tokens": 384,
        "target_chunk_tokens": 320,
        "chunk_model_tokens": 512,
        "tokenizer": "kiwipiepy",
    }
    assert e5.decay.enabled is False

    minilm = _benchmark_config(
        benchmark,
        tmp_path,
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        384,
        "unicode61",
    )
    assert minilm.embedding.dimension == 384
    assert minilm.indexing.chunk_input_prefix == ""
    assert minilm.embedding.threads != 7
    assert minilm.decay.enabled is False


def test_v2_benchmark_refuses_searches_not_reranked_as_requested():
    benchmark = _load("benchmark_v2_coverage", "tools/retrieval-eval/benchmark_v2.py")
    reranked = ("rerank", "jina", False)
    assert benchmark._check_rerank_coverage([reranked, reranked], "jina") == 2
    assert benchmark._check_rerank_coverage([("rrf", None, False)], None) == 0
    # A timeout or failure falls back to the fused order.
    with pytest.raises(RuntimeError, match="1/2 searches not reranked by jina"):
        benchmark._check_rerank_coverage([reranked, ("rrf", None, False)], "jina")
    with pytest.raises(RuntimeError, match="not reranked by jina"):
        benchmark._check_rerank_coverage([("rerank", "other", False)], "jina")
    with pytest.raises(RuntimeError, match="search cache"):
        benchmark._check_rerank_coverage([("rerank", "jina", True)], "jina")
    with pytest.raises(RuntimeError, match="reranked without a reranker"):
        benchmark._check_rerank_coverage([("rerank", "jina", False)], None)


def test_model_comparison_e5_profiles_match_the_korean_preset_and_carry_licenses():
    from memtomem.cli.init_presets import PRESETS
    from memtomem.embedding.aliases import resolve_embedder_id

    comparison = _load("compare_models_v2_e5", "tools/retrieval-eval/compare_models_v2.py")
    preset = PRESETS["korean"]
    assert comparison.E5_TOKENIZER == preset.tokenizer
    assert {
        (resolve_embedder_id(model), dimension)
        for model, dimension in comparison.E5_MODELS.values()
    } == {(resolve_embedder_id(preset.model), preset.dimension)}
    assert set(comparison.E5_MODELS) == {"english", "korean", "cross_language"}

    e5_profiles = {name for name in comparison.PROFILES if name.startswith("e5_kiwipiepy")}
    assert e5_profiles == {
        "e5_kiwipiepy",
        "e5_kiwipiepy_jina",
        "e5_kiwipiepy_jina_int8",
        "e5_kiwipiepy_gte",
    }
    for name in e5_profiles:
        spec = comparison.PROFILES[name]
        assert (spec.embedding_models, spec.tokenizer) == (comparison.E5_MODELS, "kiwipiepy")

    for name, spec in comparison.PROFILES.items():
        if spec.reranker is None:
            assert comparison._license_notice(spec) is None
            continue
        assert spec.reranker.license and spec.reranker.model_card.startswith("https://")
        notice = comparison._license_notice(spec)
        if spec.reranker.model.startswith("jinaai/"):
            assert spec.reranker.license == "cc-by-nc-4.0"
            assert notice is not None and "non-commercial use only" in notice
        else:
            assert notice is None, name


def _child_report(name: str, pool: int, **overrides) -> dict:
    track = {
        "macro": {"recall@10": 0.5, "mrr@10": 0.5, "ndcg@10": 0.5},
        "zero_hit_count": 1,
        "latency_ms": {"p50": 5.0, "p95": 9.0},
        "aggregate": {"ko|direct|ndcg@10": 0.5},
    }
    report = {
        "profile": name,
        "runs": 5,
        "queries": 120,
        "portfolio": {"query_sha256": "q", "qrel_sha256": "r"},
        "corpus": {"corpus_sha256": "c"},
        "environment": {"memtomem": "0.0.0"},
        "summary": {
            "search": {"reranker_pool": pool},
            "tracks": {"korean": deepcopy(track)},
        },
        "cost": {"wall_s": 1.0, "peak_rss_bytes": 1, "reranker_footprint": None},
        "license": None,
        "model_card": None,
    }
    report.update(overrides)
    return report


def test_model_comparison_assembles_an_e5_only_report_from_child_reports(capsys):
    comparison = _load("compare_models_v2_assemble", "tools/retrieval-eval/compare_models_v2.py")
    calls: list[tuple[str, int, int]] = []

    def run_child(name: str, *, runs: int, reranker_pool: int) -> dict:
        calls.append((name, runs, reranker_pool))
        return _child_report(name, reranker_pool)

    report = comparison.compare(
        runs=5,
        reranker_pool=30,
        profiles=("e5_kiwipiepy", "e5_kiwipiepy_jina", "e5_kiwipiepy_gte"),
        run_child=run_child,
    )
    assert calls == [
        ("e5_kiwipiepy", 5, 30),
        ("e5_kiwipiepy_jina", 5, 30),
        ("e5_kiwipiepy_gte", 5, 30),
    ]
    assert report["queries"] == 120 and report["runs"] == 5 and report["reranker_pool"] == 30
    assert set(report["profiles"]) == {"e5_kiwipiepy", "e5_kiwipiepy_jina", "e5_kiwipiepy_gte"}
    assert set(report["deltas"]) == {"jina_on_e5_kiwipiepy", "gte_on_e5_kiwipiepy"}
    assert report["profiles"]["e5_kiwipiepy"]["cost"]["peak_rss_bytes"] == 1
    stderr = capsys.readouterr().err
    assert stderr.count("non-commercial use only") == 1
    assert comparison.MULTILINGUAL_RERANKER in stderr


def test_model_comparison_refuses_children_that_disagree_or_ran_another_pool():
    comparison = _load("compare_models_v2_refuse", "tools/retrieval-eval/compare_models_v2.py")
    profiles = ("e5_kiwipiepy", "e5_kiwipiepy_gte")

    def wrong_pool(name: str, *, runs: int, reranker_pool: int) -> dict:
        return _child_report(name, 20)

    with pytest.raises(RuntimeError, match="e5_kiwipiepy_gte ran with pool 20, not 30"):
        comparison.compare(reranker_pool=30, profiles=profiles, run_child=wrong_pool)

    def other_corpus(name: str, *, runs: int, reranker_pool: int) -> dict:
        corpus = {"corpus_sha256": name}
        return _child_report(name, reranker_pool, corpus=corpus)

    with pytest.raises(RuntimeError, match="profiles disagree on corpus"):
        comparison.compare(profiles=profiles, run_child=other_corpus)

    with pytest.raises(ValueError, match="unknown or empty profile selection"):
        comparison.compare(profiles=("nope",), run_child=other_corpus)


def test_model_comparison_child_argv_forwards_runs_and_pool(tmp_path: Path):
    comparison = _load("compare_models_v2_argv", "tools/retrieval-eval/compare_models_v2.py")
    argv = comparison._child_argv(
        "e5_kiwipiepy_gte", runs=5, reranker_pool=30, output=tmp_path / "out.json"
    )
    assert argv[0] == sys.executable
    assert Path(argv[1]).name == "compare_models_v2.py"
    assert argv[2:] == [
        "--run-profile",
        "e5_kiwipiepy_gte",
        "--runs",
        "5",
        "--reranker-pool",
        "30",
        "--output",
        str(tmp_path / "out.json"),
    ]


def test_model_comparison_footprint_counts_only_the_selected_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """jina's fp32 and int8 graphs share one snapshot directory."""
    pytest.importorskip("fastembed")
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    comparison = _load("compare_models_v2_disk", "tools/retrieval-eval/compare_models_v2.py")
    repo_dir = tmp_path / "models--org--reranker"
    (repo_dir / "refs").mkdir(parents=True)
    (repo_dir / "refs" / "main").write_text("mmm456\n", encoding="utf-8")
    # Other cached revisions, sorting before and after the one fastembed loads.
    for stale in ("aaa123", "zzz789"):
        (repo_dir / "snapshots" / stale / "onnx").mkdir(parents=True)
        (repo_dir / "snapshots" / stale / "onnx" / "model_int8.onnx").write_bytes(b"x" * 999)
    snapshot = repo_dir / "snapshots" / "mmm456"
    (snapshot / "onnx").mkdir(parents=True)
    (snapshot / "onnx" / "model.onnx").write_bytes(b"x" * 100)
    (snapshot / "onnx" / "model_int8.onnx").write_bytes(b"x" * 25)
    (snapshot / "tokenizer.json").write_bytes(b"x" * 5)
    (snapshot / "unrelated.bin").write_bytes(b"x" * 1000)
    descriptions = [
        {
            "model": "org/reranker",
            "sources": {"hf": "org/reranker"},
            "model_file": "onnx/model.onnx",
        },
        {
            "model": "org/reranker:int8",
            "sources": {"hf": "org/reranker"},
            "model_file": "onnx/model_int8.onnx",
            "additional_files": ["tokenizer_alias.json"],
        },
    ]
    monkeypatch.setattr(
        TextCrossEncoder, "list_supported_models", classmethod(lambda cls: descriptions)
    )
    # A second name for the same file is counted once.
    try:
        (snapshot / "tokenizer_alias.json").hardlink_to(snapshot / "tokenizer.json")
    except OSError:
        pytest.skip("hard links are not supported here")

    int8 = comparison._reranker_footprint("org/reranker:int8", tmp_path)
    assert int8["revision"] == "mmm456"
    assert int8["files"] == {"tokenizer.json": 5, "onnx/model_int8.onnx": 25}
    assert int8["bytes"] == 30
    assert comparison._reranker_footprint("org/reranker", tmp_path)["bytes"] == 105


def test_model_comparison_refuses_a_conflicting_custom_registration(
    monkeypatch: pytest.MonkeyPatch,
):
    pytest.importorskip("fastembed")
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    comparison = _load("compare_models_v2_register", "tools/retrieval-eval/compare_models_v2.py")
    spec = comparison.GTE_INT8
    added: list[str] = []
    monkeypatch.setattr(
        TextCrossEncoder,
        "add_custom_model",
        classmethod(lambda cls, model, **_: added.append(model)),
    )
    registry: list[dict] = []
    monkeypatch.setattr(
        TextCrossEncoder, "list_supported_models", classmethod(lambda cls: registry)
    )

    comparison._register_reranker(spec)
    assert added == [spec.model]

    registry.append(
        {"model": spec.model, "sources": {"hf": spec.hf_repo}, "model_file": spec.model_file}
    )
    comparison._register_reranker(spec)
    assert added == [spec.model]

    registry[0] = {"model": spec.model, "sources": {"hf": "other/repo"}, "model_file": "x.onnx"}
    with pytest.raises(RuntimeError, match="already registered from other/repo"):
        comparison._register_reranker(spec)


class _FakeIndexStats:
    def __init__(self, files: int):
        self.total_files = files
        self.total_chunks = files * 4
        self.indexed_chunks = files * 4
        self.blocked_files = 0
        self.errors: list[str] = []
        self.duration_ms = 1.0


class _FakeSearchStats:
    def __init__(self, score_scale: str, reranker_model: str | None):
        self.score_scale = score_scale
        self.reranker_model = reranker_model
        self.cache_hit = False


def _fake_track_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    search_stats: _FakeSearchStats,
    on_index=None,
    create_error: Exception | None = None,
) -> dict:
    """Swap ``create_components`` for a stub that indexes and searches nothing real."""
    import memtomem.runtime as runtime
    from memtomem.storage.fts_tokenizer import get_tokenizer

    seen: dict = {"closed": 0}

    class _IndexEngine:
        async def index_path(self, root: Path, recursive: bool):
            if on_index is not None:
                on_index()
            return _FakeIndexStats(len(list(root.rglob("*.md"))))

    class _SearchPipeline:
        async def search(self, text: str, **kwargs):
            return [], search_stats

    class _Components:
        index_engine = _IndexEngine()
        search_pipeline = _SearchPipeline()

    async def create_components(config, **kwargs):
        seen["kwargs"] = kwargs
        seen["tokenizer_at_create"] = get_tokenizer()
        seen["config"] = config
        if create_error is not None:
            raise create_error
        return _Components()

    async def close_components(components):
        seen["closed"] += 1

    monkeypatch.setattr(runtime, "create_components", create_components)
    monkeypatch.setattr(runtime, "close_components", close_components)
    return seen


def _run_fake_track(benchmark, tokenizer: str, reranker_model: str | None) -> dict:
    portfolio = _load("query_holdout_v2_fake", "tools/retrieval-eval/query_holdout_v2.py")
    queries = tuple(query for query in portfolio.QUERIES if query.lang == "ko")[:3]
    return asyncio.run(
        benchmark._evaluate_track(
            track="korean",
            languages=("ko",),
            queries=queries,
            all_chunks=benchmark.collect_tagged_chunks(),
            weights=(1.0, 1.0),
            embedding_model="intfloat/multilingual-e5-small",
            embedding_dimension=384,
            tokenizer=tokenizer,
            reranker_model=reranker_model,
            reranker_pool=20,
            top_k=10,
            rrf_k=60,
            candidate_k=50,
        )
    )


@pytest.fixture
def _benchmark_track(monkeypatch: pytest.MonkeyPatch):
    from memtomem.storage.fts_tokenizer import get_tokenizer, set_tokenizer

    monkeypatch.chdir(ROOT)  # the fixture corpus and ir_metrics paths are repo-relative
    set_tokenizer("unicode61")
    yield _load("benchmark_v2_track", "tools/retrieval-eval/benchmark_v2.py")
    assert get_tokenizer() == "unicode61"


def test_v2_track_runs_ambient_free_with_its_tokenizer_and_resets_it(
    _benchmark_track, monkeypatch: pytest.MonkeyPatch
):
    seen = _fake_track_runtime(monkeypatch, search_stats=_FakeSearchStats("rerank", "jina"))
    report = _run_fake_track(_benchmark_track, "kiwipiepy", "jina")
    assert seen["kwargs"] == {"load_ambient_config": False, "entity_backfill": False}
    assert seen["tokenizer_at_create"] == "kiwipiepy"
    assert seen["closed"] == 1
    assert report["reranker"]["searches"] == 3
    assert report["reranker"]["searches_reranked"] == 3
    assert report["resolved"]["chunk_input_prefix"] == "passage: "
    assert report["embedding"] == {
        "provider": "onnx",
        "model": "intfloat/multilingual-e5-small",
        "dimension": 384,
    }


def test_v2_track_refuses_a_rerank_fallback(_benchmark_track, monkeypatch: pytest.MonkeyPatch):
    seen = _fake_track_runtime(monkeypatch, search_stats=_FakeSearchStats("rrf", None))
    with pytest.raises(RuntimeError, match="3/3 searches not reranked by jina"):
        _run_fake_track(_benchmark_track, "kiwipiepy", "jina")
    assert seen["closed"] == 1


def test_v2_track_refuses_a_silent_tokenizer_fallback(
    _benchmark_track, monkeypatch: pytest.MonkeyPatch
):
    from memtomem.storage.fts_tokenizer import set_tokenizer

    # What fts_tokenizer._get_kiwi does when kiwipiepy fails to import.
    _fake_track_runtime(
        monkeypatch,
        search_stats=_FakeSearchStats("rrf", None),
        on_index=lambda: set_tokenizer("unicode61"),
    )
    with pytest.raises(RuntimeError, match="korean indexed with unicode61, not kiwipiepy"):
        _run_fake_track(_benchmark_track, "kiwipiepy", None)


def test_v2_track_resets_the_tokenizer_when_components_fail(
    _benchmark_track, monkeypatch: pytest.MonkeyPatch
):
    seen = _fake_track_runtime(
        monkeypatch,
        search_stats=_FakeSearchStats("rrf", None),
        create_error=RuntimeError("model load failed"),
    )
    with pytest.raises(RuntimeError, match="model load failed"):
        _run_fake_track(_benchmark_track, "kiwipiepy", None)
    assert seen["closed"] == 0
    assert not seen["config"].storage.sqlite_path.parent.exists()


def test_model_comparison_summary_keeps_spreads_settings_and_coverage():
    comparison = _load("compare_models_v2_summary", "tools/retrieval-eval/compare_models_v2.py")
    track = {
        "embedding": {"provider": "onnx", "model": "m", "dimension": 384},
        "reranker": {"model": "r", "searches": 300, "searches_reranked": 300},
        "resolved": {"tokenizer": "kiwipiepy", "chunk_input_prefix": "passage: "},
        "index": {"files": 24, "chunks": 96, "duration_ms": 1.0},
        "runs": 5,
        "zero_hit_count": 2,
        "latency_ms": {"p50": 5.0, "p95": 9.0},
        "latency_runs_ms": [{"p50": 5.0, "p95": 9.0}],
        "max_run_spread": 0.02,
        "run_spreads": {"ko|direct|ndcg@10": 0.02},
        "aggregate": {
            "ko|direct|ndcg@10": 0.6,
            "ko|direct|recall@10": 0.7,
            "ko|direct|mrr@10": 0.5,
        },
        "per_query": [{"query_id": "q"}],
    }
    summary = comparison._profile_summary({"search": {"top_k": 10}, "tracks": {"korean": track}})
    korean = summary["tracks"]["korean"]
    for key in (
        "embedding",
        "reranker",
        "resolved",
        "index",
        "runs",
        "latency_runs_ms",
        "max_run_spread",
        "run_spreads",
    ):
        assert korean[key] == track[key], key
    assert korean["macro"]["ndcg@10"] == 0.6
    assert "per_query" not in korean
    json.dumps(summary)
