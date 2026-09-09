"""Replay actual queries on private before/after index copies, without qrels.

This measures the configured search path. Fresh-index metadata differences are
reported; results do not certify a metadata-preserving production migration.
"""

import argparse
import dataclasses
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import resource
import sqlite3
import statistics
import sys
import time

from memtomem.config import Mem2MemConfig
from memtomem.runtime.components import create_components, close_components


def percentiles(values):
    ordered = sorted(values)
    return {
        "p50_ms": statistics.median(values) * 1000,
        "p95_ms": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))] * 1000,
    }


def ranks(results):
    return [
        {
            "id": str(r.chunk.id),
            "source": str(r.chunk.metadata.source_file),
            "start_line": r.chunk.metadata.start_line,
            "end_line": r.chunk.metadata.end_line,
            "score": r.score,
            "retrieval_stage": r.source,
        }
        for r in results
    ]


async def run(args):
    os.umask(0o077)
    root = args.snapshot.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    data = json.loads((root / "installed-effective-config.json").read_text())
    source_db = root / "original.db" if args.profile in {"A", "B"} else root / "full-C/index.db"
    # SQLite backup includes a live WAL if one remains; never copy just its main file.
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(output / "query.db")) as dest:
            src.backup(dest)
    if args.profile == "C":
        from transition_preflight import candidate_config

        data = candidate_config(data).model_dump(mode="json")
        prefix = str(root / "full-C/mirror")
        with closing(sqlite3.connect(output / "query.db")) as db:
            for table in ("chunks", "chunks_fts"):
                db.execute(
                    f"UPDATE {table} SET source_file=substr(source_file,?) WHERE substr(source_file,1,?)=?",
                    (len(prefix) + 1, len(prefix), prefix),
                )
            db.commit()
    data["storage"]["sqlite_path"] = str(output / "query.db")
    data["llm"]["enabled"] = False
    config = Mem2MemConfig(**data)
    queries = json.loads((root / "queries.json").read_text())["queries"]
    comp = await create_components(config, load_ambient_config=False, entity_backfill=False)
    timings = {"dense": [], "hybrid_cold": [], "hybrid_cached": []}
    observations = []
    cached_hits = 0
    stage_ms = {}

    def timed(name, method):
        async def wrapped(*a, **kw):
            start = time.perf_counter()
            try:
                return await method(*a, **kw)
            finally:
                stage_ms[name] = stage_ms.get(name, 0) + (time.perf_counter() - start) * 1000

        return wrapped

    comp.storage.bm25_search = timed("bm25", comp.storage.bm25_search)
    comp.storage.dense_search = timed("vector_lookup", comp.storage.dense_search)
    comp.embedder.embed_query = timed("embedding", comp.embedder.embed_query)
    try:
        await comp.embedder.embed_query("CPU measurement warmup")
        cpu = time.process_time()
        started = time.perf_counter()
        for repeat in range(3):
            for i, q in enumerate(queries):
                start = time.perf_counter()
                vector = await comp.embedder.embed_query(q["text"])
                dense = await comp.storage.dense_search(vector, top_k=10)
                timings["dense"].append(time.perf_counter() - start)
                comp.search_pipeline.invalidate_cache()
                stage_ms.clear()
                start = time.perf_counter()
                cold, stats = await comp.search_pipeline.search(
                    q["text"], top_k=10, record=True, as_of_unix=args.as_of
                )
                timings["hybrid_cold"].append(time.perf_counter() - start)
                cold_stages = dict(stage_ms)
                start = time.perf_counter()
                cached, cached_stats = await comp.search_pipeline.search(
                    q["text"], top_k=10, record=True, as_of_unix=args.as_of
                )
                timings["hybrid_cached"].append(time.perf_counter() - start)
                cached_hits += int(cached_stats.cache_hit)
                if repeat == 0:
                    observations.append(
                        {
                            "query_id": q["id"],
                            "dense": ranks(dense),
                            "hybrid": ranks(cold),
                            "stats": dataclasses.asdict(stats),
                            "stage_ms": cold_stages,
                        }
                    )
                if (i + 1) % 25 == 0:
                    print(
                        json.dumps({"profile": args.profile, "repeat": repeat, "queries": i + 1}),
                        flush=True,
                    )
        result = {
            "profile": args.profile,
            "status": "UNJUDGED",
            "scope": "before stored corpus vs privacy-accepted fresh E5 clone; metadata not migrated",
            "queries": len(queries),
            "repeats": 3,
            "cpu_s": time.process_time() - cpu,
            "elapsed_s": time.perf_counter() - started,
            "rerank_enabled": config.rerank.enabled,
            "cached_hits": cached_hits,
            "timings": {key: percentiles(values) for key, values in timings.items()},
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "observations": observations,
        }
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in result.items() if k != "observations"}), flush=True)
    finally:
        await close_components(comp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["A", "B", "C"], required=True)
    parser.add_argument("--as-of", type=int)
    asyncio.run(run(parser.parse_args()))
