#!/usr/bin/env python3
"""Measure complete indexing of prepared clones into a fresh, isolated DB."""

import argparse
import asyncio
import dataclasses
import json
import time
from pathlib import Path

from memtomem.config import Mem2MemConfig
from memtomem.runtime.components import create_components, close_components


async def main(args):
    from benchmark_support import runtime_identity

    implementation = runtime_identity()
    data = json.loads((args.clone / "e5-config.json").read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    data["storage"]["sqlite_path"] = str(args.output / "index.sqlite3")
    data["embedding"] = {
        "provider": "onnx",
        "model": args.model,
        "dimension": 384 if "e5" in args.model else 1024,
        "threads": args.threads,
        "onnx_batch_size": args.batch,
        "max_sequence_tokens": 512 if "e5" in args.model else 0,
        "onnx_variant": args.variant,
        "onnx_artifact_path": args.artifact or "",
    }
    if args.model == "bge-m3":
        data["indexing"].update(
            hard_max_chunk_tokens=4096,
            chunk_context_tokens=512,
            chunk_model_tokens=8192,
            chunk_tokenizer_path=args.bge_tokenizer,
        )
    config = Mem2MemConfig(**data)
    comp = await create_components(config, load_ambient_config=False, entity_backfill=False)
    try:
        start = time.perf_counter()
        cpu = time.process_time()
        first = await comp.index_engine.index_path(args.clone / "sources")
        initial_s = time.perf_counter() - start
        initial_cpu = time.process_time() - cpu
        start = time.perf_counter()
        second = await comp.index_engine.index_path(args.clone / "sources")
        repeat_s = time.perf_counter() - start
        report = {
            "implementation": implementation,
            "model": args.model,
            "variant": args.variant,
            "threads": args.threads,
            "batch": args.batch,
            "index_s": initial_s,
            "cpu_s": initial_cpu,
            "repeat_s": repeat_s,
            "database_bytes": (args.output / "index.sqlite3").stat().st_size,
            "first": dataclasses.asdict(first),
            "repeat": dataclasses.asdict(second),
        }
        (args.output / "report.json").write_text(json.dumps(report, default=str, indent=2) + "\n")
        print(json.dumps({k: report[k] for k in ["model", "index_s", "cpu_s", "repeat_s"]}))
    finally:
        await close_components(comp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clone", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="multilingual-e5-small")
    parser.add_argument("--variant", default="fp32")
    parser.add_argument("--artifact")
    parser.add_argument("--bge-tokenizer")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch", type=int, default=4)
    asyncio.run(main(parser.parse_args()))
