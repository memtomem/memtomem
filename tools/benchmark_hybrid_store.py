"""Synthetic local Store benchmark; emits JSON and never uses a model/network.

Example: python tools/benchmark_hybrid_store.py --sizes 1000 10000 100000
Peak RSS includes the Python runtime; sizes run in fresh subprocesses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from langgraph.store.base import PutOp

from memtomem.integrations import MemtomemHybridStore


async def embed(texts):
    return [
        [(value - 127.5) / 128 for value in hashlib.sha256(text.encode()).digest()]
        for text in texts
    ]


def measure(size: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="hybrid-bench-") as directory:
        path = Path(directory) / "store.db"
        index = {"embed": embed, "dims": 32, "fields": ["text"]}
        start = time.perf_counter()
        with MemtomemHybridStore(path, index=index, index_id="synthetic-sha256-v1") as store:
            for first in range(0, size, 1000):
                store.batch(
                    [
                        PutOp(
                            ("benchmark",),
                            str(i),
                            {
                                "text": f"Document {i} topic{i % 100} local agent memory",
                                "group": i % 100,
                            },
                        )
                        for i in range(first, min(first + 1000, size))
                    ]
                )
        ingest_seconds = time.perf_counter() - start
        modes = {}
        for mode in ("bm25", "dense", "hybrid"):
            with MemtomemHybridStore(
                path, index=index, index_id="synthetic-sha256-v1", retrieval={"mode": mode}
            ) as store:
                trials = {}
                for selective in (False, True):
                    samples = []
                    for run in range(11):
                        begin = time.perf_counter()
                        result = store.search_with_diagnostics(
                            ("benchmark",),
                            query="topic7",
                            filter={"group": 7} if selective else None,
                            refresh_ttl=False,
                        )
                        elapsed = (time.perf_counter() - begin) * 1000
                        if run:
                            samples.append(elapsed)
                        assert result["items"]
                    trials["filtered_1_percent" if selective else "unfiltered"] = {
                        "median_ms": round(statistics.median(samples), 3),
                        "max_ms": round(max(samples), 3),
                        "candidate_counts": result["diagnostics"]["candidate_counts"],
                    }
                modes[mode] = trials
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "size": size,
            "dims": 32,
            "ingest_seconds": round(ingest_seconds, 3),
            "database_bytes": path.stat().st_size,
            "peak_rss_mib": round(rss / (1024**2 if sys.platform == "darwin" else 1024), 2),
            "search": modes,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument("--child", type=int)
    args = parser.parse_args()
    if args.child is not None:
        print(json.dumps(measure(args.child)))
        return
    results = []
    for size in args.sizes:
        output = subprocess.check_output(
            [sys.executable, __file__, "--child", str(size)], text=True
        )
        results.append(json.loads(output))
    print(
        json.dumps(
            {
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "langgraph": version("langgraph"),
                "sqlite_vec": version("sqlite-vec"),
                "warmup": 1,
                "trials": 10,
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
