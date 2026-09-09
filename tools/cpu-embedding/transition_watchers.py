"""Measure configured watchers using real filesystem writes in a private lab.

Each child owns its observer and runtime. The parent retains and reaps every
child; no production process or database is addressed.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

from memtomem.config import Mem2MemConfig
from memtomem.indexing.watcher import FileWatcher
from memtomem.runtime.components import create_components, close_components


async def worker(args):
    config = Mem2MemConfig(**json.loads((args.output / "config.json").read_text()))
    comp = await create_components(config, load_ambient_config=False, entity_backfill=False)
    watcher = FileWatcher(comp.index_engine, config.indexing)
    original_index = comp.index_engine.index_file
    original_chunk = comp.index_engine.chunk_content
    original_embed = comp.embedder.embed_texts
    results = []
    counts = {"chunk_calls": 0, "embed_calls": 0}
    done = asyncio.Event()

    def chunk(*a, **kw):
        counts["chunk_calls"] += 1
        return original_chunk(*a, **kw)

    async def embed(*a, **kw):
        counts["embed_calls"] += 1
        return await original_embed(*a, **kw)

    async def index(*a, **kw):
        stats = await original_index(*a, **kw)
        results.append(
            {
                "indexed": stats.indexed_chunks,
                "skipped": stats.skipped_chunks,
                "errors": stats.errors,
            }
        )
        if not stats.errors:
            done.set()
        return stats

    comp.index_engine.chunk_content = chunk
    comp.embedder.embed_texts = embed
    comp.index_engine.index_file = index
    started, cpu = time.perf_counter(), time.process_time()
    try:
        await watcher.start()
        (args.output / f"ready-{args.worker}").touch()
        await asyncio.wait_for(done.wait(), 180)
        # The parent finishes the 0.4-second identical-byte burst before the
        # default debounce window. Give late observer delivery one more window.
        await asyncio.sleep(max(1.2, watcher._debounce_s))
    finally:
        await watcher.stop()
        await close_components(comp)
    report = {
        "results": results,
        "counts": counts,
        "elapsed_s": time.perf_counter() - started,
        "cpu_s": time.process_time() - cpu,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
    }
    (args.output / f"client-{args.worker}.json").write_text(json.dumps(report, indent=2) + "\n")


def parent(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    source_root = args.output / "source"
    source_root.mkdir()
    source = source_root / "fixture.md"
    source.write_text("# Watcher\n\nInitial private laboratory fixture.\n")
    raw = json.loads(
        (
            args.snapshot
            / ("installed-effective-config.json" if args.profile == "A" else "original-config.json")
        ).read_text()
    )
    data = {
        "embedding": raw["embedding"],
        "storage": {"sqlite_path": str(args.output / "index.db")},
        "indexing": {"memory_dirs": [str(source_root)], "startup_backfill": False},
        "llm": {"enabled": False},
        "rerank": {"enabled": False},
    }
    if args.profile == "B":
        data["indexing"].update(
            hard_max_chunk_tokens=4096, chunk_tokenizer_path=raw["indexing"]["chunk_tokenizer_path"]
        )
    if args.profile == "C":
        from transition_preflight import candidate_config

        data = candidate_config(data).model_dump(mode="json")
    (args.output / "config.json").write_text(json.dumps(data, indent=2))

    # Initialize the shared schema once. This experiment measures established
    # watchers, separately from competing first-ever WAL/schema initialization.
    async def initialize():
        comp = await create_components(
            Mem2MemConfig(**data), load_ambient_config=False, entity_backfill=False
        )
        await close_components(comp)

    asyncio.run(initialize())
    children, streams = [], []
    try:
        for i in range(args.clients):
            stream = (args.output / f"client-{i}.log").open("w")
            streams.append(stream)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        __file__,
                        "--snapshot",
                        str(args.snapshot),
                        "--profile",
                        args.profile,
                        "--output",
                        str(args.output),
                        "--worker",
                        str(i),
                    ],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            )
        deadline = time.monotonic() + 60
        while not all((args.output / f"ready-{i}").exists() for i in range(args.clients)):
            if time.monotonic() > deadline or any(p.poll() is not None for p in children):
                raise RuntimeError("Watcher failed before barrier")
            time.sleep(0.05)
        body = (
            "# Watcher\n\n"
            + "한국어와 English 검색 인덱싱 실험. 파일 변경 알림을 병합합니다.\n" * 20
        )
        for _ in range(20):
            source.write_text(body)
            time.sleep(0.02)
        for child in children:
            if child.wait(timeout=210):
                raise RuntimeError("Watcher child failed")
        records = [
            json.loads((args.output / f"client-{i}.json").read_text()) for i in range(args.clients)
        ]
        report = {
            "profile": args.profile,
            "clients": args.clients,
            "delivery": "real filesystem writes with configured observer backend",
            "backend": data["indexing"].get("watcher_backend", "auto"),
            "writes": 20,
            "same_content_generation": True,
            "chunk_calls": sum(r["counts"]["chunk_calls"] for r in records),
            "embed_calls": sum(r["counts"]["embed_calls"] for r in records),
            "cpu_s": sum(r["cpu_s"] for r in records),
            "sum_individual_peak_rss_bytes": sum(r["peak_rss_bytes"] for r in records),
            "peak_note": "sum of individual peaks, not simultaneous physical memory",
            "records": records,
        }
        (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "records"}), flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["A", "B", "C"], required=True)
    parser.add_argument("--clients", type=int, choices=[1, 3], default=3)
    parser.add_argument("--worker", type=int)
    args = parser.parse_args()
    if args.worker is not None:
        asyncio.run(worker(args))
    else:
        parent(args)
