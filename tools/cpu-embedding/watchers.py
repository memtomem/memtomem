"""Exercise 1/3 independent watcher consumers against one cloned source/DB.

This uses the real queue/debounce/flush pipeline with synthetic filesystem
notifications. Native OS observer delivery is outside this measurement.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from memtomem.config import Mem2MemConfig
from memtomem.runtime.components import create_components, close_components
from memtomem.indexing.watcher import FileWatcher, _STOP_SENTINEL


async def worker(args):
    data = json.loads(args.config.read_text())
    config = Mem2MemConfig(**data)
    comp = await create_components(config, load_ambient_config=False, entity_backfill=False)
    watcher = FileWatcher(comp.index_engine, config.indexing, debounce_ms=50)
    completed = asyncio.Event()
    original = comp.index_engine.index_file
    results = []
    counts = {"chunk_calls": 0, "embed_calls": 0}
    chunker = comp.index_engine.chunk_content
    embed = comp.embedder.embed_texts

    def chunk(*a, **kw):
        counts["chunk_calls"] += 1
        return chunker(*a, **kw)

    async def embedding(*a, **kw):
        counts["embed_calls"] += 1
        return await embed(*a, **kw)

    comp.index_engine.chunk_content = chunk
    comp.embedder.embed_texts = embedding

    async def index(*a, **kw):
        stats = await original(*a, **kw)
        results.append(
            {
                "indexed": stats.indexed_chunks,
                "skipped": stats.skipped_chunks,
                "errors": list(stats.errors),
            }
        )
        if not stats.errors:
            completed.set()
        return stats

    comp.index_engine.index_file = index
    task = asyncio.create_task(watcher._process_events())
    start = time.perf_counter()
    try:
        for _ in range(20):
            watcher._queue.put_nowait(args.source)
        await asyncio.wait_for(completed.wait(), 180)
        args.output.write_text(
            json.dumps(
                {
                    "elapsed_s": time.perf_counter() - start,
                    "results": results,
                    "counts": counts,
                    "model_loaded": getattr(comp.embedder, "_model", None) is not None,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        watcher._queue.put_nowait(_STOP_SENTINEL)
        await asyncio.wait_for(task, 10)
        await close_components(comp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clients", type=int, choices=[1, 3], default=3)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker(args))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    processes = []
    try:
        for i in range(args.clients):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        __file__,
                        "--worker",
                        "--config",
                        str(args.config),
                        "--source",
                        str(args.source),
                        "--output",
                        str(args.output / f"client-{i}.json"),
                    ]
                )
            )
        for process in processes:
            if process.wait(timeout=240) != 0:
                raise RuntimeError("Watcher child failed")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    main()
