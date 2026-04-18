#!/usr/bin/env python3
"""B.2 v2 Phase 5d — calibrate per-type floors from the 100-query portfolio.

Runs every query in `query_portfolio.py` against the indexed 6-topic
corpus, measures recall@10 / MRR@10 / nDCG@10 per query, and aggregates
per `(lang, type)` to produce floor values (`round(mean × factor, 2)`,
factor defaults to 0.9).

Usage:
    PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \\
        tools/retrieval-eval/calibrate_portfolio.py
    # add --runs N to average across N runs (default 3)
    # add --json to emit a JSON blob for downstream CI use

Notes:
- Uses `rrf_weights=[1.0, 1.0]` (balanced fusion) as the canonical
  operating point. BM25/dense-only extremes are measured separately
  by `measure_sensitivity.py`.
- Relevance grading: primary-match = 1.0, secondary-match = 0.5,
  otherwise 0.0 (per `b2-v2-design.md` § "Relevance grading").
- Recall and MRR use primary-only binary relevance; nDCG uses graded.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import shutil
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

FIXTURE_ROOT = Path("packages/memtomem/tests/fixtures/corpus_v2")
CORE_TOPICS = (
    "caching",
    "postgres",
    "cost_optimization",
    "security",
    "observability",
    "k8s",
)
GENRES = frozenset({"runbook", "postmortem", "adr", "troubleshooting"})


@dataclass(frozen=True)
class TaggedChunk:
    """A parsed fixture chunk with its primary/secondary tags.

    `heading` matches `metadata.heading_hierarchy[-1]` on indexed chunks.
    """

    source_file: Path
    heading: str
    primary: str
    secondary: tuple[str, ...]
    lang: str


def _load_sibling(name: str) -> Any:
    path = Path(__file__).parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def collect_tagged_chunks(
    fixture_root: Path = FIXTURE_ROOT,
) -> list[TaggedChunk]:
    """Walk the corpus and parse every genre fixture into `TaggedChunk`s."""
    drift_validator = _load_sibling("drift_validator")
    chunks: list[TaggedChunk] = []
    for fx in sorted(fixture_root.rglob("*.md")):
        if fx.stem not in GENRES:
            continue
        if "corpus_v2" not in fx.parts:
            continue
        for c in drift_validator.parse_fixture(fx):
            chunks.append(
                TaggedChunk(
                    source_file=fx.resolve(),
                    heading=c.heading,
                    primary=c.primary,
                    secondary=c.secondary,
                    lang=c.lang,
                )
            )
    return chunks


def build_tag_index(
    tagged: list[TaggedChunk],
) -> dict[tuple[Path, str], TaggedChunk]:
    """`(resolved source_file, heading)` → TaggedChunk lookup."""
    return {(t.source_file, t.heading): t for t in tagged}


def build_relevance(
    tagged: list[TaggedChunk],
    targets: frozenset[str],
    query_lang: str,
) -> tuple[set[tuple[Path, str]], dict[tuple[Path, str], float]]:
    """Return `(primary_relevant_keys, graded_relevance_map)`.

    Only chunks matching `query_lang` contribute. Keys are the same
    `(source_file, heading)` tuples produced by `build_tag_index`.
    """
    primary: set[tuple[Path, str]] = set()
    graded: dict[tuple[Path, str], float] = {}
    for t in tagged:
        if t.lang != query_lang:
            continue
        key = (t.source_file, t.heading)
        if t.primary in targets:
            primary.add(key)
            graded[key] = 1.0
        elif any(s in targets for s in t.secondary):
            graded[key] = 0.5
    return primary, graded


def _retrieved_key(result: Any) -> tuple[Path, str]:
    """`(resolved source_file, heading)` key for an indexed SearchResult.

    Heading resolves to `metadata.heading_hierarchy[-1]` with any
    leading markdown heading prefix (`#`, `##`, ...) stripped so the
    match against `drift_validator.parse_fixture` output works.
    """
    hierarchy = result.chunk.metadata.heading_hierarchy
    raw = hierarchy[-1] if hierarchy else ""
    heading = raw.lstrip("#").strip()
    return (result.chunk.metadata.source_file.resolve(), heading)


def compute_floors(
    samples: dict[tuple[str, str, str], list[float]],
    factor: float = 0.9,
) -> dict[tuple[str, str, str], float]:
    """Per-(lang, type, metric) floor = round(mean × factor, 2).

    `samples` key is `(lang, type, metric)`; value is a flat list of
    per-query measurements across all runs.
    """
    floors: dict[tuple[str, str, str], float] = {}
    for key, values in samples.items():
        if not values:
            floors[key] = 0.0
            continue
        floors[key] = round(statistics.fmean(values) * factor, 2)
    return floors


def _load_ir_metrics() -> Any:
    path = (
        Path(__file__).resolve().parents[1].parent
        / "packages"
        / "memtomem"
        / "tests"
        / "ir_metrics.py"
    )
    spec = importlib.util.spec_from_file_location("_b2v2_ir_metrics", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _genre_of(source_file: Path) -> str | None:
    stem = source_file.stem
    return stem if stem in GENRES else None


async def _calibrate_once(
    comp: Any,
    queries: list[Any],
    tagged: list[TaggedChunk],
    ir_metrics: Any,
) -> dict:
    """One calibration pass — returns per-query metrics and top-1 genre counts."""
    samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    # Genre confusion — only genre_primary queries contribute (they carry
    # an expected_genre; topic-primary queries do not).
    # Key: (lang, expected_genre, observed_top_1_genre) → count across queries.
    confusion: dict[tuple[str, str, str], int] = defaultdict(int)
    per_query: list[dict] = []
    for q in queries:
        res, _ = await comp.search_pipeline.search(q.text, top_k=10, rrf_weights=[1.0, 1.0])
        retrieved_keys = [_retrieved_key(r) for r in res]
        primary, graded = build_relevance(tagged, q.targets, q.lang)
        relevance = {f"{k[0]}|{k[1]}": v for k, v in graded.items()}
        retrieved_ids = [f"{k[0]}|{k[1]}" for k in retrieved_keys]
        primary_ids = {f"{k[0]}|{k[1]}" for k in primary}

        r_at_10 = ir_metrics.recall_at_k(retrieved_ids, primary_ids, 10)
        mrr_at_10 = ir_metrics.reciprocal_rank_at_k(retrieved_ids, primary_ids, 10)
        ndcg_at_10 = ir_metrics.ndcg_at_k(retrieved_ids, relevance, 10)

        samples[(q.lang, q.type, "recall@10")].append(r_at_10)
        samples[(q.lang, q.type, "mrr@10")].append(mrr_at_10)
        samples[(q.lang, q.type, "ndcg@10")].append(ndcg_at_10)

        top_1_genre = None
        if res:
            top_1_genre = _genre_of(res[0].chunk.metadata.source_file)
        if q.type == "genre_primary" and q.genre:
            observed = top_1_genre or "none"
            confusion[(q.lang, q.genre, observed)] += 1

        per_query.append(
            {
                "query": q.text,
                "lang": q.lang,
                "type": q.type,
                "genre": q.genre,
                "top_1_genre": top_1_genre,
                "recall@10": r_at_10,
                "mrr@10": mrr_at_10,
                "ndcg@10": ndcg_at_10,
                "relevant_primary_count": len(primary),
            }
        )
    return {"samples": samples, "confusion": confusion, "per_query": per_query}


async def calibrate(runs: int = 3, factor: float = 0.9) -> dict[str, Any]:
    """Run calibration over the 6-topic corpus and return a report blob.

    Report fields:
    - `runs`: number of repeated measurement passes
    - `per_query`: list of per-query metric rows (from the last run)
    - `floors`: per-(lang, type, metric) floor values
    - `aggregate_means`: per-(lang, type, metric) observed means
    - `index_stats`: chunks_indexed / files_indexed
    """
    portfolio = _load_sibling("query_portfolio")
    # Ensure drift_validator is loaded for tagged-chunk collection.
    tagged = collect_tagged_chunks()

    from memtomem.config import Mem2MemConfig
    import memtomem.config as _cfg
    from memtomem.server.component_factory import close_components, create_components

    tmp = Path(mkdtemp(prefix="b2v2_cal_"))
    db_path = tmp / "golden.db"
    mem_dir = tmp / "memories"
    mem_dir.mkdir()

    for lang in ("en", "ko"):
        for topic in CORE_TOPICS:
            src = FIXTURE_ROOT / lang / topic
            if not src.exists():
                raise SystemExit(f"Fixture missing: {src}")
            dst = mem_dir / lang / topic
            shutil.copytree(src, dst)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    config.embedding.dimension = 384

    _orig_load = _cfg.load_config_overrides
    _cfg.load_config_overrides = lambda c: None

    comp = await create_components(config)
    try:
        stats = await comp.index_engine.index_path(mem_dir, recursive=True)
        # Reparent tagged source_file keys to match the temp-copied corpus.
        # search results report the *temp* source_file; rewrite our
        # tagged keys to that temp path so the (source_file, heading)
        # join works.
        tagged_rewritten: list[TaggedChunk] = []
        for t in tagged:
            # original rooted at FIXTURE_ROOT; map to mem_dir root
            try:
                rel = t.source_file.relative_to(FIXTURE_ROOT.resolve())
            except ValueError:
                continue
            tagged_rewritten.append(
                TaggedChunk(
                    source_file=(mem_dir / rel).resolve(),
                    heading=t.heading,
                    primary=t.primary,
                    secondary=t.secondary,
                    lang=t.lang,
                )
            )

        ir_metrics = _load_ir_metrics()
        all_samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        confusion_total: dict[tuple[str, str, str], int] = defaultdict(int)
        last_per_query: list[dict] = []
        for _ in range(runs):
            result = await _calibrate_once(comp, portfolio.QUERIES, tagged_rewritten, ir_metrics)
            for key, values in result["samples"].items():
                all_samples[key].extend(values)
            for key, count in result["confusion"].items():
                confusion_total[key] += count
            last_per_query = result["per_query"]

        floors = compute_floors(all_samples, factor=factor)
        means = {
            key: round(statistics.fmean(values), 3) if values else 0.0
            for key, values in all_samples.items()
        }

        return {
            "runs": runs,
            "factor": factor,
            "index_stats": {
                "chunks_indexed": stats.indexed_chunks,
                "files_indexed": stats.total_files,
            },
            "aggregate_means": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in means.items()},
            "floors": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in floors.items()},
            "genre_confusion": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in confusion_total.items()},
            "per_query": last_per_query,
        }
    finally:
        _cfg.load_config_overrides = _orig_load
        await close_components(comp)
        shutil.rmtree(tmp)


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        f"B.2 v2 Phase 5d calibration — {report['runs']} run(s), floor factor {report['factor']}",
        f"indexed {report['index_stats']['chunks_indexed']} chunks "
        f"across {report['index_stats']['files_indexed']} files",
        "",
        f"{'lang':4} {'type':16} {'metric':10} {'mean':>7} {'floor':>7}",
    ]
    keys = sorted(report["aggregate_means"])
    for key in keys:
        lang, qtype, metric = key.split("|", 2)
        mean_val = report["aggregate_means"][key]
        floor_val = report["floors"][key]
        lines.append(f"{lang:4} {qtype:16} {metric:10} {mean_val:7.3f} {floor_val:7.2f}")

    # Genre confusion matrix (Phase 5e). Only genre_primary queries
    # contribute. One sub-table per language × expected_genre shows
    # the observed top-1 genre distribution across runs.
    confusion = report.get("genre_confusion", {})
    if confusion:
        lines += ["", "Genre confusion (top-1, genre_primary queries only):"]
        lines.append(f"{'lang':4} {'expected':14} {'observed':14} {'count':>5}")
        for key in sorted(confusion):
            lang, expected, observed = key.split("|", 2)
            lines.append(f"{lang:4} {expected:14} {observed:14} {confusion[key]:>5d}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B.2 v2 Phase 5d portfolio calibration")
    parser.add_argument("--runs", type=int, default=3, help="calibration runs (default 3)")
    parser.add_argument(
        "--factor",
        type=float,
        default=0.9,
        help="floor factor (default 0.9 → floor = round(mean × 0.9, 2))",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit full JSON report (for CI) instead of the formatted table",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(calibrate(runs=args.runs, factor=args.factor))

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
