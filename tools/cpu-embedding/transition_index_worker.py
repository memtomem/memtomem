"""One isolated end-to-end indexing measurement (A/B/C).

Fresh clone DBs are performance artifacts, never production migrations. Ambient
configuration loading, LLM calls, and background watchers are disabled.
"""

import argparse
import asyncio
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import sys
import time

import memtomem
from memtomem.config import Mem2MemConfig
from memtomem.runtime.components import create_components, close_components


async def run(args):
    os.umask(0o077)
    root = args.snapshot.resolve()
    preflight = json.loads((root / "preflight.json").read_text())
    ready = {row["source"] for row in preflight["sources"] if row["status"] == "ready"}
    manifest = json.loads((root / "manifest.json").read_text())
    sources = [row for row in manifest["sources"] if row["source"] in ready]
    if args.limit:
        # Deterministic stratification by suffix and size quartile.
        groups = {}
        for row in sorted(sources, key=lambda item: (item["bytes"], item["id"])):
            groups.setdefault(Path(row["source"]).suffix, []).append(row)
        sample = []
        for items in groups.values():
            n = max(1, round(args.limit * len(items) / len(sources)))
            sample.extend(
                items[min(len(items) - 1, int((i + 0.5) * len(items) / n))] for i in range(n)
            )
        sources = sorted({row["id"]: row for row in sample}.values(), key=lambda item: item["id"])
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    mirror = output / "mirror"

    def relocated(path):
        return str(mirror / str(Path(path).expanduser().resolve()).lstrip("/"))

    for row in sources:
        dest = Path(relocated(row["source"]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / row["copy"], dest)
    config_file = (
        "installed-effective-config.json" if args.profile == "A" else "original-config.json"
    )
    data = json.loads((root / config_file).read_text())
    data["storage"]["sqlite_path"] = str(output / "index.db")
    data.setdefault("llm", {})["enabled"] = False
    data["indexing"]["memory_dirs"] = [
        relocated(p) for p in data["indexing"].get("memory_dirs", [])
    ]
    if args.profile == "C":
        from transition_preflight import candidate_config

        config = candidate_config(data)
    else:
        config = Mem2MemConfig(**data)
    (output / "runtime-config.json").write_text(config.model_dump_json(indent=2) + "\n")
    package = Path(memtomem.__file__).resolve().parent
    result = {
        "profile": args.profile,
        "scope": "evaluation-only privacy-accepted sources",
        "sources": len(sources),
        "excluded_sources": len(manifest["sources"]) - len(ready),
        "python": sys.version,
        "package": str(package),
        "version": importlib.metadata.version("memtomem"),
        "config_sha256": hashlib.sha256(
            json.dumps(config.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest(),
        "implementation": {
            name: hashlib.sha256((package / name).read_bytes()).hexdigest()
            for name in ("config.py", "embedding/onnx.py", "indexing/engine.py")
        },
        "libraries": {
            name: importlib.metadata.version(name)
            for name in ("onnxruntime", "fastembed", "numpy", "tokenizers")
        },
    }
    start = time.perf_counter()
    comp = await create_components(config, load_ambient_config=False, entity_backfill=False)
    try:
        # Record first model-load latency independently of indexing.
        await comp.embedder.embed_query("CPU measurement warmup")
        result["load_s"] = time.perf_counter() - start
        started, cpu = time.perf_counter(), time.process_time()
        errors = []
        indexed = 0
        for i, row in enumerate(sources):
            stats = await comp.index_engine.index_file(
                Path(relocated(row["source"])), path_scope="explicit"
            )
            indexed += stats.indexed_chunks
            if stats.errors:
                errors.append({"source_id": row["id"], "errors": stats.errors})
            if (i + 1) % 10 == 0:
                print(
                    json.dumps(
                        {
                            "profile": args.profile,
                            "done": i + 1,
                            "total": len(sources),
                            "index_s": time.perf_counter() - started,
                        }
                    ),
                    flush=True,
                )
        result.update(
            index_s=time.perf_counter() - started,
            cpu_s=time.process_time() - cpu,
            indexed=indexed,
            errors=errors,
        )
        start = time.perf_counter()
        repeat = 0
        for row in sources:
            stats = await comp.index_engine.index_file(
                Path(relocated(row["source"])), path_scope="explicit"
            )
            repeat += stats.indexed_chunks
        result.update(
            repeat_s=time.perf_counter() - start,
            repeat_indexed=repeat,
            peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
        )
        with closing(sqlite3.connect(output / "index.db")) as db:
            result["chunks"] = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        result["database_bytes"] = (output / "index.db").stat().st_size
    finally:
        await close_components(comp)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "profile",
                    "sources",
                    "load_s",
                    "index_s",
                    "cpu_s",
                    "repeat_s",
                    "peak_rss_bytes",
                    "chunks",
                )
            }
        ),
        flush=True,
    )
    if result["errors"]:
        raise SystemExit("Index errors: see private result")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=["A", "B", "C"], required=True)
    parser.add_argument("--limit", type=int, default=0)
    asyncio.run(run(parser.parse_args()))
