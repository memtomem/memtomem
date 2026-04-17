#!/usr/bin/env python3
"""B.2 v2 retrieval sensitivity spot-check — per-topic and cross-topic.

Measures genre-primary query divergence between BM25-only
(`rrf_weights=[1.0, 0.0]`) and dense-only (`rrf_weights=[0.0, 1.0]`)
configurations against a committed fixture corpus. Reports the
per-topic divergence count that phase1-validation § 10 (postgres) and
§ 11 (cost_optimization) consume.

Methodology canonical references:
- Divergence definition: `docs/planning/b2-v2-phase2b-ledger.md`
  § "Formal definitions" (fixed across 14 topics, no post-hoc redef)
- Query-body overlap rule: ibid. + `compute_idf_baseline.py`
  pre-measurement step
- Anchor reproduction: postgres should produce 0/8 divergence + 7/8
  BM25 top-1 + 7/8 dense top-1 (byte-identical across 2 consecutive
  runs)

Usage:
    uv run python tools/retrieval-eval/measure_sensitivity.py \
        --topic postgres
    uv run python tools/retrieval-eval/measure_sensitivity.py \
        --topic cost_optimization

Determinism:
    PYTHONHASHSEED=0 OMP_NUM_THREADS=1 uv run python \
        tools/retrieval-eval/measure_sensitivity.py --topic postgres

Two consecutive runs should produce byte-identical stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path
from tempfile import mkdtemp


# Canonical simple queries — topic-prefix + genre-anchor vocabulary.
# These are the queries used for the Phase 2b postgres anchor
# verification (0/8 divergence) and Phase 2c cost_optimization
# measurement (0/8 divergence, counter-prediction). Changing query
# text breaks comparability with historical numbers — treat as
# stable reference; add new query sets as separate named constants.
QUERIES = {
    "postgres": [
        ("postgres 절차 접속 CONFIG SET 수행", "ko", "runbook"),
        ("postgres KST 원인 후속 조치 장애", "ko", "postmortem"),
        ("postgres 대신 채택 결정 trade-off 감수", "ko", "adr"),
        ("postgres 증상 의심 만약 점검 진단", "ko", "troubleshooting"),
        ("postgres configure run verify inspect command", "en", "runbook"),
        ("postgres at UTC root cause follow-up", "en", "postmortem"),
        ("postgres chose over accepted re-evaluate revisit", "en", "adr"),
        ("postgres likely root cause workaround symptom", "en", "troubleshooting"),
    ],
    "cost_optimization": [
        ("cost 절차 접속 CONFIG SET 수행", "ko", "runbook"),
        ("cost KST 원인 후속 조치 장애", "ko", "postmortem"),
        ("cost 대신 채택 결정 trade-off 감수", "ko", "adr"),
        ("cost 증상 의심 만약 점검 진단", "ko", "troubleshooting"),
        ("cost configure run verify inspect command", "en", "runbook"),
        ("cost at UTC root cause follow-up", "en", "postmortem"),
        ("cost chose over accepted re-evaluate revisit", "en", "adr"),
        ("cost likely root cause workaround symptom", "en", "troubleshooting"),
    ],
    "security": [
        ("security 절차 접속 CONFIG SET 수행", "ko", "runbook"),
        ("security KST 원인 후속 조치 장애", "ko", "postmortem"),
        ("security 대신 채택 결정 trade-off 감수", "ko", "adr"),
        ("security 증상 의심 만약 점검 진단", "ko", "troubleshooting"),
        ("security configure run verify inspect command", "en", "runbook"),
        ("security at UTC root cause follow-up", "en", "postmortem"),
        ("security chose over accepted re-evaluate revisit", "en", "adr"),
        ("security likely root cause workaround symptom", "en", "troubleshooting"),
    ],
    "observability": [
        ("observability 절차 접속 CONFIG SET 수행", "ko", "runbook"),
        ("observability KST 원인 후속 조치 장애", "ko", "postmortem"),
        ("observability 대신 채택 결정 trade-off 감수", "ko", "adr"),
        ("observability 증상 의심 만약 점검 진단", "ko", "troubleshooting"),
        ("observability configure run verify inspect command", "en", "runbook"),
        ("observability at UTC root cause follow-up", "en", "postmortem"),
        ("observability chose over accepted re-evaluate revisit", "en", "adr"),
        ("observability likely root cause workaround symptom", "en", "troubleshooting"),
    ],
    # KO uses "kubernetes" instead of "k8s" — kiwi tokenizer drops
    # digit-containing abbreviations. Fixtures include "Kubernetes" in
    # each KO chunk body for fair retrieval. EN keeps "k8s".
    "k8s": [
        ("kubernetes 절차 접속 CONFIG SET 수행", "ko", "runbook"),
        ("kubernetes KST 원인 후속 조치 장애", "ko", "postmortem"),
        ("kubernetes 대신 채택 결정 trade-off 감수", "ko", "adr"),
        ("kubernetes 증상 의심 만약 점검 진단", "ko", "troubleshooting"),
        ("k8s configure run verify inspect command", "en", "runbook"),
        ("k8s at UTC root cause follow-up", "en", "postmortem"),
        ("k8s chose over accepted re-evaluate revisit", "en", "adr"),
        ("k8s likely root cause workaround symptom", "en", "troubleshooting"),
    ],
    # Add new topic query sets here as Phase 2c / 2d progresses. Keep
    # the structure identical to preserve result comparability.
}

# Strengthened queries (proper-noun-rich) — held in reserve. Per
# `b2-v2-phase2b-ledger.md` § "Sensitivity spot-check outcome" the
# simple queries above already produce 0/8 for both tested topics.
# Strengthened-query experiments are contingent on security showing
# unexpected results (see § "Security pre-registration" joint
# matrix).


FIXTURE_ROOT = Path("packages/memtomem/tests/fixtures/corpus_v2")
GENRES = {"runbook", "postmortem", "adr", "troubleshooting"}


async def measure_topic(topic: str) -> None:
    from memtomem.config import Mem2MemConfig
    import memtomem.config as _cfg
    from memtomem.server.component_factory import close_components, create_components

    if topic not in QUERIES:
        available = ", ".join(sorted(QUERIES))
        raise SystemExit(f"Unknown topic '{topic}'. Available: {available}")

    tmp = Path(mkdtemp(prefix=f"b2v2_{topic}_"))
    db_path = tmp / "golden.db"
    mem_dir = tmp / "memories"
    mem_dir.mkdir()

    for lang in ("en", "ko"):
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
        print(f"Topic: {topic}")
        print(f"Indexed: {stats.indexed_chunks} chunks across {stats.total_files} files")

        diverge = 0
        bm25_match = 0
        dense_match = 0
        print()
        print(f"{'lang':4} {'genre':16} {'BM25':18} {'dense':18} diverge  query")
        for q, lang, expected in QUERIES[topic]:
            bm25_res, _ = await comp.search_pipeline.search(q, top_k=3, rrf_weights=[1.0, 0.0])
            dense_res, _ = await comp.search_pipeline.search(q, top_k=3, rrf_weights=[0.0, 1.0])

            bm25_ids = [str(r.chunk.id) for r in bm25_res]
            dense_ids = [str(r.chunk.id) for r in dense_res]
            d = bm25_ids != dense_ids
            diverge += int(d)

            def genre_of(source_file: Path) -> str | None:
                name = source_file.stem
                return name if name in GENRES else None

            bm25_top = genre_of(bm25_res[0].chunk.metadata.source_file) if bm25_res else None
            dense_top = genre_of(dense_res[0].chunk.metadata.source_file) if dense_res else None
            bm25_match += int(bm25_top == expected)
            dense_match += int(dense_top == expected)

            print(
                f"{lang:4} {expected:16} {str(bm25_top):18} {str(dense_top):18} "
                f"{'YES' if d else 'no':7}  {q}"
            )

        print()
        n = len(QUERIES[topic])
        print(f"Divergence [1,0] vs [0,1] top-3: {diverge}/{n}")
        print(f"BM25-only top-1 matches expected genre: {bm25_match}/{n}")
        print(f"Dense-only top-1 matches expected genre: {dense_match}/{n}")
    finally:
        _cfg.load_config_overrides = _orig_load
        await close_components(comp)
        shutil.rmtree(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="B.2 v2 retrieval sensitivity spot-check")
    parser.add_argument(
        "--topic",
        required=True,
        help=f"Topic to measure (one of: {', '.join(sorted(QUERIES))})",
    )
    args = parser.parse_args()
    asyncio.run(measure_topic(args.topic))


if __name__ == "__main__":
    main()
