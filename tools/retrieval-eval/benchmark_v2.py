#!/usr/bin/env python3
"""Run the methodology-v2 bilingual retrieval benchmark."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Iterable

FIXTURE_ROOT = Path("packages/memtomem/tests/fixtures/corpus_v2")
GENRES = frozenset({"runbook", "postmortem", "adr", "troubleshooting"})


def _load_sibling(name: str) -> Any:
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"retrieval_v2_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class TaggedChunk:
    key: str
    lang: str
    topic: str
    genre: str
    heading: str
    primary: str
    secondary: tuple[str, ...]


def collect_tagged_chunks() -> tuple[TaggedChunk, ...]:
    validator = _load_sibling("drift_validator")
    chunks: list[TaggedChunk] = []
    for path in sorted(FIXTURE_ROOT.rglob("*.md")):
        if path.stem not in GENRES:
            continue
        relative = path.relative_to(FIXTURE_ROOT)
        for chunk in validator.parse_fixture(path):
            chunks.append(
                TaggedChunk(
                    key=f"{relative.as_posix()}|{chunk.heading}",
                    lang=chunk.lang,
                    topic=chunk.topic,
                    genre=chunk.genre,
                    heading=chunk.heading,
                    primary=chunk.primary,
                    secondary=chunk.secondary,
                )
            )
    return tuple(chunks)


def _target_topics(query: Any) -> frozenset[str]:
    return frozenset(target.split("/", 1)[0] for target in query.targets)


def build_qrels(query: Any, chunks: Iterable[TaggedChunk]) -> dict[str, Any]:
    """Freeze explicit portable qrels from the reviewed methodology rules."""
    target_topics = _target_topics(query)
    relevant: dict[str, float] = {}
    hard_negative: list[str] = []
    for chunk in chunks:
        target_primary = chunk.primary in query.targets
        target_secondary = any(tag in query.targets for tag in chunk.secondary)
        if query.type == "genre_primary":
            if chunk.topic in target_topics and chunk.genre == query.genre:
                relevant[chunk.key] = 1.0
        elif query.type == "negation":
            if target_primary and chunk.genre == query.genre:
                relevant[chunk.key] = 1.0
            elif target_primary:
                hard_negative.append(chunk.key)
        elif target_primary:
            relevant[chunk.key] = 1.0
        elif target_secondary:
            relevant[chunk.key] = 0.5
    primary = sorted(key for key, grade in relevant.items() if grade == 1.0)
    return {
        "relevant": dict(sorted(relevant.items())),
        "primary": primary,
        "hard_negative": sorted(hard_negative),
        "intents": sorted(target_topics),
    }


def _portable_result_key(result: Any, memory_root: Path) -> str:
    relative = result.chunk.metadata.source_file.resolve().relative_to(memory_root.resolve())
    hierarchy = result.chunk.metadata.heading_hierarchy
    heading = (hierarchy[-1] if hierarchy else "").lstrip("#").strip()
    return f"{relative.as_posix()}|{heading}"


def _reciprocal_rank(ids: list[str], relevant: set[str], k: int = 10) -> float:
    for index, item in enumerate(ids[:k], start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_ir_metrics() -> Any:
    path = Path("packages/memtomem/tests/ir_metrics.py")
    spec = importlib.util.spec_from_file_location("retrieval_v2_ir_metrics", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _evaluate_track(
    *,
    track: str,
    languages: tuple[str, ...],
    queries: tuple[Any, ...],
    all_chunks: tuple[TaggedChunk, ...],
    weights: tuple[float, float],
    embedding_model: str,
    embedding_dimension: int,
    reranker_model: str | None,
    reranker_pool: int,
    top_k: int,
    rrf_k: int,
    candidate_k: int,
) -> dict[str, Any]:
    from memtomem.config import Mem2MemConfig
    import memtomem.config as config_module
    from memtomem.server.component_factory import close_components, create_components

    tmp = Path(mkdtemp(prefix=f"retrieval_v2_{track}_"))
    memory_root = tmp / "memories"
    memory_root.mkdir()
    for language in languages:
        shutil.copytree(FIXTURE_ROOT / language, memory_root / language)

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp / "benchmark.db"
    config.indexing.memory_dirs = [memory_root]
    config.embedding.provider = "onnx"
    config.embedding.model = embedding_model
    config.embedding.dimension = embedding_dimension
    config.search.cache_ttl = 0.0
    config.search.rrf_k = rrf_k
    config.search.bm25_candidates = candidate_k
    config.search.dense_candidates = candidate_k
    if reranker_model is not None:
        config.rerank.enabled = True
        config.rerank.provider = "fastembed"
        config.rerank.model = reranker_model
        config.rerank.oversample = 2.0
        config.rerank.min_pool = reranker_pool
        config.rerank.max_pool = reranker_pool

    original_loader = config_module.load_config_overrides
    config_module.load_config_overrides = lambda config: None
    components = await create_components(config)
    ir_metrics = _load_ir_metrics()
    try:
        stats = await components.index_engine.index_path(memory_root, recursive=True)
        expected = (24 * len(languages), 96 * len(languages), 96 * len(languages), 0, 0)
        observed = (
            stats.total_files,
            stats.total_chunks,
            stats.indexed_chunks,
            stats.blocked_files,
            len(stats.errors),
        )
        if observed != expected:
            raise RuntimeError(
                f"incomplete {track} index: expected {expected}, observed {observed}"
            )

        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for query in queries:
            qrels = build_qrels(query, all_chunks)
            allowed_primary = {
                key
                for key in qrels["primary"]
                if track == "cross_language" or key.startswith(f"{query.lang}/")
            }
            allowed_relevance = {
                key: grade
                for key, grade in qrels["relevant"].items()
                if track == "cross_language" or key.startswith(f"{query.lang}/")
            }
            allowed_hard_negative = {
                key
                for key in qrels["hard_negative"]
                if track == "cross_language" or key.startswith(f"{query.lang}/")
            }
            if not allowed_primary:
                raise RuntimeError(f"query has no primary qrel in {track}: {query.query_id}")

            started = time.perf_counter()
            results, _ = await components.search_pipeline.search(
                query.text,
                top_k=top_k,
                rrf_weights=list(weights),
            )
            latencies.append((time.perf_counter() - started) * 1000)
            retrieved = [_portable_result_key(result, memory_root) for result in results]
            recall = ir_metrics.recall_at_k(retrieved, allowed_primary, top_k)
            mrr = ir_metrics.reciprocal_rank_at_k(retrieved, allowed_primary, top_k)
            ndcg = ir_metrics.ndcg_at_k(retrieved, allowed_relevance, top_k)
            row: dict[str, Any] = {
                "query_id": query.query_id,
                "pair_id": query.pair_id,
                "lang": query.lang,
                "type": query.type,
                f"recall@{top_k}": recall,
                f"mrr@{top_k}": mrr,
                f"ndcg@{top_k}": ndcg,
                "zero_hit": recall == 0.0,
                "retrieved": retrieved,
            }
            if query.type == "genre_primary":
                matching_genre = {
                    chunk.key
                    for chunk in all_chunks
                    if chunk.genre == query.genre
                    and chunk.topic in _target_topics(query)
                    and (track == "cross_language" or chunk.lang == query.lang)
                }
                row["genre_hit@1"] = float(bool(retrieved and retrieved[0] in matching_genre))
                row[f"genre_mrr@{top_k}"] = _reciprocal_rank(retrieved, matching_genre, top_k)
            if query.type == "negation":
                first_relevant = next(
                    (index for index, key in enumerate(retrieved) if key in allowed_primary),
                    None,
                )
                first_negative = next(
                    (index for index, key in enumerate(retrieved) if key in allowed_hard_negative),
                    None,
                )
                row[f"constraint_success@{top_k}"] = float(
                    first_relevant is not None
                    and (first_negative is None or first_relevant < first_negative)
                )
                row[f"hard_negative_hits@{top_k}"] = sum(
                    key in allowed_hard_negative for key in retrieved[:top_k]
                )
            if query.type == "multi_topic":
                retrieved_topics = {
                    chunk.topic for chunk in all_chunks if chunk.key in set(retrieved[:top_k])
                }
                intents = set(qrels["intents"])
                row[f"intent_coverage@{top_k}"] = len(retrieved_topics & intents) / len(intents)
            if track == "cross_language":
                row[f"same_language_precision@{top_k}"] = sum(
                    key.startswith(f"{query.lang}/") for key in retrieved[:top_k]
                ) / max(1, len(retrieved[:top_k]))
                row[f"cross_language_relevant@{top_k}"] = sum(
                    key in allowed_primary and not key.startswith(f"{query.lang}/")
                    for key in retrieved[:top_k]
                )
            rows.append(row)

        aggregate: dict[str, float] = {}
        samples: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            prefix = f"{row['lang']}|{row['type']}"
            for key, value in row.items():
                if key in {
                    f"recall@{top_k}",
                    f"mrr@{top_k}",
                    f"ndcg@{top_k}",
                    "genre_hit@1",
                    f"genre_mrr@{top_k}",
                    f"constraint_success@{top_k}",
                    f"hard_negative_hits@{top_k}",
                    f"intent_coverage@{top_k}",
                    f"same_language_precision@{top_k}",
                    f"cross_language_relevant@{top_k}",
                }:
                    samples[f"{prefix}|{key}"].append(float(value))
        for key, values in samples.items():
            aggregate[key] = round(statistics.fmean(values), 6)

        return {
            "track": track,
            "languages": list(languages),
            "embedding": {
                "provider": config.embedding.provider,
                "model": config.embedding.model,
                "dimension": config.embedding.dimension,
            },
            "reranker": {
                "enabled": reranker_model is not None,
                "provider": "fastembed" if reranker_model is not None else None,
                "model": reranker_model,
                "pool": reranker_pool if reranker_model is not None else None,
            },
            "index": {
                "files": stats.total_files,
                "chunks": stats.indexed_chunks,
                "duration_ms": round(stats.duration_ms, 3),
            },
            "latency_ms": {
                "p50": round(_percentile(latencies, 0.50), 3),
                "p95": round(_percentile(latencies, 0.95), 3),
            },
            "zero_hit_count": sum(row["zero_hit"] for row in rows),
            "aggregate": aggregate,
            "per_query": rows,
        }
    finally:
        config_module.load_config_overrides = original_loader
        await close_components(components)
        shutil.rmtree(tmp)


def _combine_track_runs(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ValueError("at least one track report is required")
    combined = dict(reports[-1])
    keys = reports[0]["aggregate"]
    combined["aggregate"] = {
        key: round(statistics.fmean(report["aggregate"][key] for report in reports), 6)
        for key in keys
    }
    combined["runs"] = len(reports)
    combined["run_spreads"] = {
        key: round(
            max(report["aggregate"][key] for report in reports)
            - min(report["aggregate"][key] for report in reports),
            6,
        )
        for key in keys
    }
    combined["max_run_spread"] = max(combined["run_spreads"].values(), default=0.0)
    combined["zero_hit_count"] = max(report["zero_hit_count"] for report in reports)
    combined["latency_ms"] = {
        metric: max(report["latency_ms"][metric] for report in reports) for metric in ("p50", "p95")
    }
    return combined


async def _repeat_track(runs: int, **kwargs: Any) -> dict[str, Any]:
    return _combine_track_runs([await _evaluate_track(**kwargs) for _ in range(runs)])


async def benchmark(
    weights: tuple[float, float] = (1.0, 1.0),
    *,
    runs: int = 1,
    embedding_models: dict[str, tuple[str, int]] | None = None,
    reranker_model: str | None = None,
    reranker_pool: int = 20,
    top_k: int = 10,
    rrf_k: int = 60,
    candidate_k: int = 50,
) -> dict[str, Any]:
    if runs <= 0:
        raise ValueError("runs must be positive")
    if min(top_k, rrf_k, candidate_k) <= 0:
        raise ValueError("top_k, rrf_k, and candidate_k must be positive")
    if reranker_model is not None and reranker_pool < top_k:
        raise ValueError("reranker_pool must be at least top_k")
    portfolio = _load_sibling("query_holdout_v2")
    chunks = collect_tagged_chunks()
    models = embedding_models or {
        "english": ("BAAI/bge-small-en-v1.5", 384),
        "korean": (
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            384,
        ),
        "cross_language": (
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            384,
        ),
    }
    qrel_manifest = {query.query_id: build_qrels(query, chunks) for query in portfolio.QUERIES}
    same_en = await _repeat_track(
        runs,
        track="english",
        languages=("en",),
        queries=tuple(query for query in portfolio.QUERIES if query.lang == "en"),
        all_chunks=chunks,
        weights=weights,
        embedding_model=models["english"][0],
        embedding_dimension=models["english"][1],
        reranker_model=reranker_model,
        reranker_pool=reranker_pool,
        top_k=top_k,
        rrf_k=rrf_k,
        candidate_k=candidate_k,
    )
    same_ko = await _repeat_track(
        runs,
        track="korean",
        languages=("ko",),
        queries=tuple(query for query in portfolio.QUERIES if query.lang == "ko"),
        all_chunks=chunks,
        weights=weights,
        embedding_model=models["korean"][0],
        embedding_dimension=models["korean"][1],
        reranker_model=reranker_model,
        reranker_pool=reranker_pool,
        top_k=top_k,
        rrf_k=rrf_k,
        candidate_k=candidate_k,
    )
    cross_language = await _repeat_track(
        runs,
        track="cross_language",
        languages=("en", "ko"),
        queries=portfolio.QUERIES,
        all_chunks=chunks,
        weights=weights,
        embedding_model=models["cross_language"][0],
        embedding_dimension=models["cross_language"][1],
        reranker_model=reranker_model,
        reranker_pool=reranker_pool,
        top_k=top_k,
        rrf_k=rrf_k,
        candidate_k=candidate_k,
    )
    query_payload = [asdict(query) for query in portfolio.QUERIES]
    return {
        "schema_version": 2,
        "methodology": "bilingual-holdout-v2",
        "runs": runs,
        "environment": {"memtomem": importlib.metadata.version("memtomem")},
        "portfolio": {
            "queries": len(portfolio.QUERIES),
            "pairs": len({query.pair_id for query in portfolio.QUERIES}),
            "query_sha256": _hash_json(query_payload),
            "qrel_sha256": _hash_json(qrel_manifest),
        },
        "corpus": {
            "files": 48,
            "chunks": 192,
            "corpus_sha256": _load_sibling("audit_public_corpus").audit().corpus_sha256,
        },
        "search": {
            "rrf_weights": list(weights),
            "top_k": top_k,
            "rrf_k": rrf_k,
            "bm25_candidates": candidate_k,
            "dense_candidates": candidate_k,
            "reranker_model": reranker_model,
            "reranker_pool": reranker_pool if reranker_model is not None else None,
        },
        "qrels": qrel_manifest,
        "tracks": {
            "english": same_en,
            "korean": same_ko,
            "cross_language": cross_language,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="1,1", help="BM25,dense RRF weights")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    values = tuple(float(value.strip()) for value in args.weights.split(","))
    if len(values) != 2 or any(value < 0 for value in values) or not any(values):
        parser.error("--weights requires two non-negative values with at least one non-zero")
    report = asyncio.run(
        benchmark(
            (values[0], values[1]),
            runs=args.runs,
            top_k=args.top_k,
            rrf_k=args.rrf_k,
            candidate_k=args.candidate_k,
        )
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
